#!/usr/bin/env python3
"""Fetch every ad for a Page (or keyword) from the official Ad Library API (ads_archive).

Every call goes through scripts/guard.py (kill switch, cooldown, caps, spacing, lock,
ledger in data/runs/). Throttle errors are never retried. The access token is stripped
from ad_snapshot_url before anything is written, and raw files are 0600. A run that
stops early has no summary line, and normalize.py refuses it unless --allow-partial.
Nothing here touches facebook.com pages.

Examples:
  python scripts/fetch_ads.py --competitor forest-essentials-uk --since 2025-10 --until 2026-09
  python scripts/fetch_ads.py --search-terms "Pukka Herbs" --country GB --since 2026-01 --until 2026-09
  python scripts/fetch_ads.py --competitor kapiva --since 2026-01 --until 2026-09 --dry-run
  python scripts/fetch_ads.py --competitor forest-essentials-uk --since 2026-01 --until 2026-09 \
      --fixture tests/fixtures/ads_archive_synthetic.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

from common import (
    DATA_DIR,
    DEFAULT_API_VERSION,
    api_last_day,
    ROOT,
    EU_UK,
    assert_no_token,
    iso_z,
    load_competitors,
    load_env,
    month_windows,
    scrub_tokens,
    slug,
    strip_token_params,
    utc_now,
)
from guard import THROTTLE_CODES, TRANSIENT_CODES, Guard, GuardStop, classify_error
from token_store import TokenStoreError, get_token

FIELDS = [
    "id", "page_id", "page_name", "ad_creation_time", "ad_delivery_start_time",
    "ad_delivery_stop_time", "ad_creative_bodies", "ad_creative_link_titles",
    "ad_creative_link_descriptions", "ad_creative_link_captions", "ad_snapshot_url",
    "languages", "publisher_platforms", "eu_total_reach", "total_reach_by_location",
    "age_country_gender_reach_breakdown", "target_ages", "target_gender",
    "target_locations", "beneficiary_payers",
]
MEDIA_TYPES = ("ALL", "IMAGE", "MEME", "VIDEO", "NONE")
SEARCH_TYPES = ("KEYWORD_EXACT_PHRASE", "KEYWORD_UNORDERED")
ACCESS_ERRORS = {
    2332002: "Ad Library API access not approved yet: finish identity confirmation at "
             "https://www.facebook.com/ID and wait for approval (it can take a few days).",
    2332004: "An app access token was used. ads_archive needs a USER access token "
             "(Graph API Explorer > Generate Access Token), then exchange it for a long-lived one.",
    190: "Access token invalid or expired. Generate a new user token and re-exchange it "
         "(README: 'Long-lived token').",
}


class MetaApiError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None):
        super().__init__(scrub_tokens(message))
        self.code, self.subcode = code, subcode


def meta_said(e: dict) -> str:
    """Graph's message plus its user-facing title/detail, which name the bad parameter for code 100."""
    parts = [str(e.get("message") or "")]
    for k in ("error_user_title", "error_user_msg"):
        if e.get(k) and str(e[k]) not in parts:
            parts.append(str(e[k]))
    return " - ".join(p for p in parts if p)  # MetaApiError scrubs tokens from the whole message


