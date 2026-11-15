#!/usr/bin/env python3
"""Normalize a raw ads_archive run into data/ads/<target>/<library_id>.json (schema v0.2).

Only whitelisted fields are copied. ad_snapshot_url (which embeds the access token)
never leaves data/raw/. Existing llm{} blocks are kept when an ad is re-normalized.

A run that stopped early (no closing summary line) is refused unless --allow-partial,
so a throttled or killed run is never mistaken for a complete month range.

Usage:
  python scripts/normalize.py data/raw/<target>/<run_id>.jsonl
  python scripts/normalize.py --target <target>            # latest run for that target
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

import jsonschema

from common import (
    DATA_DIR,
    EU_UK,
    SCHEMA_PATH,
    months_between,
    parse_ts,
    permalink,
    write_json,
)
from text_metrics import SCHEMA_VERSION, assign_near_duplicates, compute, upgrade_ad

EU_FIELDS = ("eu_total_reach", "total_reach_by_location", "age_country_gender_reach_breakdown",
             "target_ages", "target_gender", "target_locations", "beneficiary_payers")
POLITICAL_FIELDS = ("spend", "impressions", "currency", "demographic_distribution",
                    "delivery_by_region", "estimated_audience_size", "bylines")


@lru_cache(maxsize=1)
def validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


class PartialRunError(ValueError):
    pass


def read_run(path: Path, allow_partial: bool = False) -> tuple[dict, list[dict]]:
    header, records, summary, stopped = {}, [], None, None
    # Split on "\n" only: str.splitlines() also breaks on U+2028, U+0085 etc., which can sit inside ad text.
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "run":
            header = rec
        elif rec.get("type") == "ad":
            records.append(rec)
        elif rec.get("type") == "summary":
            summary = rec
        elif rec.get("type") == "stopped":
            stopped = rec
    if not header:
        raise ValueError(f"{path}: missing run header line")
    if summary is None and not allow_partial:
        why = f"it stopped early: {stopped.get('reason')}; missing windows: {stopped.get('windows_missing')}" \
            if stopped else "it has no closing summary line (aborted or still running)"
        raise PartialRunError(f"{path.name} is a partial run ({why}). Re-fetch, or pass --allow-partial "
                              "to normalize what it has.")
    return header, records


def _strs(value) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def coverage_basis(ad: dict, header: dict) -> str:
    if header.get("mode") == "fixture":
        return "fixture"
    if any(k in ad for k in ("spend", "impressions", "bylines")):
        return "political_issue"
    countries = set((header.get("target") or {}).get("countries") or [])
    if countries and not countries & EU_UK and "ALL" not in countries:
        return "political_issue"  # outside EU/UK the API only returns political/issue ads
    return "eu_uk_delivery"


def build_dates(ad: dict, fetched_at: str) -> dict:
    start = parse_ts(ad.get("ad_delivery_start_time"))
    stop = parse_ts(ad.get("ad_delivery_stop_time"))
    fetched = parse_ts(fetched_at)
    end = stop if stop and stop <= fetched else fetched
    return {
        "created": ad.get("ad_creation_time"),
        "delivery_start": ad.get("ad_delivery_start_time"),
        "delivery_stop": ad.get("ad_delivery_stop_time"),
        "start_month": f"{start.year:04d}-{start.month:02d}" if start else None,
        "active_months": months_between(start.date(), end.date()) if start and end >= start else [],
        # Inclusive calendar days from first impression to stop (or to the fetch date if active).
        "days_running": (end.date() - start.date()).days + 1 if start and end >= start else None,
        "active_at_fetch": stop is None or stop.date() >= fetched.date(),
    }


def normalize_ad(rec: dict, header: dict, media_hint: str | None, existing: dict | None) -> dict:
    ad = rec["ad"]
    text = {
        "bodies": _strs(ad.get("ad_creative_bodies")),
        "link_titles": _strs(ad.get("ad_creative_link_titles")),
        "link_descriptions": _strs(ad.get("ad_creative_link_descriptions")),
        "link_captions": _strs(ad.get("ad_creative_link_captions")),
    }
    eu = {k: ad[k] for k in EU_FIELDS if k in ad}
    political = {k: ad[k] for k in POLITICAL_FIELDS if k in ad}
    llm = (existing or {}).get("llm")
    prov_old = (existing or {}).get("provenance") or {}
    basis = coverage_basis(ad, header)
    out = {
        "schema_version": SCHEMA_VERSION,
        "library_id": str(ad["id"]),
        "page": {"id": str(ad["page_id"]) if ad.get("page_id") else None, "name": ad.get("page_name")},
        "permalink": permalink(ad["id"]),
        "coverage_basis": basis,
        "dates": build_dates(ad, rec["fetched_at"]),
        "platforms": _strs(ad.get("publisher_platforms")),
        "languages": _strs(ad.get("languages")),
        "media_type_hint": media_hint,
        "text": text,
        "eu_metrics": eu or None,
        "political_metrics": political or None,
        "derived": compute(text, eu, basis),
        "llm": llm,
        "creative": None,
        "provenance": {
            "api_version": header.get("api_version"),
            "fetched_at": rec["fetched_at"],
            "run_id": header["run_id"],
            "extractor_model": prov_old.get("extractor_model") if llm else None,
            "prompt_version": prov_old.get("prompt_version") if llm else None,
        },
    }
    return out


def normalize_run(raw_path: Path, data_dir: Path = DATA_DIR, only_page_id: str | None = None,
                  allow_partial: bool = False, page_map: dict[str, str] | None = None) -> Path:
    """Normalize one raw run into data/ads/<key>/.

    page_map ({page_id: brand_key}) splits a grouped multi-Page run (scripts/run_project.py) into
    one folder per brand; ads from Pages not in the map are dropped. Returns data/ads/ in that case.
    """
    header, records = read_run(raw_path, allow_partial)
    if page_map is not None:
        by_key: dict[str, list[dict]] = {k: [] for k in page_map.values()}
        for rec in records:
            key = page_map.get(str(rec["ad"].get("page_id")))
            if key:
                by_key[key].append(rec)
        for key, recs in by_key.items():
            _normalize_records(recs, header, data_dir / "ads" / key, raw_path)
        return data_dir / "ads"
    if only_page_id:
        records = [r for r in records if str(r["ad"].get("page_id")) == str(only_page_id)]
    return _normalize_records(records, header, data_dir / "ads" / header["target"]["key"], raw_path)


def _normalize_records(records: list[dict], header: dict, out_dir: Path, raw_path: Path) -> Path:
    # Last record per library id wins (latest fetch); media hint from any typed query.
    latest: dict[str, dict] = {}
    hints: dict[str, str] = {}
    for rec in records:
        lid = str(rec["ad"]["id"])
        latest[lid] = rec
        if rec.get("media_type") and rec["media_type"] != "ALL":
            hints[lid] = rec["media_type"]

    ads: dict[str, dict] = {}
    if out_dir.exists():
        for f in out_dir.glob("*.json"):
            ads[f.stem] = upgrade_ad(json.loads(f.read_text()))
    for lid, rec in latest.items():
        ads[lid] = normalize_ad(rec, header, hints.get(lid), ads.get(lid))
    assign_near_duplicates(list(ads.values()))

    for lid, ad in ads.items():
        errors = sorted(validator().iter_errors(ad), key=lambda e: list(e.path))
        if errors:
            raise ValueError(f"ad {lid} fails schema: {errors[0].message} at {list(errors[0].path)}")
        write_json(out_dir / f"{lid}.json", ad)
    print(f"normalized {len(latest)} ads from {raw_path.name} ({len(ads)} total) -> {out_dir}",
          file=sys.stderr)
    return out_dir


def latest_run(data_dir: Path, target: str) -> Path:
    runs = sorted((data_dir / "raw" / target).glob("*.jsonl"))
    if not runs:
        sys.exit(f"no raw runs under {data_dir / 'raw' / target}")
    return runs[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="raw ads_archive JSONL -> normalized per-ad JSON")
    ap.add_argument("raw", nargs="?", type=Path, help="data/raw/<target>/<run_id>.jsonl")
    ap.add_argument("--target", help="use the latest run for this target key")
    ap.add_argument("--only-page-id", help="keep only ads from this Page (useful for keyword runs)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="normalize a run that stopped early (its missing windows stay missing)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)
    raw = args.raw or (latest_run(args.data_dir, args.target) if args.target else None)
    if not raw:
        ap.error("give a raw file or --target")
    try:
        print(normalize_run(raw, args.data_dir, args.only_page_id, args.allow_partial))
    except PartialRunError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
