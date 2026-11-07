#!/usr/bin/env python3
"""Deterministic plumbing around the ad-extractor sub-agent.

plan:  split normalized ads into batch input files for ad-extractor sub-agents.
       python scripts/llm_batches.py plan --target <key> [--size 20] [--all] [--unique-only]
       -> data/llm/<key>/<batch_run>/input-NN.json (prints one line per batch:
          "<input path> -> <output path>")

merge: validate sub-agent outputs against schemas/ad.schema.json#/$defs/llm and
       write llm{} + provenance into data/ads/<key>/<library_id>.json.
       python scripts/llm_batches.py merge --target <key> data/llm/<key>/<batch_run>/output-*.json
       Exit code 1 if any result was rejected (they are listed; re-run those batches).

Output file shape (written by the sub-agent):
  {"prompt_version": "extract-v1", "extractor_model": "sonnet",
   "results": [{"library_id": "123", "llm": {...}}]}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jsonschema

from common import DATA_DIR, SCHEMA_PATH, utc_now, write_json

SLIM_DERIVED = ("variant_count", "body_words", "has_price", "has_percent_off", "landing_domain",
                "near_duplicate_of")


def llm_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    sub = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/llm"}
    return jsonschema.Draft202012Validator(sub)


def load_ads(data_dir: Path, target: str) -> dict[str, tuple[Path, dict]]:
    ad_dir = data_dir / "ads" / target
    if not ad_dir.exists():
        sys.exit(f"no normalized ads in {ad_dir}; run normalize.py first")
    return {f.stem: (f, json.loads(f.read_text())) for f in sorted(ad_dir.glob("*.json"))}


def slim(ad: dict) -> dict:
    return {
        "library_id": ad["library_id"],
        "page_name": ad["page"]["name"],
        "start_month": ad["dates"]["start_month"],
        "days_running": ad["dates"]["days_running"],
        "platforms": ad["platforms"],
        "media_type_hint": ad["media_type_hint"],
        "text": ad["text"],
        "derived": {k: ad["derived"][k] for k in SLIM_DERIVED},
    }


def plan(args) -> int:
    ads = load_ads(args.data_dir, args.target)
    todo = [ad for _, ad in ads.values() if args.all or ad.get("llm") is None]
    if args.unique_only:
        todo = [ad for ad in todo if not ad["derived"].get("near_duplicate_of")]
    todo.sort(key=lambda a: (a["dates"]["delivery_start"] or "", a["library_id"]))
    if not todo:
        print("nothing to extract (all ads have llm{}; pass --all to redo)", file=sys.stderr)
        return 0
    run_dir = args.data_dir / "llm" / args.target / utc_now().strftime("%Y%m%dT%H%M%SZ")
    for i in range(0, len(todo), args.size):
        n = i // args.size + 1
        inp = run_dir / f"input-{n:02d}.json"
        write_json(inp, {"target": args.target, "batch": n, "ads": [slim(a) for a in todo[i:i + args.size]]})
        print(f"{inp} -> {run_dir / f'output-{n:02d}.json'}")
    print(f"{len(todo)} ads in {-(-len(todo) // args.size)} batches", file=sys.stderr)
    return 0


def merge(args) -> int:
    ads = load_ads(args.data_dir, args.target)
    v = llm_validator()
    merged, rejected = 0, []
    for out in args.outputs:
        doc = json.loads(Path(out).read_text())
        for res in doc.get("results", []):
            lid = str(res.get("library_id"))
            if lid not in ads:
                rejected.append((out, lid, "unknown library_id"))
                continue
            errors = list(v.iter_errors(res.get("llm")))
            if errors:
                rejected.append((out, lid, errors[0].message))
                continue
            path, ad = ads[lid]
            ad["llm"] = res["llm"]
            ad["provenance"]["extractor_model"] = doc.get("extractor_model")
            ad["provenance"]["prompt_version"] = doc.get("prompt_version")
            write_json(path, ad)
            merged += 1
    for out, lid, why in rejected:
        print(f"REJECTED {lid} in {out}: {why}", file=sys.stderr)
    missing = [lid for lid, (_, ad) in ads.items() if ad.get("llm") is None]
    print(f"merged {merged}, rejected {len(rejected)}, still without llm: {len(missing)}", file=sys.stderr)
    return 1 if rejected else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="plan/merge ad-extractor batches")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--target", required=True)
    p.add_argument("--size", type=int, default=20)
    p.add_argument("--all", action="store_true", help="include ads that already have llm{}")
    p.add_argument("--unique-only", action="store_true",
                   help="skip near-duplicates (they count as volume only and get no llm{})")
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    m = sub.add_parser("merge")
    m.add_argument("--target", required=True)
    m.add_argument("outputs", nargs="+", type=Path)
    m.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)
    return plan(args) if args.cmd == "plan" else merge(args)


if __name__ == "__main__":
    sys.exit(main())
