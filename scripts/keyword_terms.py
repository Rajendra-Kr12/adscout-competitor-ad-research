#!/usr/bin/env python3
"""Candidate search keywords from known brands' ad openings, weighted by distinct copy clusters.

Discovery step 1 (skill meta-keyword-discovery). No API calls: reads normalized ads (data/ads/<brand>/).
An ad's opening is its first line plus its headlines. Each copy cluster (derived.near_duplicate_of, else the
ad itself) counts once, so a brand that re-runs one message hundreds of times does not swamp the ranking.
Creative-only ads and ads outside --language are skipped. Terms are 1-3 word n-grams that neither start nor
end with a stopword (config/discovery.yaml). Ranking: multi-word terms used by the most brands, then by most
clusters, then single words. The agent picks the final search terms from this list; the script never does.

Usage:
  python scripts/keyword_terms.py --project <slug> [--language en] [--top 40] [--out F]
  python scripts/keyword_terms.py --brand upcircle --brand evolve-beauty [--language en]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from common import DATA_DIR, load_discovery_config, write_json
import project as projects

WORD_RE = re.compile(r"[a-z][a-z'\-]+")


def opening(ad: dict) -> str:
    return " ".join([ad["derived"].get("first_line") or ""] + list(ad["text"].get("link_titles") or []))


def clusters_for(data_dir: Path, brand: str, language: str | None) -> dict[str, str]:
    """{cluster id: opening text} for one brand: one opening per distinct copy cluster."""
    out: dict[str, str] = {}
    for f in sorted((data_dir / "ads" / brand).glob("*.json")):
        ad = json.loads(f.read_text())
        if ad["derived"].get("text_status") == "creative-only":
            continue
        if language and ad.get("languages") and language not in ad["languages"]:
            continue
        root = ad["derived"].get("near_duplicate_of") or ad["library_id"]
        out.setdefault(root, opening(ad))
    return out


def grams(text: str, stop: set[str]) -> set[str]:
    words = WORD_RE.findall(text.lower().replace("’", "'"))
    out = set()
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            g = words[i:i + n]
            if g[0] in stop or g[-1] in stop or (n == 1 and len(g[0]) < 4):
                continue
            out.add(" ".join(g))
    return out


def rank_terms(clusters: dict[str, dict[str, str]], stop: set[str], min_clusters: int = 3) -> list[dict]:
    """clusters: {brand: {cluster: opening}} -> ranked terms with cluster and brand counts."""
    count, brands = Counter(), defaultdict(set)
    for brand, cs in clusters.items():
        for text in cs.values():
            for g in grams(text, stop):
                count[g] += 1
                brands[g].add(brand)
    rows = [{"term": t, "words": len(t.split()), "clusters": c, "brands": len(brands[t]), "brand_keys": sorted(brands[t])}
            for t, c in count.items() if c >= min_clusters]
    rows.sort(key=lambda r: (r["words"] == 1, -r["brands"], -r["clusters"], r["term"]))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--project", help="project slug: use its brands")
    src.add_argument("--brand", action="append", help="brand key under data/ads (repeatable)")
    ap.add_argument("--language", default="en", help="keep ads tagged with this language ('' for all)")
    ap.add_argument("--top", type=int, default=40, help="rows to print")
    ap.add_argument("--min-clusters", type=int, default=3)
    ap.add_argument("--out", type=Path, help="write the full ranking as JSON")
    ap.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--config", type=Path, help="discovery config (default config/discovery.yaml)")
    args = ap.parse_args(argv)

    cfg = load_discovery_config(args.config) if args.config else load_discovery_config()
    stop = set(cfg["terms"]["stopwords"].split())
    keys = args.brand or [b["key"] for b in projects.load_project(args.project, args.projects_dir)["brands"]]
    stop |= {w for k in keys for w in re.split(r"[-_]", k)}  # brand names are never search terms
    clusters = {k: clusters_for(args.data_dir, k, args.language or None) for k in keys}
    rows = rank_terms(clusters, stop, args.min_clusters)
    result = {"brands": {k: len(v) for k, v in clusters.items()}, "language": args.language or None, "terms": rows}
    if args.out:
        write_json(args.out, result)
    print("distinct copy clusters per brand:", json.dumps(result["brands"]))
    print("clusters  brands  term")
    for r in rows[:args.top]:
        print(f"{r['clusters']:>8}  {r['brands']:>6}  {r['term']}")
    if not rows:
        print("no terms: are the brands' ads normalized under data/ads/?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