class GraphTransport:
    """GET through the guard. Retries only transient failures (codes 1/2, HTTP 5xx, network).

    Throttle codes (4, 17, 32, 341, 613, 80000-80014) are never retried: the guard sets a
    cooldown and the error is raised, because Meta says to stop calling at the limit.
    """

    def __init__(self, session=None, *, guard: Guard | None = None, retries: int = 3,
                 delays=(2, 8, 30), sleep=time.sleep, log=None, soft_kinds: tuple[str, ...] = ()):
        self.session = session or requests.Session()
        self.guard = guard
        self.soft_kinds = soft_kinds
        self.retries, self.delays = retries, list(delays)
        self.sleep = sleep
        self.log = log or (lambda msg: print(scrub_tokens(msg), file=sys.stderr))

    def get(self, url: str, params: dict | None) -> dict:
        attempt = 0
        while True:
            if self.guard:
                self.guard.before_call()
            code = subcode = status = None
            headers: dict = {}
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                err = MetaApiError(f"network error: {exc}")
            else:
                status, headers = resp.status_code, resp.headers
                try:
                    body = resp.json()
                except ValueError:
                    body = {"error": {"message": f"non-JSON response (HTTP {status})"}}
                if status == 200 and "error" not in body:
                    if self.guard:
                        self.guard.after_success(headers)
                    return body
                e = body.get("error") or {}
                code, subcode = e.get("code"), e.get("error_subcode")
                hint = next((ACCESS_ERRORS[c] for c in (subcode, code) if c in ACCESS_ERRORS), None)
                said = meta_said(e)
                if hint:
                    err = MetaApiError(f"[{subcode or code}] {hint} (Meta said: {said})", code, subcode)
                else:
                    err = MetaApiError(f"HTTP {status} code={code} subcode={subcode}: {said}", code, subcode)
            if self.guard:
                self.guard.after_failure(code=code, subcode=subcode, status=status, headers=headers,
                                         message=str(err), soft_kinds=self.soft_kinds)
            fatal = code in THROTTLE_CODES or classify_error(code, subcode) is not None
            transient = code in TRANSIENT_CODES or status is None or status >= 500
            if fatal or not transient or attempt >= min(self.retries, len(self.delays)):
                raise err
            delay = self.delays[attempt]
            self.log(f"transient error, retry {attempt + 1}/{self.retries} in {delay}s: {err}")
            self.sleep(delay)
            attempt += 1


class GuardedFixture:
    """Offline transport that still passes through the guard (kill switch, run cap, ledger)."""

    def __init__(self, inner, guard: Guard):
        self.inner, self.guard = inner, guard

    def get(self, url: str, params: dict | None) -> dict:
        self.guard.before_call()
        body = self.inner.get(url, params)
        self.guard.after_success({})
        return body


def iter_ads(transport, url: str, params: dict, *, max_pages: int = 50, on_page=None):
    """Yield every ad across pages by following paging.next, up to max_pages pages."""
    seen: set[str] = set()
    pages = 0
    p: dict | None = params
    while url:
        body = transport.get(url, p)
        pages += 1
        if on_page:
            on_page()
        yield from body.get("data") or []
        url = (body.get("paging") or {}).get("next")
        p = None  # the next URL already carries every parameter
        if url and (url in seen or pages >= max_pages):
            raise MetaApiError(f"paging stopped: repeated next URL or over {max_pages} pages")
        if url:
            seen.add(url)


def build_params(*, page_ids, search_terms, countries, date_min, date_max, media_type,
                 limit, token, search_type: str = "KEYWORD_EXACT_PHRASE") -> dict:
    params = {
        "ad_reached_countries": json.dumps(countries),
        "ad_active_status": "ALL",
        "ad_type": "ALL",
        "ad_delivery_date_min": date_min,
        "ad_delivery_date_max": date_max,
        "fields": ",".join(FIELDS),
        "limit": str(limit),
    }
    if page_ids:
        params["search_page_ids"] = json.dumps([str(p) for p in page_ids])
    if search_terms:
        params["search_terms"] = search_terms
        params["search_type"] = search_type
    if media_type != "ALL":
        params["media_type"] = media_type
    if token:
        params["access_token"] = token
    return params


def zero_ads_note(countries: list[str]) -> str:
    outside = [c for c in countries if c not in EU_UK and c != "ALL"]
    if outside and len(outside) == len(countries):
        return (f"0 ads returned for {', '.join(countries)}. Meta's Ad Library API only covers commercial "
                "ads delivered to the EU/UK, so this is a coverage limit, not an error.")
    return ("0 ads returned. Commercial ads are only returned when delivered to the EU/UK; "
            "check coverage in README before assuming the Page ID is wrong.")


