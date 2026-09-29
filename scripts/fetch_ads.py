#!/usr/bin/env python3
"""Fetch every ad for a Page (or keyword) from the official Ad Library API (ads_archive).

Writes verbatim API records to data/raw/<target>/<run_id>.jsonl. Raw files contain
ad_snapshot_url values that embed the access token, so data/raw/ is git-ignored and
retention-limited (see CLAUDE.md). Nothing here touches facebook.com pages.

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
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from common import (
    DATA_DIR,
    DEFAULT_API_VERSION,
    iso_z,
    load_competitors,
    load_env,
    month_windows,
    scrub_tokens,
    slug,
    utc_now,
)

FIELDS = [
    "id", "page_id", "page_name", "ad_creation_time", "ad_delivery_start_time",
    "ad_delivery_stop_time", "ad_creative_bodies", "ad_creative_link_titles",
    "ad_creative_link_descriptions", "ad_creative_link_captions", "ad_snapshot_url",
    "languages", "publisher_platforms", "eu_total_reach", "total_reach_by_location",
    "age_country_gender_reach_breakdown", "target_ages", "target_gender",
    "target_locations", "beneficiary_payers",
]
MEDIA_TYPES = ("ALL", "IMAGE", "MEME", "VIDEO", "NONE")

# Graph error codes worth retrying: transient (1, 2), app/user/page/custom rate limits
# (4, 17, 32, 613), and the Ads-API throttling family (80000-80014).
RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 613} | set(range(80000, 80015))
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


class GraphTransport:
    """GET with retry/backoff on rate-limit and transient errors."""

    def __init__(self, session=None, *, max_retries: int = 5, base_delay: float = 2.0,
                 max_delay: float = 300.0, sleep=time.sleep, log=None):
        self.session = session or requests.Session()
        self.max_retries, self.base_delay, self.max_delay = max_retries, base_delay, max_delay
        self.sleep = sleep
        self.log = log or (lambda msg: print(msg, file=sys.stderr))

    def _backoff(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2 ** attempt))
        return delay + random.uniform(0, delay / 4)

    def get(self, url: str, params: dict | None) -> dict:
        attempt = 0
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                err, status = MetaApiError(f"network error: {exc}"), None
            else:
                status = resp.status_code
                try:
                    body = resp.json()
                except ValueError:
                    body = {"error": {"message": f"non-JSON response (HTTP {status})"}}
                if status == 200 and "error" not in body:
                    self._respect_usage(resp.headers)
                    return body
                e = body.get("error") or {}
                code, subcode = e.get("code"), e.get("error_subcode")
                for c in (subcode, code):
                    if c in ACCESS_ERRORS:
                        raise MetaApiError(f"[{c}] {ACCESS_ERRORS[c]} (Meta said: {e.get('message')})",
                                           code, subcode)
                err = MetaApiError(f"HTTP {status} code={code} subcode={subcode}: {e.get('message')}",
                                   code, subcode)
                if code not in RETRYABLE_CODES and not (status and status >= 500):
                    raise err
            if attempt >= self.max_retries:
                raise err
            delay = self._backoff(attempt)
            self.log(f"retry {attempt + 1}/{self.max_retries} in {delay:.0f}s: {err}")
            self.sleep(delay)
            attempt += 1

    def _respect_usage(self, headers) -> None:
        """Pause when X-App-Usage says we are close to the hourly app limit."""
        raw = (headers or {}).get("x-app-usage") or (headers or {}).get("X-App-Usage")
        if not raw:
            return
        try:
            usage = json.loads(raw)
            peak = max(float(v) for v in usage.values())
        except (ValueError, TypeError, AttributeError):
            return
        if peak >= 95:
            self.log(f"X-App-Usage at {peak:.0f}%, pausing 60s")
            self.sleep(60)


def iter_ads(transport, url: str, params: dict, *, max_pages: int = 10000):
    """Yield every ad across pages by following paging.next until it disappears."""
    seen: set[str] = set()
    pages = 0
    p: dict | None = params
    while url:
        body = transport.get(url, p)
        pages += 1
        yield from body.get("data") or []
        url = (body.get("paging") or {}).get("next")
        p = None  # the next URL already carries every parameter
        if url in seen or pages >= max_pages:
            raise MetaApiError(f"paging stopped: repeated next URL or over {max_pages} pages")
        if url:
            seen.add(url)


def build_params(*, page_ids, search_terms, countries, date_min, date_max, media_type,
                 limit, token) -> dict:
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
    if media_type != "ALL":
        params["media_type"] = media_type
    if token:
        params["access_token"] = token
    return params


def resolve_target(args, competitors) -> dict:
    target = {"key": args.out_key, "page_ids": [], "search_terms": None, "countries": None,
              "competitor": None}
    if args.competitor:
        if args.competitor not in competitors:
            sys.exit(f"unknown competitor '{args.competitor}'. Known: {', '.join(competitors)}")
        c = competitors[args.competitor]
        target.update(competitor=c["key"], countries=c.get("countries"))
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_argument_group("target")
    src.add_argument("--competitor", help="key from config/competitors.yaml")
    src.add_argument("--page-id", action="append", help="numeric Page ID (repeatable, max 10)")
    src.add_argument("--search-terms", help="keyword query instead of page ids")
    src.add_argument("--country", action="append", help="ad_reached_countries (repeatable), e.g. GB")
    ap.add_argument("--since", required=True, help="first month, YYYY-MM")
    ap.add_argument("--until", required=True, help="last month, YYYY-MM (inclusive)")
    ap.add_argument("--single-window", action="store_true",
                    help="one query for the whole range instead of one per calendar month")
    ap.add_argument("--media-types", default="ALL",
                    help="comma list of ALL,IMAGE,MEME,VIDEO,NONE. Per-type queries fill media_type_hint")
    ap.add_argument("--limit", type=int, default=500, help="page size (API max 5000)")
    ap.add_argument("--out-key", help="folder name under data/raw (default: competitor key or page id)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--dry-run", action="store_true", help="print the request plan and exit")
    ap.add_argument("--fixture", type=Path, help="serve responses from a synthetic fixture (offline)")
    args = ap.parse_args(argv)

    env = load_env()
    api_version = env.get("META_API_VERSION") or DEFAULT_API_VERSION
    target = resolve_target(args, load_competitors())
    media_types = [m.strip().upper() for m in args.media_types.split(",") if m.strip()]
    if bad := [m for m in media_types if m not in MEDIA_TYPES]:
        sys.exit(f"unknown media type(s): {bad}")
    windows = month_windows(args.since, args.until)
    if args.single_window:
        windows = [(windows[0][0], windows[-1][1])]
    base_url = f"https://graph.facebook.com/{api_version}/ads_archive"

    if args.dry_run:
        for (dmin, dmax) in windows:
            for mt in media_types:
                params = build_params(page_ids=target["page_ids"], search_terms=target["search_terms"],
                                      countries=target["countries"], date_min=dmin, date_max=dmax,
                                      media_type=mt, limit=args.limit, token="REDACTED")
                print(f"GET {base_url}?{urlencode(params)}")
        print(f"# {len(windows) * len(media_types)} queries (+ paging). Nothing was sent.", file=sys.stderr)
        return 0

    if args.fixture:
        from fixture_api import FixtureTransport
        transport, token, mode = FixtureTransport(args.fixture), "FIXTURE-NOT-A-REAL-TOKEN", "fixture"
    else:
        token = env.get("META_ACCESS_TOKEN")
        if not token:
            sys.exit("META_ACCESS_TOKEN missing. Copy .env.example to .env (see README), "
                     "or use --dry-run / --fixture.")
        transport, mode = GraphTransport(), "live"

    started = utc_now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    out = args.data_dir / "raw" / target["key"] / f"{run_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "run", "run_id": run_id, "mode": mode, "api_version": api_version,
              "target": target, "since": args.since, "until": args.until,
              "media_types": media_types, "windows": windows, "started_at": iso_z(started)}
    unique: set[str] = set()
    total = 0
    per_window: dict[str, int] = {}
    with out.open("w") as fh:
        fh.write(json.dumps(header) + "\n")
        for (dmin, dmax) in windows:
            for mt in media_types:
                params = build_params(page_ids=target["page_ids"], search_terms=target["search_terms"],
                                      countries=target["countries"], date_min=dmin, date_max=dmax,
                                      media_type=mt, limit=args.limit, token=token)
                n = 0
                for ad in iter_ads(transport, base_url, params):
                    rec = {"type": "ad", "window": [dmin, dmax], "media_type": mt,
                           "countries": target["countries"], "fetched_at": iso_z(utc_now()), "ad": ad}
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    unique.add(str(ad.get("id")))
                    n += 1
                per_window[f"{dmin}/{mt}"] = n
                total += n
        summary = {"type": "summary", "records": total, "unique_ads": len(unique),
                   "per_window": per_window, "finished_at": iso_z(utc_now())}
        fh.write(json.dumps(summary) + "\n")

    print(f"{mode}: {total} records, {len(unique)} unique ads -> {out}", file=sys.stderr)
    if not unique:
        print("0 ads returned. Commercial ads are only returned when delivered to the EU/UK; "
              "check coverage in README before assuming the Page ID is wrong.", file=sys.stderr)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
