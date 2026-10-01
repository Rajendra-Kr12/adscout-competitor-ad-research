#!/usr/bin/env python3
"""Export one project run to an Excel workbook and CSVs (deterministic; no LLM).

Sheets (plan §3.4): About, Ads (one row per ad, full text), Variants (every unique text per field),
Monthly (brand x month), Offers, Reach (age x gender rows), Changes (filled by monitoring, stage 4)
and Coverage (per brand: countries, route, coverage plan, 0-ad explanation, LLM coverage, critic findings).
The CSV folder has one file per sheet except About.

The workbook links to public Ad Library permalinks only. It embeds no images (competitor creatives
are retention-limited Platform Data and reports are kept), and it refuses to write any cell that
contains `access_token`. Text that starts with = + - @ is stored as text, never as a formula.

Usage (run_project.py calls this when the project asks for xlsx or csv):
  python scripts/export_xlsx.py <project> [--date YYYY-MM-DD]     # re-export from the run's run.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import statistics
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

import build_report
import project as projects
from common import DATA_DIR, REPORTS_DIR, assert_no_token, months_between, parse_month, utc_now
from run_project import brand_ads
from text_metrics import PERCENT_OFF_RE, breakdown_rows

EXCEL_CELL_MAX = 32767


def _ads_columns() -> list[str]:
    """The per-brand ads.csv columns plus the creative columns (empty until stage 3) and coverage basis."""
    cols = list(build_report.CSV_COLUMNS)
    cols.insert(cols.index("cta_copy") + 1, "cta_button")
    at = cols.index("fetched_at")
    cols[at:at] = ["on_image_text", "visual_notes", "media_paths", "creative_reuse_of", "coverage_basis"]
    return cols


ADS_COLUMNS = _ads_columns()
SHEETS = {
    "Variants": ["brand", "library_id", "field", "index", "text", "repeats"],
    "Monthly": ["brand", "month", "new_ads", "active_ads", "median_words_new", "new_with_offer", "pct_new_with_offer"],
    "Offers": ["brand", "month", "offer_type", "offer_text", "depth_pct", "ads", "library_ids", "source"],
    "Reach": ["brand", "library_id", "country", "age_range", "male", "female", "unknown"],
    "Changes": ["brand", "library_id", "event", "detected_at", "detail"],
    "Coverage": ["brand", "countries", "ads_archive_countries", "mcp_countries", "status", "ads_in_window",
                 "zero_ads_explanation", "llm_coverage", "coverage_plan", "critic_findings"],
}
FIELD_NAMES = {"bodies": "body", "link_titles": "title", "link_descriptions": "description", "link_captions": "caption"}
WIDE = {"primary_text": 80, "headlines": 40, "descriptions": 40, "captions": 30, "text": 80, "coverage_plan": 80,
        "offer_text": 40, "proposition": 40, "hook": 40, "zero_ads_explanation": 50, "critic_findings": 50}
PCT_RE = re.compile(r"(\d{1,3})\s?%")


class ExportError(RuntimeError):
    pass


# --- rows ---------------------------------------------------------------------------------

def creative_fields(ad: dict) -> dict:
    c = ad.get("creative") if isinstance(ad.get("creative"), dict) else {}
    return {"cta_button": c.get("cta_button"), "on_image_text": c.get("on_image_text"),
            "visual_notes": c.get("visual_notes"), "media_paths": ", ".join(c.get("media_paths") or []),
            "creative_reuse_of": c.get("creative_reuse_of")}


def ads_rows(brand: str, ads: list[dict]) -> list[dict]:
    return [build_report.ad_row(a, brand) | creative_fields(a) | {"coverage_basis": a["coverage_basis"]}
            for a in ads]


def variant_rows(brand: str, ads: list[dict]) -> list[dict]:
    return [{"brand": brand, "library_id": a["library_id"], "field": FIELD_NAMES[f], "index": i,
             "text": v["text"], "repeats": v["count"]}
            for a in ads for f in FIELD_NAMES for i, v in enumerate(a["derived"]["variants_unique"][f], 1)]


def has_offer(ad: dict) -> bool:
    """LLM offer when extracted, else the '% off' regex (the About sheet says so)."""
    llm = ad.get("llm")
    if llm:
        return (llm.get("offer") or {}).get("type") not in (None, "none")
    return bool(ad["derived"]["has_percent_off"])


def monthly_rows(brand: str, ads: list[dict], since: str, until: str) -> list[dict]:
    out = []
    for m in months_between(parse_month(since), parse_month(until)):
        new = [a for a in ads if a["dates"]["start_month"] == m]
        words = [x for a in new for x in a["derived"]["body_words"]]
        offers = sum(1 for a in new if has_offer(a))
        out.append({"brand": brand, "month": m, "new_ads": len(new),
                    "active_ads": sum(1 for a in ads if m in a["dates"]["active_months"]),
                    "median_words_new": statistics.median(words) if words else None,
                    "new_with_offer": offers, "pct_new_with_offer": round(100 * offers / len(new)) if new else None})
    return out


def _depth(*texts) -> int | None:
    pcts = [int(n) for t in texts if t for n in PCT_RE.findall(t) if 0 < int(n) <= 100]
    return max(pcts) if pcts else None


def offer_rows(brand: str, ads: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for a in ads:
        llm = a.get("llm")
        body = "\n".join(a["text"]["bodies"] + a["text"]["link_titles"] + a["text"]["link_descriptions"])
        if llm and (llm.get("offer") or {}).get("type") not in (None, "none"):
            o = llm["offer"]
            key = (a["dates"]["start_month"], o["type"], o.get("text") or "", "llm")
            depth = _depth(o.get("text"))
        elif not llm and a["derived"]["has_percent_off"]:
            m = PERCENT_OFF_RE.search(body)
            key = (a["dates"]["start_month"], "percent_off", m.group(0) if m else "", "regex")
            depth = _depth(m.group(0) if m else None)
        else:
            continue
        g = groups.setdefault(key, {"brand": brand, "month": key[0], "offer_type": key[1], "offer_text": key[2],
                                    "depth_pct": depth, "ads": 0, "library_ids": [], "source": key[3]})
        g["ads"] += 1
        g["library_ids"].append(a["library_id"])
        if depth is not None and (g["depth_pct"] is None or depth > g["depth_pct"]):
            g["depth_pct"] = depth
    rows = sorted(groups.values(), key=lambda r: (r["month"] or "", r["offer_type"], r["offer_text"]))
    return [r | {"library_ids": ", ".join(r["library_ids"])} for r in rows]


def reach_rows(brand: str, ads: list[dict]) -> list[dict]:
    return [{"brand": brand, "library_id": a["library_id"], **r}
            for a in ads for r in breakdown_rows((a.get("eu_metrics") or {}).get("age_country_gender_reach_breakdown"))]


def zero_ads_explanation(entry: dict) -> str | None:
    if entry["stats"]["ads_in_window"]:
        return None
    routes = entry["routes"]
    if not routes["ads_archive"]:
        return (f"None of {', '.join(entry['countries'])} is in the EU/UK, so ads_archive has no commercial ads "
                "there, and the Meta Ads MCP route is not connected in this stage. Coverage limit, not an error.")
    if entry["status"].startswith("unresolved"):
        return "Page ID unresolved, so nothing was fetched."
    if entry["status"] not in ("ok", "rebuilt from stored data"):
        return f"The fetch did not complete ({entry['status']})."
    return (f"ads_archive returned no ads delivered to {', '.join(routes['ads_archive'])} in the window. Commercial "
            "ads appear only if delivered there in the past year.")


def critic_findings(paths: projects.RunPaths, brand: str) -> str:
    """Findings from this run's critique files that name the brand (or no brand at all)."""
    out = []
    for stage in ("plan", "data", "report"):
        f = paths.critique(stage)
        if not f.exists():
            continue
        try:
            doc = json.loads(f.read_text())
        except ValueError:
            continue
        for x in doc.get("findings") or []:
            if not x.get("brands") or brand in x["brands"]:
                out.append(f"{stage} {x.get('id')} [{x.get('severity')}] {x.get('claim')}")
    return "\n".join(out)


