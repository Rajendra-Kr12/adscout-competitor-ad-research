#!/usr/bin/env python3
"""Find candidate Facebook Pages for a brand name: one ads_archive keyword call per brand.

The query uses search_type=KEYWORD_EXACT_PHRASE (KEYWORD_UNORDERED would match each word
separately) on the brand's EU/UK countries, asks only for id, page_id and page_name, and reads
the first page of results (no paging), so a brand costs exactly 1 call through scripts/guard.py.
It prints candidates (Page ID, name, ads seen) for the user to confirm; it never picks one.
Confirm with: python scripts/project.py set-page <project> <brand> <page_id> --source discovery

Countries outside the EU/UK are skipped: ads_archive has no commercial ads there, and the Meta Ads
MCP route that could search them is not connected in this stage.

Usage:
  python scripts/discover_pages.py --project <slug> [--dry-run | --fixture F]   # every unresolved brand
  python scripts/discover_pages.py --name "Pukka Herbs" --country GB [--dry-run | --fixture F]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from common import DATA_DIR, DEFAULT_API_VERSION, EU_UK, ROOT, iso_z, load_env, slug, utc_now, write_private
from fetch_ads import MetaApiError, build_params, open_session
from guard import Guard, GuardStop
import project as projects

FIELDS = "id,page_id,page_name"
LIMIT = 500


def discovery_params(name: str, countries: list[str], today: date, token: str | None) -> dict:
    params = build_params(page_ids=None, search_terms=name, countries=countries,
                          date_min=(today - timedelta(days=365)).isoformat(), date_max=today.isoformat(),
                          media_type="ALL", limit=LIMIT, token=token, search_type="KEYWORD_EXACT_PHRASE")
    params["fields"] = FIELDS
    return params


def _words(s: str) -> set[str]:
    return set(re.findall(r"\w+", (s or "").lower()))


def candidates(ads: list[dict], name: str, truncated: bool) -> list[dict]:
    """Pages seen in the results, most ads first; name_match = every word of the brand in the Page name."""
    seen: dict[str, dict] = {}
    for ad in ads:
        pid = str(ad.get("page_id") or "")
        if not pid:
            continue
        c = seen.setdefault(pid, {"page_id": pid, "page_name": ad.get("page_name"), "ads_seen": 0})
        c["ads_seen"] += 1
    want = _words(name)
    out = sorted(seen.values(), key=lambda c: (-(want <= _words(c["page_name"])), -c["ads_seen"], c["page_id"]))
    for c in out:
        c["name_match"] = bool(want) and want <= _words(c["page_name"])
        c["ads_seen_is_lower_bound"] = truncated
    return out


def targets_from_args(args) -> list[dict]:
    if args.project:
        doc = projects.load_project(args.project, args.projects_dir)
        brands = projects.unresolved(doc) if not args.all else [
            b for b in doc["brands"] if b["coverage"]["ads_archive"]]
        return [{"key": b["key"], "name": b.get("search_terms") or b["name"],
                 "countries": b["coverage"]["ads_archive"]} for b in brands]
    if not (args.name and args.country):
        sys.exit("need --project, or --name with --country")
    countries = projects.parse_countries(args.country)
    return [{"key": slug(args.name), "name": args.name, "countries": [c for c in countries if c in EU_UK],
             "skipped": [c for c in countries if c not in EU_UK]}]


def main(argv=None, *, transport=None, guard: Guard | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project")
    ap.add_argument("--all", action="store_true", help="with --project: also brands that already have a Page ID")
    ap.add_argument("--name")
    ap.add_argument("--country", action="append")
    ap.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixture", type=Path)
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else utc_now().date()
    try:
        targets = targets_from_args(args)
    except projects.ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    api_version = load_env(args.env_file).get("META_API_VERSION") or DEFAULT_API_VERSION
    url = f"https://graph.facebook.com/{api_version}/ads_archive"
    for t in targets:
        if t.get("skipped"):
            print(f"# {t['name']}: {', '.join(t['skipped'])} skipped - outside the EU/UK ads_archive has no "
                  "commercial ads, and the Meta Ads MCP route is not connected in this stage", file=sys.stderr)
    targets = [t for t in targets if t["countries"]]
    if not targets:
        print("nothing to discover (no unresolved brand with an EU/UK country)", file=sys.stderr)
        print(json.dumps({"brands": []}, indent=2))
        return 0
    if args.dry_run:
        for t in targets:
            print(f"GET {url}?{urlencode(discovery_params(t['name'], t['countries'], today, 'REDACTED'))}")
        print(f"# {len(targets)} call(s), 1 per brand. Nothing was sent.", file=sys.stderr)
        return 0

    if transport is not None:
        from fetch_ads import Session
        session = Session(transport, "TEST-NOT-A-REAL-TOKEN", "live", guard or Guard(args.data_dir), False)
    else:
        session = open_session(fixture=args.fixture, env_file=args.env_file, data_dir=args.data_dir, guard=guard)
    g = session.guard
    results, rc = [], 0
    try:
        with g.lock():
            session.preflight(api_version, args.data_dir)
            run_id = "discover-" + utc_now().strftime("%Y%m%dT%H%M%SZ")
            g.start_run(run_id, target="discover", countries=sorted({c for t in targets for c in t["countries"]}),
                        windows_planned=[t["key"] for t in targets])
            stopped = None
            for t in targets:
                try:
                    body = session.transport.get(url, discovery_params(t["name"], t["countries"], today, session.token))
                except (MetaApiError, GuardStop) as exc:
                    stopped = exc
                    break
                g.ledger["pages"] += 1
                g.ledger["windows_done"].append(t["key"])
                ads = body.get("data") or []
                truncated = bool((body.get("paging") or {}).get("next"))
                results.append({"brand": t["key"], "name": t["name"], "countries": t["countries"],
                                "candidates": candidates(ads, t["name"], truncated)})
            missing = [t["key"] for t in targets if t["key"] not in g.ledger["windows_done"]]
            ledger = g.close_run("stopped" if stopped else "ok", windows_missing=missing,
                                 stop_reason=str(stopped) if stopped else None)
            if stopped:
                print(f"STOPPED: {stopped} (ledger {ledger})", file=sys.stderr)
                rc = 3 if isinstance(stopped, GuardStop) else 2
    except GuardStop as exc:
        print(f"guard refused the run: {exc}", file=sys.stderr)
        return 3
    doc = {"mode": session.mode, "searched_at": iso_z(utc_now()), "search_type": "KEYWORD_EXACT_PHRASE",
           "brands": results}
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    for r in results:
        write_private(args.data_dir / "state" / "discovery" / f"{r['brand']}.json",
                      json.dumps(r | {"mode": session.mode, "searched_at": doc["searched_at"]}, indent=2) + "\n")
    print(text, end="")
    for r in results:
        top = r["candidates"][:3]
        shown = "; ".join(f"{c['page_name']} - {c['page_id']} - {c['ads_seen']}"
                          f"{'+' if c['ads_seen_is_lower_bound'] else ''} ads" for c in top) or "no Pages found"
        print(f"# {r['name']} ({', '.join(r['countries'])}): {shown}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
