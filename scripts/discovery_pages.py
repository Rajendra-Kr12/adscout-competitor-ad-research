#!/usr/bin/env python3
"""Group discovery search results by advertiser Page, classify each Page, rank the candidates.

Discovery step 3 (skill meta-keyword-discovery). No API calls: reads data/discovery/<name>/<run>.jsonl from
scripts/discover_keywords.py (lean records: Page, dates, headlines, link captions).

Every Page gets one class, by the first rule that applies. The full ordered list (14 rules) is in
.claude/skills/meta-keyword-discovery/references/classification.md; patterns and thresholds are in
config/discovery.yaml `pages`, overridable with --config. In short:
  known                    already tracked (project brands, competitors.yaml, --exclude-page): not a candidate
  drop:clinic              clinic, injectables or B2B supplier (name, domain or headline)
  drop:off-category        non-skincare (name or domain) or a non-UK/EU market domain or name
  brand                    brand owner: the Page name contains a known brand (wherever it links)
  drop:advertorial         persona, "expert", fake-blog, review or interview funnel (its brand is still logged)
  channel                  retailer, marketplace, distributor, reseller or creator whose ads mostly name ONE other
                           brand (brand share >= brand_share_min): kept, tagged with the brand it carries
  brand                    brand owner: most of its ads name the Page's own brand (own-brand share), or the
                           landing domain matches the Page name
  drop:multi-brand-retail  retailer or marketplace without a single brand in its ads (price-and-promo ads)
  drop:reseller-funnel     landing domain unlike the Page name, no single brand, mostly promo wording
  drop:unverified          landing domain unlike the Page name, no single brand and no promo signal
"Brand share": among a Page's ads with a headline or caption, the share that name the same brand from the
lexicon (config known_brands + the project's brands + competitors.yaml + names of brand-owner Pages in this
sample). A link caption that is a brand's own shop domain counts as naming that brand. Dropped Pages stay in
the output with their reason, ad count and carried brand, so a human can overrule any of them.

Usage:
  python scripts/discovery_pages.py --name <run-name> [--run F] [--project <slug>] [--exclude-page ID]
      [--config F] [--top 25] [--out F] [--tables-md F]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from common import DATA_DIR, assert_no_token, load_competitors, load_discovery_config, slug, write_json
import project as projects

DOMAIN_RE = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})")
NAME_NOISE = re.compile(r"\b(uk|official|global|shop|store|ltd|limited|skin ?care|beauty|cosmetics|sale|london)\b", re.I)
KEEP = ("brand", "channel")


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def domain_label(domain: str) -> str:
    """'theayurvedaexperience.co.uk' -> 'theayurvedaexperience'; 'trykivora.co' -> 'kivora'."""
    parts = domain.split(".")
    core = parts[-3] if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") else parts[-2] if len(parts) >= 2 else domain
    return re.sub(r"^(try|shop|get|go|my)", "", core.replace("-", ""))


def brand_like(page_name: str, domains: list[str]) -> bool:
    name = compact(page_name)
    for d in domains:
        label = domain_label(d)
        if len(label) >= 3 and name and (label in name or name[:6] in label):
            return True
    return False


def brand_core(page_name: str) -> str:
    """'MooGoo Skin Care UK' -> 'moogoo': the Page name without channel and market noise."""
    return compact(NAME_NOISE.sub(" ", page_name or ""))


def aggregate(records: list[dict]) -> list[dict]:
    ads: dict[str, dict] = {}
    for r in records:  # one ad can match several keywords and windows
        a = ads.setdefault(r["id"], dict(r, keywords=set(), windows=set()))
        a["keywords"].add(r["keyword"])
        a["windows"].add(r.get("window"))
    pages: dict[str, dict] = defaultdict(lambda: {"ads": [], "keywords": Counter(), "windows": set(),
                                                  "domains": Counter(), "name": None})
    for a in ads.values():
        p = pages[str(a["page_id"])]
        p["name"] = a.get("page_name")
        p["ads"].append(a)
        p["keywords"].update(a["keywords"])
        p["windows"] |= a["windows"]
        for cap in a.get("ad_creative_link_captions") or []:
            if m := DOMAIN_RE.match((cap or "").strip().lower()):
                p["domains"][m.group(1)] += 1
    rows = []
    for pid, p in pages.items():
        starts = [a["ad_delivery_start_time"][:10] for a in p["ads"] if a.get("ad_delivery_start_time")]
        rows.append({
            "page_id": pid, "page_name": p["name"], "ads": len(p["ads"]),
            "live_ads": sum(1 for a in p["ads"] if not a.get("ad_delivery_stop_time")),
            "keywords_matched": len(p["keywords"]), "keywords": dict(p["keywords"]), "windows_matched": len(p["windows"]),
            "top_domains": [d for d, _ in p["domains"].most_common(3)],
            "first_start": min(starts) if starts else None, "last_start": max(starts) if starts else None,
            "texts": [" ".join((a.get("ad_creative_link_titles") or []) + (a.get("ad_creative_link_captions") or []))
                      for a in p["ads"]],
            "page_ads_link": "https://www.facebook.com/ads/library/?active_status=all&ad_type=all&view_all_page_id=" + pid,
        })
    return rows


def _rx(patterns) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE)


def build_lexicon(rows: list[dict], cfg: dict, extra_brands: list[str]) -> dict[str, str]:
    """{compact form: display name}: config brands, project/competitor brands and this sample's brand owners."""
    lex = {compact(b): b for b in list(cfg["known_brands"]) + list(extra_brands) if len(compact(b)) >= 3}
    min_chars = cfg["thresholds"]["lexicon_min_chars"]
    generic = {compact(w) for w in cfg.get("lexicon_stopwords") or []}
    for r in rows:
        core = brand_core(r["page_name"])
        if len(core) >= min_chars and core not in generic and brand_like(r["page_name"], r["top_domains"]):
            lex.setdefault(core, re.sub(r"\s+", " ", NAME_NOISE.sub(" ", r["page_name"])).strip())
    return lex


