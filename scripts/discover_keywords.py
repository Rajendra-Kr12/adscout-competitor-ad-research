#!/usr/bin/env python3
"""Ad Library keyword search for discovery: lean fields, a hard call budget, stop on the first warning.

Discovery step 2 (skill meta-keyword-discovery). Every call goes through scripts/guard.py. The query asks
for a lean field list (config/discovery.yaml: Page, dates, headline, link caption; no ad text and no
ad_snapshot_url), so a page holds ~300 ads. ads_archive returns the newest ads first, so one query over a
whole quarter only samples its last few days (live run 2026-10-01: 4 of 5 keywords' 1,200-ad samples all
started on 28-30 Sep). Each keyword is therefore split into monthly or weekly windows with a page cap per
window, which spreads the sample over the range.

Safety: the plan (keywords x windows x pages) must fit --budget before anything is sent; nothing is retried
(code 1 included); the run stops at the first error, guard stop or usage warning (DiscoveryGuard turns the
guard's soft-limit pause into a stop). Records (lean fields only) go to data/discovery/<name>/<run_id>.jsonl (0600).

Usage:
  python scripts/discover_keywords.py --name <run-name> --keyword "dark circles" --keyword "menopause skin:unordered" \
      --country GB --since 2026-07 --until 2026-09 [--window month|week] [--pages-per-window 1] [--budget 25]
      [--dry-run | --fixture F]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

from common import (DATA_DIR, DEFAULT_API_VERSION, EU_UK, ROOT, api_last_day, assert_no_token, country_code,
                    load_discovery_config, load_env, month_windows, scrub_tokens, slug, utc_now, week_windows,
                    write_json, write_private)
from fetch_ads import MetaApiError, Session, build_params, iter_ads, open_session
from guard import Guard, GuardStop, load_config, usage_from_headers

SEARCH_TYPES = {"exact": "KEYWORD_EXACT_PHRASE", "unordered": "KEYWORD_UNORDERED"}


class UsageWarning(RuntimeError):
    """The guard reached a soft usage limit: discovery stops instead of sleeping and continuing."""


class DiscoveryGuard(Guard):
    """The project guard, caps unchanged, that stops at the first usage warning instead of pausing.

    Only the soft band changes: at or above usage_soft_pct (below usage_hard_pct) the peak is recorded and
    UsageWarning is raised. The hard limit still sets the guard's cooldown, and call spacing still sleeps."""

    def check_usage(self, headers) -> None:
        peaks, _ = usage_from_headers(headers)
        if peaks:
            name, peak = max(peaks.items(), key=lambda kv: kv[1])
            if self.api["usage_soft_pct"] <= peak < self.api["usage_hard_pct"]:
                if self.ledger is not None:
                    for k, v in peaks.items():
                        self.ledger["usage_peak"][k] = max(v, self.ledger["usage_peak"].get(k, 0))
                self._event("usage-soft", header=name, pct=peak)
                raise UsageWarning(f"{name} at {peak:.0f}% (guard soft limit {self.api['usage_soft_pct']}%): "
                                   "discovery stops at the first warning")
        super().check_usage(headers)


def discovery_guard(data_dir: Path, *, live: bool = True, **kwargs) -> Guard:
    """DiscoveryGuard with no retries (code 1 and every other error stop the run at once)."""
    cfg = load_config()
    cfg = {**cfg, "api": {**cfg["api"], "transient_retries": 0}}
    return DiscoveryGuard(data_dir, cfg, live=live, **kwargs)


def parse_keyword(value: str) -> tuple[str, str]:
    """'dark circles' -> exact phrase; 'menopause skin:unordered' -> words in any order."""
    term, _, kind = value.rpartition(":") if value.endswith((":exact", ":unordered")) else (value, "", "exact")
    term = term.strip()
    if not term:
        raise SystemExit(f"empty keyword in {value!r}")
    return term, SEARCH_TYPES[kind]


def plan(keywords, windows, pages_per_window: int) -> int:
    """Upper bound on ads_archive calls: one per page, every window capped at pages_per_window."""
    return len(keywords) * len(windows) * pages_per_window


def run(session: Session, *, keywords, countries, windows, fields, limit, pages_per_window, budget,
        api_version, out: Path) -> dict:
    """Search every keyword x window; returns the summary. The caller holds the guard lock."""
    g = session.guard
    base = f"https://graph.facebook.com/{api_version}/ads_archive"
    run_id = out.stem
    g.start_run(run_id, target=f"discovery:{out.parent.name}", countries=countries, raw=str(out),
                windows_planned=[f"{t}|{a}" for t, _ in keywords for a, _ in windows])
    summary = {"run_id": run_id, "countries": countries, "windows": windows, "budget": budget, "queries": [],
               "calls": 0, "stopped": None, "records": 0}
    records: list[dict] = []
    status = "ok"
    try:
        for term, stype in keywords:
            for dmin, dmax in windows:
                if g.run_calls >= budget:
                    raise GuardStop(f"discovery budget of {budget} call(s) reached", "cap")
                params = build_params(page_ids=None, search_terms=term, countries=countries, date_min=dmin,
                                      date_max=dmax, media_type="ALL", limit=limit, token=session.token,
                                      search_type=stype, fields=fields)
                n, capped = 0, False
                try:
                    for ad in iter_ads(session.transport, base, params,
                                       max_pages=min(pages_per_window, budget - g.run_calls)):
                        rec = {k: ad.get(k) for k in fields}
                        rec.update(keyword=term, window=f"{dmin}..{dmax}")
                        records.append(rec)
                        n += 1
                except MetaApiError as exc:
                    if not str(exc).startswith("paging stopped"):
                        raise
                    capped = True  # more results exist: this window is a sample
                summary["queries"].append({"keyword": term, "search_type": stype, "window": f"{dmin}..{dmax}",
                                           "ads": n, "capped": capped})
    except (GuardStop, UsageWarning, MetaApiError) as exc:
        summary["stopped"] = scrub_tokens(f"{type(exc).__name__}: {exc}")
        status = "stopped"
    finally:
        summary["calls"] = g.run_calls
        summary["records"] = len(records)
        g.close_run(status, records=len(records), stop_reason=summary["stopped"])
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    assert_no_token(text, str(out))
    write_private(out, text)
    write_json(out.with_suffix(".summary.json"), summary)
    return summary