def resolve_target(args, competitors) -> dict:
    target = {"key": args.out_key, "page_ids": [], "search_terms": None, "search_type": None,
              "countries": None, "competitor": None}
    if args.competitor:
        if args.competitor not in competitors:
            sys.exit(f"unknown competitor '{args.competitor}'. Known: {', '.join(competitors)}")
        c = competitors[args.competitor]
        target.update(competitor=c["key"], countries=c.get("countries"), search_type=c.get("search_type"))
        if c.get("page_id") and not args.search_terms:
            target["page_ids"] = [str(c["page_id"])]
        else:
            target["search_terms"] = args.search_terms or c.get("search_terms")
        target["key"] = target["key"] or c["key"]
    if args.page_id:
        target["page_ids"] = [str(p) for p in args.page_id]
        target["search_terms"] = None
    if args.search_terms and not args.competitor:
        target["search_terms"] = args.search_terms
    if args.search_type:
        target["search_type"] = args.search_type
    if target["search_terms"]:
        target["search_type"] = (target["search_type"] or "KEYWORD_EXACT_PHRASE").upper()
        if target["search_type"] not in SEARCH_TYPES:
            sys.exit(f"unknown search type {target['search_type']!r}; use one of {', '.join(SEARCH_TYPES)}")
    else:
        target["search_type"] = None
    if args.country:
        target["countries"] = [c.upper() for c in args.country]
    if not (target["page_ids"] or target["search_terms"]):
        sys.exit("need --competitor, --page-id, or --search-terms")
    if len(target["page_ids"]) > 10:
        sys.exit("ads_archive accepts at most 10 page ids per query")
    if not target["countries"]:
        sys.exit("need --country (ad_reached_countries is required), or a competitor with countries")
    target["key"] = target["key"] or (
        target["page_ids"][0] if target["page_ids"] else "terms-" + slug(target["search_terms"]))
    return target