def carried_brand(texts: list[str], lexicon: dict[str, str], own: str) -> tuple[str | None, float, int]:
    """The brand most of a Page's ads name (other than the Page itself), its share and the ads with text."""
    with_text = [compact(t) for t in texts if t.strip()]
    if not with_text:
        return None, 0.0, 0
    count = Counter()
    for t in with_text:
        named = {k for k in lexicon if k in t and k != own}
        count.update(named)
    if not count:
        return None, 0.0, len(with_text)
    key, n = max(count.items(), key=lambda kv: (kv[1], len(kv[0])))
    return lexicon[key], round(n / len(with_text), 3), len(with_text)


def classify(rows: list[dict], known: set[str], cfg: dict, extra_brands: list[str] = ()) -> list[dict]:
    th = cfg["thresholds"]
    rx = {k: _rx(cfg[k]) for k in ("marketplace_patterns", "persona_patterns", "clinic_patterns",
                                     "non_category_patterns", "non_uk_patterns", "promo_patterns")}
    lexicon = build_lexicon(rows, cfg, list(extra_brands))
    known_brand_keys = {compact(b) for b in cfg["known_brands"]}
    established = {compact(b) for b in list(cfg["known_brands"]) + list(extra_brands)}
    for r in rows:
        name, doms, texts = r["page_name"] or "", r["top_domains"], r.pop("texts")
        core = brand_core(name)
        brand, share, n_text = carried_brand(texts, lexicon, core)
        promo = round(sum(1 for t in texts if t.strip() and rx["promo_patterns"].search(t)) / n_text, 3) if n_text else 0.0
        own_name_brand = next((lexicon[k] for k in known_brand_keys if k and k in compact(name)), None)
        own_share = round(sum(1 for t in texts if t.strip() and len(core) >= 4 and core in compact(t)) / n_text, 3) if n_text else 0.0
        is_retailer = bool(rx["marketplace_patterns"].search(name))  # the Page IS a retailer or marketplace
        via_market = any(rx["marketplace_patterns"].search(d) for d in doms)  # the Page links to one
        carried_key = compact(brand) if brand else ""
        same_brand = bool(carried_key and core and (carried_key in core or core in carried_key))
        r.update(carried_brand=brand, brand_share=share, own_brand_share=own_share, promo_share=promo, ads_with_text=n_text)
        hay = [name] + doms
        if r["page_id"] in known:
            cls, why = "known", "already tracked in this project or competitors.yaml"
        elif any(rx["clinic_patterns"].search(x) for x in hay + texts):
            cls, why = "drop:clinic", "clinic, injectables or B2B supplier: a different audience"
        elif any(rx["non_category_patterns"].search(x) for x in hay):
            cls, why = "drop:off-category", "outside skincare"
        elif any(rx["non_uk_patterns"].search(x) for x in hay):
            cls, why = "drop:off-category", "a non-UK/EU market site or Page"
        elif own_name_brand:
            cls = "brand"
            why = f"brand owner ({own_name_brand})" + (", selling via a marketplace" if via_market else "")
        elif rx["persona_patterns"].search(name) or any(rx["persona_patterns"].search(d) for d in doms):
            cls = "drop:advertorial"
            why = "persona, expert or fake-blog funnel" + (f" (pushes {brand})" if brand else "")
        elif same_brand and share >= th["brand_share_min"]:
            cls, why = "brand", f"brand owner: another Page of {brand}"
        elif brand and share >= th["brand_share_min"] and (carried_key in established or is_retailer):
            kind = "retailer or marketplace" if is_retailer else "distributor, reseller or creator"
            cls, why = "channel", f"{kind}; {share:.0%} of its ads name {brand}"
        elif brand and share >= th["brand_share_min"]:
            cls, why = "drop:advertorial", (f"third-party Page pushing {brand}, a brand seen only in this sample "
                                            "(advertorial or affiliate funnel)")
        elif is_retailer:  # before the own-brand test: a retailer's captions always name its own shop
            cls, why = "drop:multi-brand-retail", "multi-brand retailer or marketplace: no single brand in its ads"
        elif own_share >= th["brand_share_min"]:
            cls = "brand"
            why = f"brand owner: {own_share:.0%} of its ads name the Page's own brand" + (", selling via a marketplace" if via_market else "")
        elif via_market and not brand:
            cls, why = "brand", "brand owner selling via a marketplace (no other brand named; unverified)"
        elif brand_like(name, doms):
            cls, why = "brand", "brand owner: the landing domain matches the Page name"
        elif n_text and promo >= th["promo_share_min"]:
            cls, why = "drop:reseller-funnel", f"landing domain unlike the Page, no single brand, {promo:.0%} promo wording"
        else:
            cls, why = "drop:unverified", "landing domain unlike the Page name and no single brand named"
        r["class"], r["reason"] = cls, why
        r["uk_site"] = any(d.endswith(".uk") for d in doms)
    rows.sort(key=lambda r: (r["class"] not in KEEP, -r["keywords_matched"], -r["windows_matched"], -r["ads"], r["page_id"]))
    return rows


