#!/usr/bin/env python3
"""Deterministic text metrics for normalized ads (no LLM).

compute(text) -> the per-ad `derived` block except near_duplicate_of.
assign_near_duplicates(ads) -> fills derived.near_duplicate_of across one Page's ads.

CLI: python scripts/text_metrics.py data/ads/<target>   # recompute in place
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from common import write_json

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF]"
)
HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
PRICE_RE = re.compile(
    r"(?:[£$€₹]\s?\d[\d,]*(?:\.\d+)?)"
    r"|(?:\b(?:rs\.?|inr|gbp|usd|eur)\s?\d[\d,]*(?:\.\d+)?)"
    r"|(?:\b\d[\d,]*(?:\.\d+)?\s?(?:gbp|inr|usd|eur|rupees)\b)",
    re.IGNORECASE,
)
PERCENT_OFF_RE = re.compile(
    r"\d{1,3}\s?%\s*(?:off|discount)|(?:save|up\s?to|flat|extra)\s*\d{1,3}\s?%",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.IGNORECASE)
NEAR_DUP_THRESHOLD = 0.85


def _nonempty_lines(s: str) -> list[str]:
    return [ln for ln in s.splitlines() if ln.strip()]


def landing_domain(captions: list[str]) -> str | None:
    for cap in captions:
        m = DOMAIN_RE.match(cap.strip())
        if m:
            return m.group(1).lower()
    return None


def compute(text: dict) -> dict:
    bodies = [b for b in text.get("bodies", []) if b is not None]
    all_text = "\n".join(bodies + text.get("link_titles", []) + text.get("link_descriptions", []))
    first = next((ln.strip() for b in bodies for ln in _nonempty_lines(b)), None)
    return {
        "body_chars": [len(b) for b in bodies],
        "body_words": [len(b.split()) for b in bodies],
        "body_lines": [len(_nonempty_lines(b)) for b in bodies],
        "variant_count": len({b.strip() for b in bodies if b.strip()}),
        "emoji_count": len(EMOJI_RE.findall(all_text)),
        "hashtag_count": len(HASHTAG_RE.findall(all_text)),
        "has_price": bool(PRICE_RE.search(all_text)),
        "has_percent_off": bool(PERCENT_OFF_RE.search(all_text)),
        "first_line": first[:200] if first else None,
        "landing_domain": landing_domain(text.get("link_captions", [])),
        "near_duplicate_of": None,
    }


def _shingles(s: str, k: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", s.lower())
    if len(words) < k:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def assign_near_duplicates(ads: list[dict], threshold: float = NEAR_DUP_THRESHOLD) -> None:
    """Point each ad at the earliest earlier ad whose body text is >= threshold similar."""
    ordered = sorted(ads, key=lambda a: (a["dates"]["delivery_start"] or "9999", a["library_id"]))
    seen: list[tuple[str, set]] = []
    for ad in ordered:
        sh = _shingles("\n".join(ad["text"]["bodies"]))
        ad["derived"]["near_duplicate_of"] = next(
            (lid for lid, other in seen if jaccard(sh, other) >= threshold), None)
        if sh:
            seen.append((ad["library_id"], sh))


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    files = sorted(Path(argv[0]).glob("*.json"))
    ads = [json.loads(f.read_text()) for f in files]
    for ad in ads:
        ad["derived"] = compute(ad["text"])
    assign_near_duplicates(ads)
    for f, ad in zip(files, ads):
        write_json(f, ad)
    print(f"recomputed metrics for {len(ads)} ads in {argv[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