def main(argv=None, *, transport=None, guard: Guard | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", required=True, help="discovery run name, e.g. the project slug")
    ap.add_argument("--keyword", action="append", required=True,
                    help="search term; add ':unordered' to match the words in any order (default exact phrase)")
    ap.add_argument("--country", action="append", default=None, help="EU/UK ISO-2 country (repeatable, default GB)")
    ap.add_argument("--since", required=True, help="first month, YYYY-MM")
    ap.add_argument("--until", required=True, help="last month, YYYY-MM (inclusive)")
    ap.add_argument("--window", choices=("month", "week"), default="month")
    ap.add_argument("--pages-per-window", type=int)
    ap.add_argument("--budget", type=int, help="hard cap on ads_archive calls (default config/discovery.yaml)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    ap.add_argument("--dry-run", action="store_true", help="print the requests and the call ceiling, send nothing")
    ap.add_argument("--fixture", type=Path, help="serve responses from a synthetic fixture (offline)")
    args = ap.parse_args(argv)

    cfg = load_discovery_config()["search"]
    budget = args.budget or cfg["budget"]
    pages = args.pages_per_window or cfg["pages_per_window"]
    limit = args.limit or cfg["limit"]
    fields = list(cfg["fields"])
    keywords = [parse_keyword(k) for k in args.keyword]
    countries = [country_code(c) for c in (args.country or ["GB"])]
    if bad := [c for c in countries if c not in EU_UK]:
        sys.exit(f"{', '.join(bad)}: ads_archive has commercial ads only for EU/UK countries")
    make = week_windows if args.window == "week" else month_windows
    windows = make(args.since, args.until, last_day=api_last_day())
    ceiling = plan(keywords, windows, pages)
    if ceiling > budget:
        sys.exit(f"plan needs up to {ceiling} call(s) ({len(keywords)} keyword(s) x {len(windows)} {args.window} "
                 f"window(s) x {pages} page(s)) but the budget is {budget}. Use fewer keywords, --window month, "
                 "fewer pages per window or a larger --budget.")
    api_version = load_env(args.env_file).get("META_API_VERSION") or DEFAULT_API_VERSION

    if args.dry_run:
        for term, stype in keywords:
            for dmin, dmax in windows:
                p = build_params(page_ids=None, search_terms=term, countries=countries, date_min=dmin, date_max=dmax,
                                 media_type="ALL", limit=limit, token="REDACTED", search_type=stype, fields=fields)
                print(f"GET https://graph.facebook.com/{api_version}/ads_archive?{urlencode(p)}")
        g = guard or Guard(args.data_dir)
        c = g.counts()
        print(f"# at most {ceiling} ads_archive call(s) (budget {budget}) + at most 1 token check per day. Nothing was "
              f"sent. Guard: {c['hour']}/{g.api['max_calls_per_hour']} this hour, {c['day']}/{g.api['max_calls_per_day']} "
              "in 24h.", file=sys.stderr)
        return 0

    if transport is not None:  # injected (tests)
        session = Session(transport, "TEST-NOT-A-REAL-TOKEN", "live", guard or discovery_guard(args.data_dir), False)
    else:
        g = guard or discovery_guard(args.data_dir, live=not args.fixture)
        session = open_session(fixture=args.fixture, env_file=args.env_file, data_dir=args.data_dir, guard=g)
    out = args.data_dir / "discovery" / slug(args.name) / f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    try:
        with session.guard.lock():
            session.preflight(api_version, args.data_dir)
            summary = run(session, keywords=keywords, countries=countries, windows=windows, fields=fields, limit=limit,
                          pages_per_window=pages, budget=budget, api_version=api_version, out=out)
    except GuardStop as exc:
        print(f"guard refused the run: {exc}", file=sys.stderr)
        return 3
    capped = sum(q["capped"] for q in summary["queries"])
    print(f"{summary['records']} ad record(s) from {len(summary['queries'])} keyword x window quer(ies); "
          f"{summary['calls']} ads_archive call(s); {capped} window(s) hit the page cap (samples). Records: {out}")
    if summary["stopped"]:
        print(f"STOPPED: {summary['stopped']}. Do not retry; relay it.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