def channel_vs_brand(rows: list[dict]) -> list[dict]:
    """Brands seen both on their own Page and through channel Pages: the angle 'who speaks for the brand'."""
    own = {compact(r["carried_brand"] or brand_core(r["page_name"])): r for r in rows if r["class"] == "brand"}
    by_brand: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["carried_brand"] and r["class"] != "brand":
            by_brand[compact(r["carried_brand"])].append(r)
    out = []
    for key, chans in by_brand.items():
        o = next((r for k, r in own.items() if k and (k in key or key in k)), None)
        out.append({"brand": chans[0]["carried_brand"], "own_page": o["page_name"] if o else None,
                    "own_ads": o["ads"] if o else 0, "own_promo_share": o["promo_share"] if o else None,
                    "channel_pages": len(chans), "channel_ads": sum(c["ads"] for c in chans),
                    "channel_promo_share": round(sum(c["promo_share"] * c["ads"] for c in chans) / max(1, sum(c["ads"] for c in chans)), 3),
                    "channels": [f"{c['page_name']} ({c['class']})" for c in chans]})
    out.sort(key=lambda x: -(x["own_ads"] + x["channel_ads"]))
    return out


def _cell(v) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "/").replace("\n", " ")


def tables_md(result: dict, top: int) -> str:
    """The report's deterministic tables: candidates, channel vs brand, and every dropped Page with its reason."""
    rows = result["rows"]
    keep = [r for r in rows if r["class"] in KEEP]
    dropped = sorted((r for r in rows if r["class"].startswith("drop:")), key=lambda r: (-r["ads"], r["page_id"]))
    L = ["### Candidates (brand owners and brand-led channels)", "",
         f"Top {min(top, len(keep))} of {len(keep)} kept Pages by keywords matched, windows matched, then ads; every "
         "channel is listed even below the cut.", "",
         "| Class | Page | Page ID | Ads (live) | Keywords | Carries brand | UK site | Why kept |", "|---|---|---|---|---|---|---|---|"]
    shown = keep[:top] + [r for r in keep[top:] if r["class"] == "channel"]
    for r in shown:
        L.append(f"| {r['class']} | {_cell(r['page_name'])} | {r['page_id']} | {r['ads']} ({r['live_ads']}) | "
                 f"{r['keywords_matched']} | {_cell(r['carried_brand'])} | {'yes' if r['uk_site'] else '-'} | {_cell(r['reason'])} |")
    L += ["", "### Channel versus brand messaging", "",
          "Brands that reach people both through their own Page and through other Pages (retailers, distributors, "
          "creators or funnels). Promo share is the share of ads with generic promo wording.", "",
          "| Brand | Own Page | Own ads | Own promo share | Other Pages | Their ads | Their promo share | Pages (class) |",
          "|---|---|---|---|---|---|---|---|"]
    for a in result["channel_vs_brand"]:
        own_p = f"{a['own_promo_share']:.0%}" if a["own_promo_share"] is not None else "-"
        L.append(f"| {_cell(a['brand'])} | {_cell(a['own_page'])} | {a['own_ads']} | {own_p} | {a['channel_pages']} | "
                 f"{a['channel_ads']} | {a['channel_promo_share']:.0%} | {_cell('; '.join(a['channels']))} |")
    by_reason = Counter(r["class"] for r in dropped)
    L += ["", "### Dropped and why", "",
          "Every dropped Page, most ads first, so any of them can be overruled. Totals: "
          + "; ".join(f"{c} {n} Page(s), {sum(r['ads'] for r in dropped if r['class'] == c)} ads"
                      for c, n in by_reason.most_common()) + ".", "",
          "| Page | Page ID | Class | Reason | Ads | Carries brand | Top domain |", "|---|---|---|---|---|---|---|"]
    for r in dropped:
        L.append(f"| {_cell(r['page_name'])} | {r['page_id']} | {r['class']} | {_cell(r['reason'])} | {r['ads']} | "
                 f"{_cell(r['carried_brand'])} | {_cell((r['top_domains'] or [None])[0])} |")
    known = [r for r in rows if r["class"] == "known"]
    if known:
        L += ["", "Already tracked (not candidates): " + ", ".join(f"{r['page_name']} ({r['ads']} ads)" for r in known) + "."]
    return "\n".join(L) + "\n"