def coverage_rows(manifest: dict, paths: projects.RunPaths) -> list[dict]:
    rows = []
    for e in manifest["brands"]:
        s = e["stats"]
        rows.append({"brand": e["name"], "countries": ", ".join(e["countries"]),
                     "ads_archive_countries": ", ".join(e["routes"]["ads_archive"]),
                     "mcp_countries": ", ".join(e["routes"]["mcp"]), "status": e["status"],
                     "ads_in_window": s["ads_in_window"], "zero_ads_explanation": zero_ads_explanation(e),
                     "llm_coverage": f"{s['llm_coverage']}/{s['ads_in_window']}", "coverage_plan": e["coverage_plan"],
                     "critic_findings": critic_findings(paths, e["key"])})
    return rows


def collect(project: dict, manifest: dict, paths: projects.RunPaths, data_dir: Path) -> dict[str, list[dict]]:
    since, until = manifest["window"]["since"], manifest["window"]["until"]
    sheets: dict[str, list[dict]] = {"Ads": [], **{k: [] for k in SHEETS}}
    for e in manifest["brands"]:
        ads = brand_ads(data_dir, e["key"], since, until)
        sheets["Ads"] += ads_rows(e["name"], ads)
        sheets["Variants"] += variant_rows(e["name"], ads)
        sheets["Monthly"] += monthly_rows(e["name"], ads, since, until)
        sheets["Offers"] += offer_rows(e["name"], ads)
        sheets["Reach"] += reach_rows(e["name"], ads)
    sheets["Coverage"] = coverage_rows(manifest, paths)
    return sheets