def open_private(path: Path):
    """Create a new 0600 file for writing (raw pulls are Platform Data)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")


def raw_line(rec: dict) -> str:
    line = json.dumps(strip_token_params(rec), ensure_ascii=False)
    assert_no_token(line, "a raw record")  # anything the stripper missed stops the run
    return line + "\n"


def fetch_run(*, target: dict, windows, media_types, since: str, until: str, limit: int,
              transport, token: str | None, mode: str, api_version: str, guard: Guard,
              data_dir: Path) -> dict:
    """One guarded fetch run for one target. Returns rc (0 ok, 2 API stop, 3 guard stop),
    the raw path, record counts and the ledger path. The caller holds the guard lock."""
    started = utc_now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    out = data_dir / "raw" / target["key"] / f"{run_id}.jsonl"
    n = 1
    while out.exists():  # two runs in the same second for one target
        n += 1
        run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{n}"
        out = out.with_name(f"{run_id}.jsonl")
    header = {"type": "run", "run_id": run_id, "mode": mode, "api_version": api_version,
              "target": target, "since": since, "until": until,
              "media_types": media_types, "windows": windows, "started_at": iso_z(started)}
    planned = [f"{dmin}/{mt}" for (dmin, _) in windows for mt in media_types]
    guard.start_run(run_id, target=target["key"], countries=target["countries"],
                    raw=str(out.relative_to(data_dir)), windows_planned=planned)
    try:
        result = _fetch_windows(out, header, planned, windows, media_types, target, limit, token,
                                transport, guard, f"https://graph.facebook.com/{api_version}/ads_archive")
    except BaseException as exc:
        guard.close_run("error", stop_reason=scrub_tokens(f"{type(exc).__name__}: {exc}"))
        raise
    stopped, total, unique = result["stopped"], result["total"], result["unique"]
    calls = guard.run_calls
    ledger = guard.close_run("stopped" if stopped else "ok", windows_missing=result["missing"],
                             records=total, unique_ads=len(unique),
                             stop_reason=str(stopped) if stopped else None)
    rc = (3 if isinstance(stopped, GuardStop) else 2) if stopped else 0
    return {"rc": rc, "raw": out, "run_id": run_id, "records": total, "unique_ads": len(unique),
            "calls": calls, "stopped": stopped, "missing": result["missing"], "ledger": ledger}


def run_fetch(**kwargs) -> int:
    res = fetch_run(**kwargs)
    mode, out = kwargs["mode"], res["raw"]
    if res["stopped"]:
        print(f"{mode}: STOPPED after {res['calls']} call(s), {res['records']} records: {res['stopped']}",
              file=sys.stderr)
        print(f"partial run (normalize.py refuses it without --allow-partial): {out}", file=sys.stderr)
        print(f"ledger: {res['ledger']}", file=sys.stderr)
        return res["rc"]
    print(f"{mode}: {res['records']} records, {res['unique_ads']} unique ads in {res['calls']} call(s) -> {out}",
          file=sys.stderr)
    if not res["unique_ads"]:
        print(zero_ads_note(kwargs["target"]["countries"]), file=sys.stderr)
    print(out)
    return 0


def _fetch_windows(out: Path, header: dict, planned: list[str], windows, media_types, target: dict,
                   limit: int, token, transport, guard: Guard, base_url: str) -> dict:
    """Write the raw file window by window; stop cleanly on an API error or a guard refusal."""
    unique: set[str] = set()
    per_window: dict[str, int] = {}
    total = 0
    stopped: Exception | None = None

    def count_page():
        guard.ledger["pages"] += 1

    with open_private(out) as fh:
        fh.write(raw_line(header))
        for (dmin, dmax) in windows:
            for mt in media_types:
                if stopped:
                    break
                params = build_params(page_ids=target["page_ids"], search_terms=target["search_terms"],
                                      countries=target["countries"], date_min=dmin, date_max=dmax,
                                      media_type=mt, limit=limit, token=token,
                                      search_type=target["search_type"] or "KEYWORD_EXACT_PHRASE")
                n = 0
                try:
                    for ad in iter_ads(transport, base_url, params,
                                       max_pages=guard.api["max_pages_per_query"], on_page=count_page):
                        rec = {"type": "ad", "window": [dmin, dmax], "media_type": mt,
                               "countries": target["countries"], "fetched_at": iso_z(utc_now()), "ad": ad}
                        fh.write(raw_line(rec))
                        unique.add(str(ad.get("id")))
                        n += 1
                except (MetaApiError, GuardStop) as exc:
                    stopped = exc
                    break
                per_window[f"{dmin}/{mt}"] = n
                total += n
                guard.ledger["windows_done"].append(f"{dmin}/{mt}")
        missing = [w for w in planned if w not in per_window]
        if stopped:
            fh.write(raw_line({"type": "stopped", "reason": str(stopped), "records": total,
                               "windows_missing": missing, "stopped_at": iso_z(utc_now())}))
        else:
            fh.write(raw_line({"type": "summary", "records": total, "unique_ads": len(unique),
                               "per_window": per_window, "finished_at": iso_z(utc_now())}))
    return {"stopped": stopped, "total": total, "unique": unique, "missing": missing}


def check_token_status(token: str, guard: Guard, api_version: str, data_dir: Path) -> None:
    """Daily debug_token check with expiry warnings. Only a kill-switch outcome blocks the fetch."""
    from token_cli import ensure_status
    t = GraphTransport(guard=guard, retries=0, soft_kinds=("permission",))
    try:
        ensure_status(token, guard, t, api_version, data_dir)
    except MetaApiError as exc:
        print(f"token status check failed (continuing): {exc}", file=sys.stderr)


@dataclass
class Session:
    """A guarded transport plus the token it sends (live) or a placeholder (fixture/test)."""
    transport: object
    token: str
    mode: str
    guard: Guard
    check_token: bool

    def preflight(self, api_version: str, data_dir: Path) -> None:
        """Kill switch and cooldown, then the daily token check for live runs. Call inside the lock."""
        self.guard.preflight()
        if self.check_token:
            check_token_status(self.token, self.guard, api_version, data_dir)
            self.guard.preflight()  # debug_token may have tripped the kill switch


def open_session(*, fixture: Path | None, env_file: Path, data_dir: Path, guard: Guard | None = None) -> Session:
    """Fixture transport (offline, still guarded) or the live Graph transport with the stored token."""
    if fixture:
        from fixture_api import FixtureTransport
        guard = guard or Guard(data_dir, live=False)
        return Session(GuardedFixture(FixtureTransport(fixture), guard), "FIXTURE-NOT-A-REAL-TOKEN", "fixture",
                       guard, False)
    guard = guard or Guard(data_dir)
    try:
        token, source = get_token(guard.cfg, data_dir, env_path=env_file)
    except TokenStoreError as exc:
        sys.exit(f"token store: {exc}")
    if not token:
        sys.exit(f"No Meta token in the {source} store. See README 'Long-lived token' "
                 "(scripts/token_cli.py), or use --dry-run / --fixture.")
    transport = GraphTransport(guard=guard, retries=guard.api["transient_retries"],
                               delays=guard.api["transient_delays_s"])
    return Session(transport, token, "live", guard, True)


def main(argv=None, *, transport=None, guard: Guard | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_argument_group("target")
    src.add_argument("--competitor", help="key from config/competitors.yaml")
    src.add_argument("--page-id", action="append", help="numeric Page ID (repeatable, max 10)")
    src.add_argument("--search-terms", help="keyword query instead of page ids")
    src.add_argument("--search-type", choices=SEARCH_TYPES,
                     help="keyword matching (default KEYWORD_EXACT_PHRASE; KEYWORD_UNORDERED matches the "
                          "words separately, in any order)")
    src.add_argument("--country", action="append",
                     help="ad_reached_countries, any ISO-2 code (repeatable). Outside the EU/UK only "
                          "political/issue ads exist in the API")
    ap.add_argument("--since", required=True, help="first month, YYYY-MM")
    ap.add_argument("--until", required=True, help="last month, YYYY-MM (inclusive)")
    ap.add_argument("--single-window", action="store_true",
                    help="one query for the whole range instead of one per calendar month")
    ap.add_argument("--media-types", default="ALL",
                    help="comma list of ALL,IMAGE,MEME,VIDEO,NONE. Per-type queries fill media_type_hint")
    ap.add_argument("--limit", type=int, default=500, help="page size (API max 5000)")
    ap.add_argument("--out-key", help="folder name under data/raw (default: competitor key or page id)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env",
                    help="the .env token store and API version (default: the project .env)")
    ap.add_argument("--dry-run", action="store_true", help="print the request plan and exit")
    ap.add_argument("--fixture", type=Path, help="serve responses from a synthetic fixture (offline)")
    args = ap.parse_args(argv)

    env = load_env(args.env_file)
    api_version = env.get("META_API_VERSION") or DEFAULT_API_VERSION
    target = resolve_target(args, load_competitors())
    media_types = [m.strip().upper() for m in args.media_types.split(",") if m.strip()]
    if bad := [m for m in media_types if m not in MEDIA_TYPES]:
        sys.exit(f"unknown media type(s): {bad}")
    windows = month_windows(args.since, args.until, last_day=api_last_day())
    if args.single_window:
        windows = [(windows[0][0], windows[-1][1])]
    base_url = f"https://graph.facebook.com/{api_version}/ads_archive"

    if args.dry_run:
        for (dmin, dmax) in windows:
            for mt in media_types:
                params = build_params(page_ids=target["page_ids"], search_terms=target["search_terms"],
                                      countries=target["countries"], date_min=dmin, date_max=dmax,
                                      media_type=mt, limit=args.limit, token="REDACTED",
                                      search_type=target["search_type"] or "KEYWORD_EXACT_PHRASE")
                print(f"GET {base_url}?{urlencode(params)}")
        g = guard or Guard(args.data_dir)
        c = g.counts()
        print(f"# {len(windows) * len(media_types)} queries (+ paging). Nothing was sent. Guard caps: "
              f"{g.api['max_calls_per_run']}/run, {c['hour']}/{g.api['max_calls_per_hour']} used this hour, "
              f"{c['day']}/{g.api['max_calls_per_day']} used in 24h.", file=sys.stderr)
        return 0

    if transport is not None:  # injected (tests)
        session = Session(transport, "TEST-NOT-A-REAL-TOKEN", "live", guard or Guard(args.data_dir), False)
    else:
        session = open_session(fixture=args.fixture, env_file=args.env_file, data_dir=args.data_dir, guard=guard)
    try:
        with session.guard.lock():
            session.preflight(api_version, args.data_dir)
            return run_fetch(target=target, windows=windows, media_types=media_types, since=args.since,
                             until=args.until, limit=args.limit, transport=session.transport,
                             token=session.token, mode=session.mode, api_version=api_version,
                             guard=session.guard, data_dir=args.data_dir)
    except GuardStop as exc:
        print(f"guard refused the run: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
