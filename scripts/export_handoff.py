#!/usr/bin/env python3
"""Write handoff/<slug>.md: a one-page research brief for a separate Instagram creative agent.

Deterministic and offline. It reads data the project already has (data/ads/<brand>/*.json, the project file,
the latest run window) and never calls Meta. `--fixture` builds the synthetic dataset in a temp dir through the
guarded fixture transport, so a demo needs no token and no live call. Every statement is tagged
Observed / Pattern / Hypothesis / Unknown. No spend, CPM, CPA or ROAS, no tokens or snapshot URLs: evidence is
`https://www.facebook.com/ads/library/?id=<library_id>` only.

Usage:
  python scripts/export_handoff.py <project-slug | brand-key> [--dry-run] [--date YYYY-MM-DD]
  python scripts/export_handoff.py synthetic --fixture tests/fixtures/ads_archive_synthetic.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

import build_report
import project as projects
from common import (CONFIG_PATH, DATA_DIR, EU_UK, ROOT, REPORTS_DIR, assert_no_token, load_competitors,
                    permalink, utc_now)

HANDOFF_DIR = ROOT / "handoff"
MAX_LINES = 70            # "max one page"
HOOKS_PER_BRAND = 2
HOOK_CHARS = 110
SNAPSHOT_MARKERS = ("ad_snapshot_url", "access_token")
TONES = ["urgent", "confident", "warm", "playful", "educational", "aspirational", "luxurious", "clinical",
         "authentic", "calm", "celebratory", "empathetic", "salesy"]
KNOWN_FIXTURE = {"synthetic-uk": ("Synthetic Ayurveda UK", "900000000000001", "GB"),
                 "synthetic-multi": ("Synthetic Multi-market Botanicals", "900000000000003", "GB"),
                 "synthetic-in": ("Synthetic Ayurveda IN", "900000000000002", "IN")}


class HandoffError(Exception):
    pass


# --- inputs ------------------------------------------------------------------------------

def build_fixture(fixture: Path, data_dir: Path) -> None:
    """Fetch + normalize + merge LLM output from a synthetic fixture, offline, through the guarded transport."""
    import fetch_ads
    import llm_batches
    import normalize

    for key, (_name, page_id, country) in KNOWN_FIXTURE.items():
        rc = fetch_ads.main(["--page-id", page_id, "--country", country, "--since", "2026-01", "--until", "2026-09",
                             "--out-key", key, "--fixture", str(fixture), "--data-dir", str(data_dir), "--limit", "5"])
        if rc != 0:
            raise HandoffError(f"fixture fetch for {key} failed (rc={rc})")
        raw = sorted((data_dir / "raw" / key).glob("*.jsonl"))
        if raw and key != "synthetic-in":
            normalize.main([str(raw[-1]), "--data-dir", str(data_dir)])
    llm = fixture.parent / "llm_output_synthetic.json"
    if llm.exists() and (data_dir / "ads" / "synthetic-uk").exists():
        llm_batches.main(["merge", "--target", "synthetic-uk", str(llm), "--data-dir", str(data_dir)])


def fixture_brands() -> list[dict]:
    return [{"key": k, "name": n, "countries": [c], "page_ids": [p],
             "routes": {"ads_archive": [c] if c in EU_UK else [], "mcp": [] if c in EU_UK else [c]}}
            for k, (n, p, c) in KNOWN_FIXTURE.items()]


def project_brands(project: dict) -> list[dict]:
    out = []
    for b in project["brands"]:
        countries = projects.brand_countries(b, project)
        cov = b.get("coverage") or projects.coverage(b, countries)
        out.append({"key": b["key"], "name": b["name"], "countries": countries, "page_ids": b.get("page_ids") or [],
                    "routes": {"ads_archive": cov["ads_archive"], "mcp": cov["mcp"]}})
    return out


def single_brand(key: str, data_dir: Path) -> list[dict]:
    comp = load_competitors().get(key) if CONFIG_PATH.exists() else None
    ads = build_report.load(data_dir, key)
    if not ads and not comp:
        raise HandoffError(f"no project file, competitor or stored ads for {key!r}. Run /analyze-competitor or "
                           "/research-brands first, or use --fixture for the synthetic demo.")
    countries = build_report.queried_countries(data_dir, key, comp) or (comp or {}).get("countries") or ["GB"]
    name = (comp or {}).get("name") or (ads[0]["page"]["name"] if ads else key)
    return [{"key": key, "name": name, "countries": countries, "page_ids": [(comp or {}).get("page_id")] if comp else [],
             "routes": {"ads_archive": [c for c in countries if c in EU_UK],
                        "mcp": [c for c in countries if c not in EU_UK]}}]


def run_window(slug: str, reports_dir: Path) -> tuple[str, str] | None:
    f = reports_dir / slug / "latest" / "run.json"
    if f.exists():
        try:
            w = json.loads(f.read_text())["window"]
            return w["since"], w["until"]
        except (ValueError, KeyError):
            return None
    return None


# --- analysis ----------------------------------------------------------------------------

def clip(text: str | None, n: int = HOOK_CHARS) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "..."


def hook_of(ad: dict) -> tuple[str, str] | None:
    """(text, kind): an extracted hook if the LLM step ran, else the literal first line of the ad."""
    llm = ad.get("llm")
    if llm and llm.get("hook"):
        return clip(llm["hook"]), "hook"
    first = ad["derived"].get("first_line")
    return (clip(first), "opens with") if first else None


def format_of(ad: dict) -> str:
    llm = ad.get("llm")
    if llm and llm.get("format_guess") not in (None, "unknown"):
        return llm["format_guess"].replace("_", " ")
    if ad["derived"]["text_status"] == "creative-only":
        return "creative-only"
    return "multi-variant text" if ad["derived"]["variant_count"] > 1 else "single text"


def brand_summary(brand: dict, ads: list[dict]) -> dict:
    days = [a["dates"]["days_running"] for a in ads if a["dates"]["days_running"] is not None]
    llm = [a["llm"] for a in ads if a.get("llm")]
    return {
        "brand": brand, "ads": ads, "n": len(ads),
        "active": sum(1 for a in ads if a["dates"]["active_at_fetch"]),
        "median_days": statistics.median(days) if days else None,
        "formats": Counter(format_of(a) for a in ads),
        "tones": Counter(t for x in llm for t in x["tone"]),
        "offers": Counter(x["offer"]["type"] for x in llm if x["offer"]["type"] != "none"),
        "llm_n": len(llm),
        "price": sum(1 for a in ads if a["derived"]["has_price"]),
        "pct_off": sum(1 for a in ads if a["derived"]["has_percent_off"]),
        "platforms": Counter(p for a in ads for p in a["platforms"]),
    }


def zero_reason(b: dict) -> str:
    countries = ", ".join(b["countries"])
    if not b["routes"]["ads_archive"]:
        return (f"{countries} is outside the EU/UK, so the official API returns no commercial ads there. "
                "This is a coverage gap, not evidence the brand does not advertise.")
    if not b["page_ids"]:
        return "Page ID not confirmed, so nothing was fetched."
    return f"no ads delivered to {', '.join(b['routes']['ads_archive'])} in the window; ads outside the EU/UK are not covered."


def top_hooks(s: dict) -> list[tuple[str, str, str]]:
    """Longest-running ads with distinct hooks: (hook, kind, library_id)."""
    seen, out = set(), []
    for a in sorted(s["ads"], key=lambda a: (-(a["dates"]["days_running"] or 0), a["library_id"])):
        h = hook_of(a)
        if not h or h[0].lower() in seen:
            continue
        seen.add(h[0].lower())
        out.append((h[0], h[1], a["library_id"]))
        if len(out) == HOOKS_PER_BRAND:
            break
    return out


def render(title: str, summaries: list[dict], window: str, source: str, today: str, synthetic: bool) -> str:
    covered = [s for s in summaries if s["n"]]
    gaps = [s for s in summaries if not s["n"]]
    total = sum(s["n"] for s in summaries)
    L: list[str] = [f"# Handoff: {title}", ""]
    if synthetic:
        L += ["> SYNTHETIC DATA (test fixture). Not real competitor activity.", ""]
    L += [f"{today} | {source} | window {window} | {len(summaries)} brand(s), {total} ads | source: Meta Ad Library "
          "(official API), stored offline.", "",
          "Tags: **Observed** = in the ads. **Pattern** = repeats (counts given). **Hypothesis** = our reading, "
          "untested. **Unknown** = not available from official sources.", "", "## Coverage"]
    for s in summaries:
        b = s["brand"]
        where = "/".join(b["countries"])
        if s["n"]:
            L.append(f"- Observed: {b['name']} ({where}): {s['n']} ads, {s['active']} live at fetch.")
        else:
            L.append(f"- Unknown: {b['name']} ({where}): 0 ads. {zero_reason(b)}")

    L += ["", "## What they run"]
    for s in covered:
        fm = ", ".join(f"{k} {v}" for k, v in s["formats"].most_common(3))
        med = f"median {s['median_days']:.0f} days live" if s["median_days"] is not None else "run length n/a"
        L.append(f"- Observed: {s['brand']['name']}: {s['n']} ads, {med}; formats {fm}.")
    if not covered:
        L.append("- Unknown: no ads in the covered markets, so nothing to describe.")

    L += ["", "## Hooks and angles (longest-running first)"]
    for s in covered:
        for text, kind, lid in top_hooks(s):
            L.append(f"- Observed: {s['brand']['name']} {kind}: \"{text}\" {permalink(lid)}")
    tones = Counter()
    offers = Counter()
    for s in covered:
        tones.update(s["tones"])
        offers.update(s["offers"])
    llm_n = sum(s["llm_n"] for s in covered)
    if tones:
        top = ", ".join(f"{t} ({c}/{llm_n})" for t, c in tones.most_common(3))
        L.append(f"- Pattern: tone across extracted ads: {top}.")
    if offers:
        L.append("- Pattern: offers by type: " + ", ".join(f"{t} {c}" for t, c in offers.most_common(4)) + ".")
    pct = sum(s["pct_off"] for s in covered)
    price = sum(s["price"] for s in covered)
    if covered:
        L.append(f"- Pattern: {pct} of {total} ads state a percent-off and {price} state a price in the copy.")
    if llm_n < total and covered:
        L.append(f"- Unknown: angles/tone were extracted for {llm_n} of {total} ads; the rest show the literal "
                 "first line only (run the extract step for the full read).")

    L += ["", "## Whitespace (Hypothesis, test before betting)"]
    used = set(tones)
    unused = [t for t in ("educational", "authentic", "playful", "empathetic", "clinical", "calm") if t not in used]
    if llm_n and unused:
        L.append(f"- Hypothesis: no extracted ad uses a {' / '.join(unused[:3])} tone; an open lane if it fits the brand.")
    formats = Counter()
    for s in covered:
        formats.update(s["formats"])
    if covered and not any(f in formats for f in ("video", "carousel")):
        L.append("- Hypothesis: formats read from text only; video/carousel use is Unknown (no creative in the API), "
                 "so check visuals by eye in the Ad Library before claiming a format gap.")
    if covered and not pct:
        L.append("- Hypothesis: nobody leads with percent-off, so a value-led angle may stand apart.")
    elif covered and pct * 2 >= total:
        L.append("- Hypothesis: discount copy is crowded here; a non-discount hook may stand out.")
    if gaps:
        L.append(f"- Hypothesis: {', '.join(g['brand']['name'] for g in gaps)} is a blind spot, not an empty lane. "
                 "Do not read the missing ads as a gap to fill.")

    L += ["", "## Unknown (do not invent)",
          "- Spend, CPM, CPA, ROAS, impressions and which ads performed: not available from official sources. "
          "Run length is only a weak signal of what works.",
          "- Images and video: the API gives text, dates, platforms and reach only. Look at creative by eye."]
    out = "\n".join(L) + "\n"
    for marker in SNAPSHOT_MARKERS:
        if marker in out.lower():
            raise HandoffError(f"refusing to write a handoff containing {marker!r}")
    assert_no_token(out, "handoff")
    return out


def fit(title, summaries, window, source, today, synthetic) -> str:
    """Render, tightening the hook count until the page fits MAX_LINES."""
    global HOOKS_PER_BRAND
    start = HOOKS_PER_BRAND
    try:
        for n in range(start, 0, -1):
            HOOKS_PER_BRAND = n
            text = render(title, summaries, window, source, today, synthetic)
            if text.count("\n") <= MAX_LINES:
                return text
        return text
    finally:
        HOOKS_PER_BRAND = start


# --- main ----------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="write handoff/<slug>.md for the Instagram creative agent")
    ap.add_argument("slug", help="project slug (config/projects/<slug>.yaml), competitor key, or any name with --fixture")
    ap.add_argument("--fixture", type=Path, help="build the synthetic dataset offline from this fixture")
    ap.add_argument("--dry-run", action="store_true", help="print what would be read and written; write nothing")
    ap.add_argument("--date", help="stamp date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    ap.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    ap.add_argument("--out-dir", type=Path, default=HANDOFF_DIR)
    args = ap.parse_args(argv)
    today = args.date or utc_now().date().isoformat()
    tmp = None
    try:
        data_dir, title, window = args.data_dir, args.slug, None
        if args.fixture:
            tmp = tempfile.TemporaryDirectory(prefix="fbcm-handoff-")
            data_dir = Path(tmp.name)
            if not args.dry_run:
                build_fixture(args.fixture, data_dir)
            brands, source = fixture_brands(), "synthetic fixture"
        elif projects.project_path(args.slug, args.projects_dir).exists():
            project = projects.load_project(args.slug, args.projects_dir)
            brands, title, source = project_brands(project), project.get("title") or args.slug, f"project {args.slug}"
            window = run_window(args.slug, args.reports_dir)
        else:
            brands, source = single_brand(args.slug, data_dir), f"brand {args.slug}"
        summaries = []
        for b in brands:
            ads = build_report.load(data_dir, b["key"])
            if window:
                ads = build_report.window_ads(ads, *window)
            summaries.append(brand_summary(b, ads))
        starts = sorted(a["dates"]["start_month"] for s in summaries for a in s["ads"] if a["dates"]["start_month"])
        label = f"{window[0]}..{window[1]}" if window else (f"{starts[0]}..{starts[-1]}" if starts else "n/a")
        target = args.out_dir / f"{args.slug}.md"
        if args.dry_run:
            print(f"# dry run: would read stored ads for {', '.join(b['key'] for b in brands)} "
                  f"({'synthetic fixture, built in a temp dir' if args.fixture else str(data_dir / 'ads')}) and write {target}. "
                  "No Meta call is made; nothing written.")
            return 0
        text = fit(title, summaries, label, source, today, bool(args.fixture))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        print(target)
        return 0
    except (HandoffError, projects.ProjectError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