def about_rows(project: dict, manifest: dict) -> list[tuple[str, str]]:
    fixture = manifest["mode"] == "fixture" or any("fixture" in b["stats"]["coverage_basis"] for b in manifest["brands"])
    rows = [("Project", project.get("title") or project["project"]), ("Run date", manifest["run_date"]),
            ("Window", f"{manifest['window']['since']} to {manifest['window']['until']}"),
            ("Brands", ", ".join(b["name"] for b in manifest["brands"])),
            ("Source", "Meta Ad Library API (ads_archive), official. Links are public Ad Library permalinks."),
            ("Generated", f"{manifest['generated_at']} by scripts/export_xlsx.py from run.json")]
    if fixture:
        rows.insert(0, ("SYNTHETIC DATA", "Built from test fixtures, not from Meta. Nothing here describes a real advertiser."))
    rows += [
        ("Coverage", "Commercial ads are in the API only if delivered to the EU/UK in the past year. Countries outside "
                     "the EU/UK are on the Meta Ads MCP route (headline, dates and permalink only; not connected yet). "
                     "See the Coverage sheet."),
        ("Not available", "Competitor spend, impressions, CPM, CPA, ROAS and engagement do not exist in any official "
                          "source. Days running is a proxy, not performance data."),
        ("Ads", "One row per ad in the window. primary_text joins unique variants with ---, [xN] marks repeats. "
                "LLM columns are empty until ad-extractor has run. Creative columns (cta_button, on_image_text, "
                "visual_notes, media_paths, creative_reuse_of) stay empty until visuals (stage 3)."),
        ("Monthly", "new_with_offer uses the LLM offer type when extracted, else the '% off' pattern in the text."),
        ("Offers", "source llm = ad-extractor offer; source regex = a '% off' pattern found in ads without LLM output."),
        ("Changes", "Filled by monitoring (stage 4). Empty in this stage."),
        ("Images", "None embedded: creatives are retention-limited Platform Data and this workbook is kept."),
    ]
    return rows


