#!/usr/bin/env python3
"""Aggregate normalized ads by month into reports/<target>/<date>-competitor-report.md,
plus one row per ad in reports/<target>/<date>-ads.csv.

Deterministic: every number comes from data/ads/<target>/*.json. The report-writer
sub-agent later fills the "Analyst notes" block; it must not change the numbers.

Usage:
  python scripts/build_report.py --target forest-essentials-uk [--since 2025-10 --until 2026-09]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from common import (
    DATA_DIR,
    EU_UK,
    REPORTS_DIR,
    assert_no_token,
    load_competitors,
    months_between,
    parse_month,
    utc_now,
)
from text_metrics import breakdown_rows, location_reach, upgrade_ad

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
    if not ad_dir.exists():
        return []
    return [upgrade_ad(json.loads(f.read_text())) for f in sorted(ad_dir.glob("*.json"))]


def queried_countries(data_dir: Path, target: str, competitor: dict | None) -> list[str]:
    """Countries the latest fetch run asked for (run header), else the competitor config."""
    runs = sorted((data_dir / "raw" / target).glob("*.jsonl"))
    if runs:
        with runs[-1].open() as fh:
            header = json.loads(fh.readline() or "{}")
        countries = (header.get("target") or {}).get("countries")
        if countries:
            return [str(c).upper() for c in countries]
    return [str(c).upper() for c in (competitor or {}).get("countries") or []]


def md_quote(text: str) -> list[str]:
    """Ad copy as a markdown blockquote: line breaks kept, markdown/HTML syntax neutralised."""
    out = []
    for line in text.strip().splitlines():
        line = line.rstrip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        line = re.sub(r"^(\s*)([#+*-]|\d+[.)])", r"\1\\\2", line)
        out.append(f"> {line}\\" if line else ">")
    if out and out[-1].endswith("\\"):
        out[-1] = out[-1][:-1]
    return out or ["> _(empty)_"]


def variants_cell(variants: list[dict], width: int | None = None) -> str:
    if not variants:
        return "_none_"
    return " · ".join(cell(v["text"], width) + (f" (×{v['count']})" if v["count"] > 1 else "")
                      for v in variants)


def fmt_int(value) -> str:
    return f"{value:,}" if isinstance(value, int) else "n/a"


def eu_reach(ad: dict) -> int | None:
    eu = ad.get("eu_metrics") or {}
    if eu.get("eu_total_reach") is not None:
        return eu["eu_total_reach"]
    return location_reach(eu.get("total_reach_by_location")).get("EU")


def ages_text(ad: dict) -> str:
    ages = [str(a) for a in (ad.get("eu_metrics") or {}).get("target_ages") or []]
    return "-".join(ages) if len(ages) == 2 else ", ".join(ages)


def locations_text(ad: dict) -> str:
    locs = [l for l in (ad.get("eu_metrics") or {}).get("target_locations") or [] if isinstance(l, dict)]
    inc = [str(l.get("name")) for l in locs if not l.get("excluded")]
    exc = [str(l.get("name")) for l in locs if l.get("excluded")]
    return "; ".join(filter(None, [", ".join(inc), ("excluded " + ", ".join(exc)) if exc else ""]))


def targeting_text(ad: dict) -> str:
    gender = (ad.get("eu_metrics") or {}).get("target_gender")
    parts = [f"ages {ages_text(ad)}" if ages_text(ad) else "", f"gender {gender}" if gender else "",
             f"locations {locations_text(ad)}" if locations_text(ad) else ""]
    return "; ".join(p for p in parts if p)


def payers_text(ad: dict) -> str:
    out = []
    for bp in (ad.get("eu_metrics") or {}).get("beneficiary_payers") or []:
        if isinstance(bp, dict):
            out.append(f"beneficiary {bp.get('beneficiary') or '?'}, payer {bp.get('payer') or '?'}")
    return "; ".join(out)


def platforms_text(ad: dict) -> str:
    return ", ".join(p.replace("_", " ").title() for p in ad["platforms"])


def ad_card(a: dict) -> list[str]:
    d, der, llm = a["dates"], a["derived"], a.get("llm") or {}
    status = "active" if d["active_at_fetch"] else "stopped"
    L = [f"### {a['library_id']} - {(d['delivery_start'] or '?')[:10]} - {status}", "",
         f"[Open in the Ad Library]({a['permalink']})", "",
         "| Field | Value |", "|---|---|",
         f"| Page | {cell(a['page']['name'])} ({cell(a['page']['id'])}) |",
         f"| Delivery | {cell((d['delivery_start'] or '?')[:10])} to "
         f"{cell((d['delivery_stop'] or 'active')[:10])} ({cell(d['days_running'])} days; "
         f"created {cell((d['created'] or '?')[:10])}) |",
         f"| Platforms | {cell(platforms_text(a)) or '_none_'} |",
         f"| Languages | {cell(', '.join(a['languages'])) or '_none_'} |",
         f"| Media type | {cell(a['media_type_hint']) or '_not queried per type_'} |",
         f"| Text | {TEXT_STATUS.get(der['text_status'], der['text_status'])} |",
         f"| Headline | {variants_cell(der['variants_unique']['link_titles'])} |",
         f"| Link description | {variants_cell(der['variants_unique']['link_descriptions'])} |",
         f"| Caption / display link | {variants_cell(der['variants_unique']['link_captions'])} |"]
    uk = der.get("uk_reach")
    L.append(f"| UK reach | {fmt_int(uk['value']) + (' (sum of the GB age x gender breakdown)' if uk['basis'] == 'breakdown_sum' else '') if uk else 'n/a'} |")
    L.append(f"| EU reach | {fmt_int(eu_reach(a))} |")
    if der.get("reach_by_country"):
        L.append("| Reach by country (breakdown sum) | "
                 + ", ".join(f"{c} {v:,}" for c, v in sorted(der["reach_by_country"].items())) + " |")
    if targeting_text(a):
        L.append(f"| Targeting | {cell(targeting_text(a))} |")
    if payers_text(a):
        L.append(f"| Beneficiary / payer | {cell(payers_text(a))} |")
    if der["near_duplicate_of"]:
        L.append(f"| Near-duplicate of | {der['near_duplicate_of']} |")
    if llm:
        offer = llm.get("offer") or {}
        L.append(f"| Hook (LLM) | {cell(llm.get('hook'))} |")
        if offer.get("type") not in (None, "none"):
            L.append(f"| Offer (LLM) | {cell(offer.get('type'))}: {cell(offer.get('text'))}"
                     + (f" (until {cell(offer['deadline'])})" if offer.get("deadline") else "") + " |")
        if llm.get("cta"):
            L.append(f"| CTA in the copy (LLM) | {cell(llm['cta'])} |")
    L.append("")
    bodies = der["variants_unique"]["bodies"]
    if bodies:
        total = sum(v["count"] for v in bodies)
        if len(bodies) == 1:
            L.append("**Primary text**" + (f" (returned ×{total}, identical):" if total > 1 else ":"))
        else:
            L.append(f"**Primary text** ({len(bodies)} unique variants of {total}):")
        L.append("")
        for i, v in enumerate(bodies, 1):
            if len(bodies) > 1:
                L.append(f"_Variant {i}" + (f", returned ×{v['count']}" if v["count"] > 1 else "") + "_")
                L.append("")
            L += md_quote(v["text"])
            L.append("")
    rows = breakdown_rows((a.get("eu_metrics") or {}).get("age_country_gender_reach_breakdown"))
    if rows:
        L += ["Reach by age and gender:", "",
              "| Country | Age | Male | Female | Unknown |", "|---|---|---|---|---|"]
        L += [f"| {r['country']} | {cell(r['age_range'])} | {r['male']:,} | {r['female']:,} | {r['unknown']:,} |"
              for r in rows]
        L.append("")
    return L


CSV_COLUMNS = [
    "brand", "page_name", "page_id", "library_id", "permalink", "status", "start", "stop", "days_running",
    "platforms", "languages", "media_type", "text_status", "primary_text", "headlines", "descriptions",
    "captions", "landing_domain", "hook", "proposition", "offer_type", "offer_text", "offer_deadline",
    "cta_copy", "tone", "audience_signals", "clarity", "specificity", "brand_consistency", "uk_reach",
    "uk_reach_basis", "eu_reach", "reach_by_country", "target_ages", "target_gender", "target_locations",
    "beneficiary_payers", "near_duplicate_of", "fetched_at", "run_id",
]


def _joined(variants: list[dict], sep: str) -> str:
    return sep.join((f"[×{v['count']}] " if v["count"] > 1 else "") + v["text"] for v in variants)


def csv_safe(value) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # no spreadsheet formulas


def ad_row(a: dict, brand: str) -> dict:
    """One ad as a flat row (CSV_COLUMNS keys): full text, LLM fields, reach, targeting."""
    d, der, llm = a["dates"], a["derived"], a.get("llm") or {}
    vu, offer, q = der["variants_unique"], (llm.get("offer") or {}), (llm.get("quality") or {})
    uk = der.get("uk_reach") or {}
    eu = a.get("eu_metrics") or {}
    return {
        "brand": brand, "page_name": a["page"]["name"], "page_id": a["page"]["id"],
        "library_id": a["library_id"], "permalink": a["permalink"],
        "status": "active" if d["active_at_fetch"] else ("unknown" if d["active_at_fetch"] is None else "stopped"),
        "start": (d["delivery_start"] or "")[:10], "stop": (d["delivery_stop"] or "")[:10],
        "days_running": d["days_running"], "platforms": ", ".join(a["platforms"]),
        "languages": ", ".join(a["languages"]), "media_type": a["media_type_hint"],
        "text_status": der["text_status"], "primary_text": _joined(vu["bodies"], "\n---\n"),
        "headlines": _joined(vu["link_titles"], " | "),
        "descriptions": _joined(vu["link_descriptions"], " | "),
        "captions": _joined(vu["link_captions"], " | "), "landing_domain": der["landing_domain"],
        "hook": llm.get("hook"), "proposition": llm.get("proposition"),
        "offer_type": offer.get("type"), "offer_text": offer.get("text"),
        "offer_deadline": offer.get("deadline"), "cta_copy": llm.get("cta"),
        "tone": ", ".join(llm.get("tone") or []),
        "audience_signals": ", ".join(llm.get("audience_signals") or []),
        "clarity": q.get("clarity"), "specificity": q.get("specificity"),
        "brand_consistency": q.get("brand_consistency"),
        "uk_reach": uk.get("value"), "uk_reach_basis": uk.get("basis"), "eu_reach": eu_reach(a),
        "reach_by_country": ", ".join(f"{c} {v}" for c, v in sorted(der["reach_by_country"].items())),
        "target_ages": ages_text(a), "target_gender": eu.get("target_gender"),
        "target_locations": locations_text(a),
        "beneficiary_payers": payers_text(a), "near_duplicate_of": der["near_duplicate_of"],
        "fetched_at": a["provenance"]["fetched_at"], "run_id": a["provenance"]["run_id"],
    }


def ads_csv(ads: list[dict], brand: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for a in ads:
        row = ad_row(a, brand)
        w.writerow([csv_safe(row[c]) for c in CSV_COLUMNS])
    return buf.getvalue()


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


def window_ads(ads_all: list[dict], since: str, until: str) -> list[dict]:
    months = set(months_between(parse_month(since), parse_month(until)))
    ads = [a for a in ads_all if in_window(a, months)]
    ads.sort(key=lambda a: (a["dates"]["delivery_start"] or "", a["library_id"]))
    return ads


def default_window(ads_all: list[dict], since: str | None, until: str | None, today: date) -> tuple[str, str]:
    starts = sorted(a["dates"]["start_month"] for a in ads_all if a["dates"]["start_month"])
    return since or (starts[0] if starts else today.strftime("%Y-%m")), until or today.strftime("%Y-%m")


TEXT_STATUS = {
    "has_text": "has text",
    "creative-only": "creative-only (no primary text; the visual carries the ad)",
    "headline-only": "headline-only (Meta Ads MCP returns no primary text)",
}


def build(ads_all: list[dict], target: str, since: str | None, until: str | None,
          competitor: dict | None, today: date, countries: list[str] | None = None,
          queried: list[str] | None = None, caveats: tuple[str, ...] = ()) -> str:
    """queried: countries actually sent to ads_archive when they differ from `countries` (project runs
    route non-EU/UK countries elsewhere); caveats: extra coverage bullets."""
    since, until = default_window(ads_all, since, until, today)
    months = months_between(parse_month(since), parse_month(until))
    ads = window_ads(ads_all, since, until)
    n = len(ads)
    countries = countries if countries is not None else [c.upper() for c in (competitor or {}).get("countries") or []]
    outside = [c for c in countries if c not in EU_UK and c != "ALL"]
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
    if queried is not None and sorted(queried) != sorted(countries):
        w(f"- Target: `{target}` - countries: {', '.join(countries)}; sent to ads_archive: "
          f"{', '.join(queried) or 'none'}")
    else:
        w(f"- Target: `{target}`" + (f" - countries queried: {', '.join(countries)}" if countries else ""))
    w(f"- Window: {since} to {until} ({len(months)} month{'s' if len(months) != 1 else ''})")
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
    for c in caveats:
        w(f"- {cell(c)}")
    w("- The official Ad Library API (`ads_archive`) returns commercial ads **only if they were "
      "delivered to the EU or UK** (past year). Ads shown only in other countries (for example "
      "India-only campaigns) are missing here even when the Ad Library website shows them.")
    if outside:
        w(f"- **{', '.join(outside)} {'is' if len(outside) == 1 else 'are'} outside the EU/UK.** Meta's Ad "
          "Library API only covers commercial ads delivered to the EU/UK, so it returns no commercial ads "
          "for those countries (only social-issue, electoral or political ads). That is a coverage limit, "
          "not an error.")
    w("- Competitor spend, impressions, CPM, CPA, ROAS and engagement are **not available** from "
      "any official source. Reach, age x gender breakdowns and targeting are returned only for ads "
      "delivered to the UK or EU; EU reach only for EU-delivered ads.")
    w("- \"Days running\" counts calendar days from first impression to stop (or to the fetch date "
      "for active ads). Long runs often indicate winners, but that is an inference, not a metric.")
    w("- Images, video, on-image text and the CTA button label are not in the API and are not analysed "
      "yet. Text analysis uses ad copy, headlines, descriptions and captions only. Ads marked "
      "`creative-only` have no text at all.")
    w(f"- LLM extraction coverage: {len(with_llm)}/{n} ads"
      + (f" (prompt {', '.join(sorted({a['provenance']['prompt_version'] or '?' for a in with_llm}))})"
         if with_llm else "") + ".")
    w("")
    if n == 0:
        if queried == []:
            w(f"**No ads were fetched.** None of this brand's countries ({', '.join(countries)}) is covered by "
              "`ads_archive` for commercial ads, and the Meta Ads MCP route is not connected in this stage. "
              "That is a coverage limit, not an error, and it does not prove the advertiser ran no ads.")
        elif countries and not set(countries) & (EU_UK | {"ALL"}):
            w(f"**No ads were returned for this window.** Meta's Ad Library API only covers commercial ads "
              f"delivered to the EU/UK, and this query asked for {', '.join(countries)} only. An empty result "
              "here is the API's coverage limit, not an error, and it does not prove the advertiser ran no ads.")
        else:
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
    w(f"| Creative-only ads (no text) | {sum(1 for a in ads if a['derived']['text_status'] == 'creative-only')} |")
    uk = [a["derived"]["uk_reach"] for a in ads if a["derived"].get("uk_reach")]
    if uk:
        vals = [u["value"] for u in uk]
        from_bd = sum(1 for u in uk if u["basis"] == "breakdown_sum")
        w(f"| UK reach (sum / median, {len(uk)} ads"
          + (f"; {from_bd} from GB breakdown sums" if from_bd else "")
          + f") | {sum(vals):,} / {statistics.median(vals):,.0f} |")
    reach = [r for r in (eu_reach(a) for a in ads) if r is not None]
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
              f"| {cell(a['derived']['first_line'], 80) or '_creative-only_'} | {cell(llm.get('hook'), 80)} "
              f"| {cell(offer_s, 60)} |")
        w("")
    # Ads that started before the window but were active in it.
    carried = [a for a in ads if a["dates"]["start_month"] not in months]
    if carried:
        w(f"### Started before {since}, active in window ({len(carried)})")
        w("")
        for a in carried:
            w(f"- [{a['library_id']}]({a['permalink']}) since {cell((a['dates']['delivery_start'] or '')[:10])}, "
              f"{cell(a['dates']['days_running'])} days - {cell(a['derived']['first_line'], 80) or '_creative-only_'}")
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
              f"| {'yes' if a['dates']['active_at_fetch'] else 'no'} "
              f"| {cell(a['derived']['first_line'], 90) or '_creative-only_'} |")
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

    w("## Ad details")
    w("")
    w("Every ad in the window with its full text as returned by the API (identical variants shown once, "
      "with a count), headline, link description, caption, reach and targeting.")
    w("")
    if not ads:
        w("_none_")
        w("")
    for a in ads:
        L.extend(ad_card(a))

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
      "included because they embed an access token. One row per ad, with the full text, is in "
      "the `-ads.csv` file next to this report. Built by `scripts/build_report.py`.")
    w("")
    return "\n".join(L)


def keep_block(old: str, new: str, start: str = NARRATIVE_START, end: str = NARRATIVE_END) -> str:
    """Carry the text between start/end markers from an older file into a rebuilt one."""
    if start in old and end in old and start in new and end in new:
        keep = old.split(start, 1)[1].split(end, 1)[0]
        head, rest = new.split(start, 1)
        return head + start + keep + end + rest.split(end, 1)[1]
    return new


def write_report(out: Path, text: str, blocks: tuple[tuple[str, str], ...] = ((NARRATIVE_START, NARRATIVE_END),)) -> None:
    """Write a report, keeping hand- or agent-written marker blocks (analyst notes) from the old file."""
    assert_no_token(text, str(out))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        old = out.read_text()
        for start, end in blocks:
            text = keep_block(old, text, start, end)
        assert_no_token(text, str(out))
    out.write_text(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="normalized ads -> monthly markdown report")
    ap.add_argument("--target", required=True)
    ap.add_argument("--since", help="YYYY-MM (default: first ad month)")
    ap.add_argument("--until", help="YYYY-MM (default: current month)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    ap.add_argument("--date", help="report date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--country", action="append",
                    help="countries the fetch queried (default: from the latest raw run, else the competitor config)")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else utc_now().date()
    competitor = load_competitors().get(args.target)
    countries = ([c.upper() for c in args.country] if args.country
                 else queried_countries(args.data_dir, args.target, competitor))
    ads_all = load(args.data_dir, args.target)
    text = build(ads_all, args.target, args.since, args.until, competitor, today, countries)
    out = args.reports_dir / args.target / f"{today.isoformat()}-competitor-report.md"
    assert_no_token(text, str(out))
    since, until = default_window(ads_all, args.since, args.until, today)
    csv_text = ads_csv(window_ads(ads_all, since, until), (competitor or {}).get("name") or args.target)
    csv_out = out.with_name(f"{today.isoformat()}-ads.csv")
    assert_no_token(csv_text, str(csv_out))
    write_report(out, text)
    csv_out.write_text(csv_text)
    print(out)
    print(csv_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
