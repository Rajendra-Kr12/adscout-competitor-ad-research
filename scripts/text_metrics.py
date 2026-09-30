#!/usr/bin/env python3
"""Deterministic text and reach metrics for normalized ads (no LLM).

compute(text, eu_metrics) -> the per-ad `derived` block except near_duplicate_of.
assign_near_duplicates(ads) -> fills derived.near_duplicate_of across one Page's ads.
upgrade_ad(ad) -> brings an older normalized ad up to the current schema in memory.

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
SCHEMA_VERSION = "0.2"
TEXT_FIELDS = ("bodies", "link_titles", "link_descriptions", "link_captions")
# total_reach_by_location key for the UK: live v26.0 data uses "GB" (observed 2026-09-30) while the
# ArchivedAd reference says "UK". Accept both.
UK_KEYS = ("GB", "UK")


def _nonempty_lines(s: str) -> list[str]:
    return [ln for ln in s.splitlines() if ln.strip()]


def landing_domain(captions: list[str]) -> str | None:
    for cap in captions:
        m = DOMAIN_RE.match(cap.strip())
        if m:
            return m.group(1).lower()
    return None


def unique_variants(values: list[str]) -> list[dict]:
    """Collapse identical texts (whitespace-insensitive), first-seen order, with a repeat count."""
    out: list[dict] = []
    index: dict[str, dict] = {}
    for v in values:
        key = " ".join(v.split())
        if not key:
            continue
        if key in index:
            index[key]["count"] += 1
        else:
            index[key] = {"text": v.strip(), "count": 1}
            out.append(index[key])
    return out


def location_reach(total_reach_by_location) -> dict[str, int]:
    """total_reach_by_location (list<KeyValue:string,int32>) -> {"UK": n, "EU": n, ...}.

    Accepts [{"key": k, "value": v}], a plain {k: v} mapping, and
    [{"location"|"country": k, "reach"|"value": v}] defensively.
    """
    items = total_reach_by_location
    if isinstance(items, dict):
        items = [{"key": k, "value": v} for k, v in items.items()]
    out: dict[str, int] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        key = item.get("key", item.get("location", item.get("country")))
        value = item.get("value", item.get("reach"))
        try:
            out[str(key).upper()] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def breakdown_rows(breakdown) -> list[dict]:
    """age_country_gender_reach_breakdown -> flat rows {country, age_range, male, female, unknown}."""
    rows = []
    for c in breakdown if isinstance(breakdown, list) else []:
        if not isinstance(c, dict):
            continue
        for b in c.get("age_gender_breakdowns") or []:
            if not isinstance(b, dict):
                continue
            rows.append({"country": str(c.get("country") or "?").upper(),
                         "age_range": str(b.get("age_range") or "?"),
                         **{g: int(b.get(g) or 0) for g in ("male", "female", "unknown")}})
    return rows


def reach_by_country(breakdown) -> dict[str, int]:
    """Sum of the age x gender breakdown per country (a lower bound, labelled as such in reports)."""
    out: dict[str, int] = {}
    for r in breakdown_rows(breakdown):
        out[r["country"]] = out.get(r["country"], 0) + r["male"] + r["female"] + r["unknown"]
    return out


def uk_reach(eu_metrics: dict | None) -> dict | None:
    """UK reach from total_reach_by_location, else the GB breakdown sum; None when neither exists."""
    eu_metrics = eu_metrics or {}
    loc = location_reach(eu_metrics.get("total_reach_by_location"))
    for k in UK_KEYS:
        if k in loc:
            return {"value": loc[k], "basis": "total_reach_by_location"}
    by_country = reach_by_country(eu_metrics.get("age_country_gender_reach_breakdown"))
    if "GB" in by_country:
        return {"value": by_country["GB"], "basis": "breakdown_sum"}
    return None


def compute(text: dict, eu_metrics: dict | None = None) -> dict:
    bodies = [b for b in text.get("bodies", []) if b is not None]
    all_text = "\n".join(bodies + text.get("link_titles", []) + text.get("link_descriptions", []))
    first = next((ln.strip() for b in bodies for ln in _nonempty_lines(b)), None)
    eu_metrics = eu_metrics or {}
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
        # creative-only: no primary text at all (catalog / video-only ads). Visuals are the only content.
        "text_status": "has_text" if any(b.strip() for b in bodies) else "creative-only",
        "variants_unique": {f: unique_variants(text.get(f, [])) for f in TEXT_FIELDS},
        "uk_reach": uk_reach(eu_metrics),
        "reach_by_country": reach_by_country(eu_metrics.get("age_country_gender_reach_breakdown")),
    }


def upgrade_ad(ad: dict) -> dict:
    """Recompute derived{} for an ad normalized under an older schema (keeps near_duplicate_of)."""
    if ad.get("schema_version") != SCHEMA_VERSION:
        near = (ad.get("derived") or {}).get("near_duplicate_of")
        ad["derived"] = {**compute(ad["text"], ad.get("eu_metrics")), "near_duplicate_of": near}
        ad["schema_version"] = SCHEMA_VERSION
    return ad


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
        ad["derived"] = compute(ad["text"], ad.get("eu_metrics"))
        ad["schema_version"] = SCHEMA_VERSION
    assign_near_duplicates(ads)
    for f, ad in zip(files, ads):
        write_json(f, ad)
    print(f"recomputed metrics for {len(ads)} ads in {argv[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