# --- writing ----------------------------------------------------------------------------------

def clean(value):
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(value) > EXCEL_CELL_MAX:
            value = value[: EXCEL_CELL_MAX - 20] + " [truncated]"
    return value


def _date_or_text(value):
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    return value


def _write_sheet(ws, columns: list[str], rows: list[dict]) -> None:
    ws.append(columns)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r, row in enumerate(rows, 2):
        for col, name in enumerate(columns, 1):
            value = row.get(name)
            if name in ("start", "stop"):
                value = _date_or_text(value)
            cell = ws.cell(row=r, column=col, value=clean(value))
            if isinstance(cell.value, str):
                cell.data_type = "s"  # never a formula, whatever the text starts with
                if name in WIDE:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
            elif isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
            if name == "permalink" and value:
                cell.hyperlink = value
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    for col, name in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(col)].width = WIDE.get(name, max(10, min(24, len(name) + 2)))


def scan_for_token(wb) -> None:
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and "access_token" in c.value.lower():
                    raise ExportError(f"refusing to write the workbook: {ws.title}!{c.coordinate} contains access_token")
                if c.hyperlink is not None and "access_token" in str(c.hyperlink.target or "").lower():
                    raise ExportError(f"refusing to write the workbook: {ws.title}!{c.coordinate} links to access_token")


def write_workbook(path: Path, project: dict, manifest: dict, sheets: dict[str, list[dict]]) -> Path:
    wb = Workbook()
    about = wb.active
    about.title = "About"
    for k, v in about_rows(project, manifest):
        about.append([k, clean(v)])
        about.cell(row=about.max_row, column=1).font = Font(bold=True)
        about.cell(row=about.max_row, column=2).data_type = "s"
        about.cell(row=about.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    about.column_dimensions["A"].width = 18
    about.column_dimensions["B"].width = 110
    _write_sheet(wb.create_sheet("Ads"), ADS_COLUMNS, sheets["Ads"])
    for name, cols in SHEETS.items():
        _write_sheet(wb.create_sheet(name), cols, sheets[name])
    scan_for_token(wb)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    wb.save(tmp)
    os.replace(tmp, path)
    return path


def csv_text(columns: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for row in rows:
        w.writerow([build_report.csv_safe(row.get(c)) for c in columns])
    return buf.getvalue()


def write_csvs(csv_dir: Path, sheets: dict[str, list[dict]]) -> list[Path]:
    csv_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for name, cols in [("Ads", ADS_COLUMNS), *SHEETS.items()]:
        text = csv_text(cols, sheets[name])
        path = csv_dir / f"{name.lower()}.csv"
        assert_no_token(text, str(path))
        path.write_text(text)
        out.append(path)
    return out


def export_run(project: dict, manifest: dict, paths: projects.RunPaths, data_dir: Path) -> list[Path]:
    sheets = collect(project, manifest, paths, data_dir)
    out = []
    if project["outputs"].get("xlsx"):
        out.append(write_workbook(paths.workbook, project, manifest, sheets))
    if project["outputs"].get("csv"):
        out += write_csvs(paths.csv_dir, sheets)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="project run -> workbook.xlsx + csv/")
    ap.add_argument("project")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    args = ap.parse_args(argv)
    run_date = args.date or utc_now().date().isoformat()
    try:
        project = projects.load_project(args.project, args.projects_dir)
    except projects.ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    paths = projects.run_paths(project, run_date, args.reports_dir)
    if not paths.manifest.exists():
        print(f"no run manifest {paths.manifest}; run scripts/run_project.py first", file=sys.stderr)
        return 2
    manifest = json.loads(paths.manifest.read_text())
    for p in export_run(project, manifest, paths, args.data_dir):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
