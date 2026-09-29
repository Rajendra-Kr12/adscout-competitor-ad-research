#!/usr/bin/env python3
"""Aggregate normalized ads by month into reports/<target>/<date>-competitor-report.md.

Deterministic: every number comes from data/ads/<target>/*.json. The report-writer
sub-agent later fills the "Analyst notes" block; it must not change the numbers.

Usage:
  python scripts/build_report.py --target forest-essentials-uk [--since 2025-10 --until 2026-09]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from common import (
    DATA_DIR,
    REPORTS_DIR,
    assert_no_token,
    load_competitors,
    months_between,
    parse_month,
    utc_now,
)

NARRATIVE_START = "<!-- report-writer:start -->"
NARRATIVE_END = "<!-- report-writer:end -->"

BASIS_TEXT = {
    "eu_uk_delivery": "commercial ads delivered to the EU/UK in the past year (ads_archive coverage rule)",
    "political_issue": "social-issue, electoral or political ads (worldwide, 7 years)",
    "mcp_library_search": "Meta Ads MCP ads_library_search (coverage undocumented)",
    "fixture": "SYNTHETIC FIXTURE DATA - not real ads, for offline testing only",
}


def cell(value, width: int | None = None) -> str:
    s = "" if value is None else str(value)
    s = " ".join(s.split()).replace("|", "\\|")
    if width and len(s) > width:
        s = s[: width - 1].rstrip() + "…"
    return s


def load(data_dir: Path, target: str) -> list[dict]:
    ad_dir = data_dir / "ads" / target
    return [json.loads(f.read_text()) for f in sorted(ad_dir.glob("*.json"))] if ad_dir.exists() else []


def in_window(ad: dict, months: set[str]) -> bool:
    return bool(set(ad["dates"]["active_months"]) & months) or (
        not ad["dates"]["active_months"] and ad["dates"]["start_month"] in months)


def pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.0f}%" if d else "n/a"


def median(values: list) -> str:
    return f"{statistics.median(values):g}" if values else "n/a"


def dist(counter: Counter, total: int, top: int = 10) -> str:
    if not counter:
        return "_none_"
    return ", ".join(f"{k} {v} ({pct(v, total)})" for k, v in counter.most_common(top))


def build(ads_all: list[dict], target: str, since: str | None, until: str | None,
          competitor: dict | None, today: date) -> str:
    starts = sorted(a["dates"]["start_month"] for a in ads_all if a["dates"]["start_month"])
    since = since or (starts[0] if starts else today.strftime("%Y-%m"))
    until = until or today.strftime("%Y-%m")
    months = months_between(parse_month(since), parse_month(until))
    ads = [a for a in ads_all if in_window(a, set(months))]
    ads.sort(key=lambda a: (a["dates"]["delivery_start"] or "", a["library_id"]))
    n = len(ads)
    name = (competitor or {}).get("name") or target
    pages = Counter(f"{a['page']['name']} ({a['page']['id']})" for a in ads)
    basis = Counter(a["coverage_basis"] for a in ads)
    runs = sorted({a["provenance"]["run_id"] for a in ads})
    api_versions = sorted({a["provenance"]["api_version"] or "?" for a in ads})
    with_llm = [a for a in ads if a.get("llm")]
    L: list[str] = []
    w = L.append

    w(f"# Competitor ad report: {name}")
    w("")
    w(f"- Target: `{target}`" + (f" - countries queried: {', '.join(competitor.get('countries') or [])}"
                                 if competitor else ""))
    w(f"- Window: {since} to {until} ({len(months)} months)")
    w(f"- Generated: {today.isoformat()} from {len(runs)} fetch run(s): {', '.join(runs) or 'none'}; "
      f"API {', '.join(api_versions) or 'n/a'}")
    w(f"- Pages: {', '.join(pages) or 'none'}")
    w("")

    w("## Coverage basis and caveats")
    w("")
    if "fixture" in basis:
        w("> **SYNTHETIC DATA.** This report was built from test fixtures, not from Meta. "
          "Nothing below describes a real advertiser.")
        w("")
    for b, c in basis.most_common():
        w(f"- {c} ad(s): {BASIS_TEXT.get(b, b)}")
    if competitor and competitor.get("coverage_note"):
        w(f"- Expected coverage for this target: {cell(competitor['coverage_note'])}")
    w("- The official Ad Library API (`ads_archive`) returns commercial ads **only if they were "
      "delivered to the EU or UK** (past year). Ads shown only in other countries (for example "
      "India-only campaigns) are missing here even when the Ad Library website shows them.")
    w("- Competitor spend, impressions, CPM, CPA, ROAS and engagement are **not available** from "
      "any official source. Reach appears only for EU-delivered ads.")
    w("- \"Days running\" counts calendar days from first impression to stop (or to the fetch date "
      "for active ads). Long runs often indicate winners, but that is an inference, not a metric.")
    w("- Images, video and on-image text are not analysed yet (Phase 1). Text analysis uses ad "
      "copy, headlines, descriptions and captions only.")
    w(f"- LLM extraction coverage: {len(with_llm)}/{n} ads"
      + (f" (prompt {', '.join(sorted({a['provenance']['prompt_version'] or '?' for a in with_llm}))})"
         if with_llm else "") + ".")
    w("")
    if n == 0:
        w("**No ads were returned for this window.** Given the coverage rule above this is expected "
          "for advertisers who do not deliver to the EU/UK; it does not prove the advertiser ran no ads.")
        w("")

    w("## Summary")
    w("")
    active = sum(1 for a in ads if a["dates"]["active_at_fetch"])
    days = [a["dates"]["days_running"] for a in ads if a["dates"]["days_running"] is not None]
    dups = sum(1 for a in ads if a["derived"]["near_duplicate_of"])
    w("| Metric | Value |")
    w("|---|---|")
    w(f"| Ads in window | {n} |")
    w(f"| Active at fetch | {active} ({pct(active, n)}) |")
    w(f"| Median days running | {median(days)} |")
    w(f"| Near-duplicate copies (same text as an earlier ad) | {dups} |")
    w(f"| Platforms | {dist(Counter(p for a in ads for p in a['platforms']), n)} |")
    w(f"| Languages | {dist(Counter(l for a in ads for l in a['languages']), n)} |")
    w(f"| Media type (only when queried per type) | {dist(Counter(a['media_type_hint'] for a in ads if a['media_type_hint']), n)} |")
    reach = [a["eu_metrics"]["eu_total_reach"] for a in ads
             if a["eu_metrics"] and a["eu_metrics"].get("eu_total_reach") is not None]
    if reach:
        w(f"| EU total reach (sum / median, {len(reach)} ads) | {sum(reach):,} / {statistics.median(reach):,.0f} |")
    w("")

    w("## Ads per month")
    w("")
    w("| Month | New ads | Active ads | Median words (new) | With % off (new) |")
    w("|---|---|---|---|---|")
    for m in months:
        new = [a for a in ads if a["dates"]["start_month"] == m]
        act = [a for a in ads if m in a["dates"]["active_months"]]
        words = [x for a in new for x in a["derived"]["body_words"]]
        off = sum(1 for a in new if a["derived"]["has_percent_off"])
        w(f"| {m} | {len(new)} | {len(act)} | {median(words)} | {off} |")
    w("")

    w("## Monthly ledger (by first-delivery month)")
    w("")
    for m in months:
        new = [a for a in ads if a["dates"]["start_month"] == m]
        if not new:
            continue
        w(f"### {m} ({len(new)} new)")
        w("")
        w("| Ad | Start | Stop | Days | Platforms | Variants | First line | Hook | Offer |")
        w("|---|---|---|---|---|---|---|---|---|")
        for a in new:
            llm = a.get("llm") or {}
            offer = (llm.get("offer") or {})
            offer_s = "" if not offer or offer.get("type") == "none" else f"{offer.get('type')}: {offer.get('text') or ''}"
            w(f"| [{a['library_id']}]({a['permalink']}) | {cell((a['dates']['delivery_start'] or '')[:10])} "
              f"| {cell((a['dates']['delivery_stop'] or 'active')[:10])} | {cell(a['dates']['days_running'])} "
              f"| {cell(', '.join(p.replace('_', ' ').title() for p in a['platforms']))} | {a['derived']['variant_count']} "
              f"| {cell(a['derived']['first_line'], 80)} | {cell(llm.get('hook'), 80)} | {cell(offer_s, 60)} |")
        w("")
    # Ads that started before the window but were active in it.
    carried = [a for a in ads if a["dates"]["start_month"] not in months]
    if carried:
        w(f"### Started before {since}, active in window ({len(carried)})")
        w("")
        for a in carried:
            w(f"- [{a['library_id']}]({a['permalink']}) since {cell((a['dates']['delivery_start'] or '')[:10])}, "
              f"{cell(a['dates']['days_running'])} days - {cell(a['derived']['first_line'], 80)}")
        w("")

    w("## Longest-running ads")
    w("")
    longest = sorted((a for a in ads if a["dates"]["days_running"] is not None),
                     key=lambda a: -a["dates"]["days_running"])[:10]
    if longest:
        w("| Ad | Days | Active | First line |")
        w("|---|---|---|---|")
        for a in longest:
            w(f"| [{a['library_id']}]({a['permalink']}) | {a['dates']['days_running']} "
              f"| {'yes' if a['dates']['active_at_fetch'] else 'no'} | {cell(a['derived']['first_line'], 90)} |")
    else:
        w("_none_")
    w("")

    w("## Length and format")
    w("")
    words = [x for a in ads for x in a["derived"]["body_words"]]
    chars = [x for a in ads for x in a["derived"]["body_chars"]]
    w("| Metric | Value |")
    w("|---|---|")
    w(f"| Bodies analysed | {len(words)} |")
    w(f"| Median words / characters per body | {median(words)} / {median(chars)} |")
    w(f"| Min-max words | {(str(min(words)) + '-' + str(max(words))) if words else 'n/a'} |")
    w(f"| Ads with multiple text variants | {sum(1 for a in ads if a['derived']['variant_count'] > 1)} |")
    w(f"| Ads with emoji / hashtags | {sum(1 for a in ads if a['derived']['emoji_count'])} / "
      f"{sum(1 for a in ads if a['derived']['hashtag_count'])} |")
    w(f"| Ads mentioning a price / a % discount | {sum(1 for a in ads if a['derived']['has_price'])} / "
      f"{sum(1 for a in ads if a['derived']['has_percent_off'])} |")
    w(f"| Landing domains | {dist(Counter(a['derived']['landing_domain'] for a in ads if a['derived']['landing_domain']), n)} |")
    w("")

    w("## Messaging (LLM extraction)")
    w("")
    if not with_llm:
        w("_No LLM extraction yet. Run the ad-extractor step of `/analyze-competitor`._")
    else:
        k = len(with_llm)
        w(f"Based on {k} ads with schema-validated extraction.")
        w("")
        w(f"- Tone: {dist(Counter(t for a in with_llm for t in a['llm']['tone']), k)}")
        w(f"- Offer types: {dist(Counter(a['llm']['offer']['type'] for a in with_llm), k)}")
        w(f"- CTAs: {dist(Counter(a['llm']['cta'] for a in with_llm if a['llm']['cta']), k)}")
        w(f"- Format guess: {dist(Counter(a['llm']['format_guess'] for a in with_llm), k)}")
        w(f"- Audience signals: {dist(Counter(s for a in with_llm for s in a['llm']['audience_signals']), k)}")
        for dim in ("clarity", "specificity", "brand_consistency"):
            vals = [a["llm"]["quality"][dim] for a in with_llm]
            w(f"- Mean {dim.replace('_', ' ')} (1-5): {statistics.mean(vals):.1f}")
        w("")
        w("| Ad | Month | Proposition | Offer |")
        w("|---|---|---|---|")
        for a in with_llm:
            o = a["llm"]["offer"]
            w(f"| [{a['library_id']}]({a['permalink']}) | {a['dates']['start_month']} "
              f"| {cell(a['llm']['proposition'], 90)} | {cell(o['text'] if o['type'] != 'none' else '', 50)} |")
    w("")

    w("## Analyst notes")
    w("")
    w(NARRATIVE_START)
    w("_Not written yet. The report-writer sub-agent fills this block (themes, tone and consistency, "
      "offer strategy, what to test) citing ads by library ID. Numbers above are authoritative._")
    w(NARRATIVE_END)
    w("")

    w("## Method")
    w("")
    w("Source: Meta Ad Library API `ads_archive` (official), `ad_active_status=ALL`, one query per "
      "calendar month, every `paging.next` followed. Links are public Ad Library permalinks "
      "(`https://www.facebook.com/ads/library/?id=<library_id>`); API snapshot URLs are never "
      "included because they embed an access token. Built by `scripts/build_report.py`.")
    w("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="normalized ads -> monthly markdown report")
    ap.add_argument("--target", required=True)
    ap.add_argument("--since", help="YYYY-MM (default: first ad month)")
    ap.add_argument("--until", help="YYYY-MM (default: current month)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    ap.add_argument("--date", help="report date YYYY-MM-DD (default: today, UTC)")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else utc_now().date()
    competitor = load_competitors().get(args.target)
    text = build(load(args.data_dir, args.target), args.target, args.since, args.until, competitor, today)
    out = args.reports_dir / args.target / f"{today.isoformat()}-competitor-report.md"
    assert_no_token(text, str(out))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        # Keep an existing analyst narrative when the numbers are rebuilt.
        old = out.read_text()
        if NARRATIVE_START in old and NARRATIVE_END in old:
            keep = old.split(NARRATIVE_START, 1)[1].split(NARRATIVE_END, 1)[0]
            head, rest = text.split(NARRATIVE_START, 1)
            text = head + NARRATIVE_START + keep + NARRATIVE_END + rest.split(NARRATIVE_END, 1)[1]
    out.write_text(text)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