def known_pages(project: str | None, projects_dir: Path, extra: list[str]) -> tuple[set[str], list[str]]:
    comps = load_competitors().values()
    ids = {str(c["page_id"]) for c in comps if c.get("page_id")}
    names = [c["name"] for c in comps if c.get("name")]
    if project:
        brands = projects.load_project(project, projects_dir)["brands"]
        ids |= {str(p) for b in brands for p in b.get("page_ids") or []}
        names += [b["name"] for b in brands]
    return ids | {str(p) for p in extra}, names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", required=True, help="discovery run name (data/discovery/<name>/)")
    ap.add_argument("--run", type=Path, help="a specific records file (default: every run under the name)")
    ap.add_argument("--project", help="project slug: its brands' Pages are known, its brand names join the lexicon")
    ap.add_argument("--exclude-page", action="append", default=[])
    ap.add_argument("--config", type=Path, help="discovery config with other criteria (default config/discovery.yaml)")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out", type=Path, help="write every Page with its class and reason as JSON")
    ap.add_argument("--tables-md", type=Path, help="write the report tables (candidates, channel vs brand, dropped and why)")
    ap.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)

    files = [args.run] if args.run else sorted((args.data_dir / "discovery" / slug(args.name)).glob("*.jsonl"))
    if not files:
        sys.exit(f"no discovery records under {args.data_dir / 'discovery' / slug(args.name)}")
    records = [json.loads(line) for f in files for line in f.read_text().splitlines() if line.strip()]
    cfg = (load_discovery_config(args.config) if args.config else load_discovery_config())["pages"]
    known, names = known_pages(args.project, args.projects_dir, args.exclude_page)
    rows = classify(aggregate(records), known, cfg, names)
    classes = Counter(r["class"] for r in rows)
    result = {"records": len(records), "pages": len(rows), "classes": dict(classes),
              "ads_by_class": {c: sum(r["ads"] for r in rows if r["class"] == c) for c in classes},
              "thresholds": cfg["thresholds"], "channel_vs_brand": channel_vs_brand(rows), "rows": rows}
    if args.out:
        write_json(args.out, result)
    if args.tables_md:
        text = tables_md(result, args.top)
        assert_no_token(text, str(args.tables_md))
        args.tables_md.parent.mkdir(parents=True, exist_ok=True)
        args.tables_md.write_text(text)
    print(f"{len(records)} record(s), {len(rows)} Page(s); classes {json.dumps(dict(classes))}")
    print("class    kw  win  ads  live  uk  page_id            page_name / carries / top domain")
    for r in [r for r in rows if r["class"] in KEEP][:args.top]:
        print(f"{r['class']:<7}  {r['keywords_matched']:>2}  {r['windows_matched']:>3}  {r['ads']:>3}  {r['live_ads']:>4}  "
              f"{'uk' if r['uk_site'] else '--'}  {r['page_id']:<17}  {r['page_name']} / {r['carried_brand'] or '-'} / "
              f"{(r['top_domains'] or ['-'])[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
