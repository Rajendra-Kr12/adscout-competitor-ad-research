#!/usr/bin/env python3
"""Run a multi-brand research project (config/projects/<slug>.yaml) into dated report folders.

Deterministic and non-interactive: it reads the saved project and never asks questions.
1. Brands on the ads_archive route (EU/UK countries, Page ID known) are batched into query groups
   of up to 10 Page IDs sharing one country set, so a group costs one query per window (+ paging).
   Every call goes through scripts/guard.py; a stop (throttle, cap, kill switch) ends the run.
2. Each group's raw file is normalized and split per brand into data/ads/<brand>/.
3. Per brand: report + ads.csv. Per run: index.md (cross-brand summary, coverage, critic block),
   run.json (manifest for exports and the critic), and the workbook/CSVs the project asked for.
   reports/<project>/project.md and the `latest` symlink are refreshed.
Brands with only non-EU/UK countries are not fetched: the Meta Ads MCP route is a stub in this
stage (scripts/mcp_source.py), and their reports say so. Unresolved Page IDs are reported, not guessed.

Usage:
  python scripts/run_project.py <slug> --dry-run            # request plan + call count, nothing sent
  python scripts/run_project.py <slug>                      # live, through the guard
  python scripts/run_project.py <slug> --fixture tests/fixtures/ads_archive_synthetic.json
  python scripts/run_project.py <slug> --no-fetch           # rebuild outputs from data/ads (e.g. after LLM merge)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import build_report
import normalize
import project as projects
from critique import CRITIC_END, CRITIC_START, render_block
from common import (
    DATA_DIR,
    DEFAULT_API_VERSION,
    REPORTS_DIR,
    ROOT,
    assert_no_token,
    iso_z,
    load_competitors,
    load_env,
    month_windows,
    utc_now,
    write_json,
)
from fetch_ads import Session, build_params, fetch_run, open_session
from guard import Guard, GuardStop

STANDARD_CAVEATS = [
    "Competitor spend, impressions, CPM, CPA, ROAS and engagement are not available from any official source.",
    "\"Days running\" is a proxy for what works, not performance data.",
    "Text analysis only: the CTA button, images, video and on-image text are not in the API (visuals are stage 3).",
]


# --- per-brand numbers (shared with export_xlsx.py and critique.py) -------------------

def brand_stats(ads: list[dict]) -> dict:
    days = [a["dates"]["days_running"] for a in ads if a["dates"]["days_running"] is not None]
    uk = [a["derived"]["uk_reach"]["value"] for a in ads if a["derived"].get("uk_reach")]
    eu = [r for r in (build_report.eu_reach(a) for a in ads) if r is not None]
    return {
        "ads_in_window": len(ads),
        "active_at_fetch": sum(1 for a in ads if a["dates"]["active_at_fetch"]),
        "median_days_running": statistics.median(days) if days else None,
        "with_percent_off": sum(1 for a in ads if a["derived"]["has_percent_off"]),
        "with_price": sum(1 for a in ads if a["derived"]["has_price"]),
        "creative_only": sum(1 for a in ads if a["derived"]["text_status"] == "creative-only"),
        "uk_reach_ads": len(uk), "uk_reach_sum": sum(uk) if uk else None,
        "eu_reach_ads": len(eu), "eu_reach_sum": sum(eu) if eu else None,
        "llm_coverage": sum(1 for a in ads if a.get("llm")),
        "pages_seen": sorted({str(a["page"]["id"]) for a in ads if a["page"]["id"]}),
        "coverage_basis": sorted({a["coverage_basis"] for a in ads}),
        "near_duplicates": sum(1 for a in ads if a["derived"]["near_duplicate_of"]),
    }


def brand_ads(data_dir: Path, key: str, since: str, until: str) -> list[dict]:
    return build_report.window_ads(build_report.load(data_dir, key), since, until)


# --- plan -------------------------------------------------------------------------------

def windows_for(project: dict, today: date, single_window: bool) -> tuple[str, str, list[tuple[str, str]]]:
    since, until = projects.resolve_window(project, today)
    windows = month_windows(since, until, last_day=projects.last_day(today))
    if not windows:
        raise projects.ProjectError(f"the window {since}..{until} starts after Meta's current day")
    if single_window:
        windows = [(windows[0][0], windows[-1][1])]
    return since, until, windows


def group_target(project: dict, g: dict) -> dict:
    return {"key": f"project-{project['project']}", "page_ids": g["page_ids"], "search_terms": None,
            "search_type": None, "countries": g["countries"], "competitor": None,
            "project": project["project"], "group": g["group"], "brands": g["brands"]}


def print_dry_run(project: dict, groups: list[dict], windows, url: str, limit: int, guard: Guard) -> None:
    for g in groups:
        print(f"# group {g['group']}: {', '.join(g['brands'])} ({len(g['page_ids'])} Page IDs) in "
              f"{', '.join(g['countries'])}")
        for dmin, dmax in windows:
            params = build_params(page_ids=g["page_ids"], search_terms=None, countries=g["countries"],
                                  date_min=dmin, date_max=dmax, media_type="ALL", limit=limit, token="REDACTED")
            print(f"GET {url}?{urlencode(params)}")
    for b in projects.unresolved(project):
        print(f"# {b['key']}: Page ID unresolved, not fetched. Run: python scripts/discover_pages.py "
              f"--project {project['project']} (1 call)")
    for r in projects.mcp_plan(project):
        print(f"# {r['brand']}: Meta Ads MCP {r['tool']} for {', '.join(r['countries'])} - not connected, not sent")
    c = guard.counts()
    n = len(groups) * len(windows)
    print(f"# {n} ads_archive queries ({len(groups)} group(s) x {len(windows)} window(s)) + paging, "
          f"plus at most 1 debug_token check per day. Nothing was sent. Guard caps: "
          f"{guard.api['max_calls_per_run']}/run, {c['hour']}/{guard.api['max_calls_per_hour']} used this hour, "
          f"{c['day']}/{guard.api['max_calls_per_day']} used in 24h.", file=sys.stderr)


# --- fetch -------------------------------------------------------------------------------

def fetch_groups(project: dict, groups: list[dict], *, session: Session, since: str, until: str, windows,
                 limit: int, api_version: str, data_dir: Path) -> list[dict]:
    """Fetch and normalize each group in turn; stop at the first group that does not finish."""
    out = []
    with session.guard.lock():
        session.preflight(api_version, data_dir)
        for g in groups:
            res = fetch_run(target=group_target(project, g), windows=windows, media_types=["ALL"], since=since,
                            until=until, limit=limit, transport=session.transport, token=session.token,
                            mode=session.mode, api_version=api_version, guard=session.guard, data_dir=data_dir)
            rec = {"group": g["group"], "countries": g["countries"], "brands": g["brands"], "page_ids": g["page_ids"],
                   "raw": str(res["raw"].relative_to(data_dir)), "run_id": res["run_id"],
                   "ledger": str(Path(res["ledger"]).relative_to(data_dir)) if res["ledger"] else None,
                   "calls": res["calls"], "records": res["records"], "unique_ads": res["unique_ads"],
                   "status": "ok" if res["rc"] == 0 else "stopped",
                   "stop_reason": str(res["stopped"]) if res["stopped"] else None, "rc": res["rc"]}
            out.append(rec)
            if res["rc"] != 0:
                print(f"group {g['group']} STOPPED: {res['stopped']}", file=sys.stderr)
                break
            normalize.normalize_run(res["raw"], data_dir, page_map=g["page_map"])
    return out


# --- outputs -----------------------------------------------------------------------------

def brand_status(b: dict, fetched: dict[str, dict], no_fetch: bool) -> str:
    cov = b["coverage"]
    if not cov["ads_archive"]:
        return "mcp-only (not fetched)"
    if not b["page_ids"]:
        return "unresolved Page ID (not fetched)"
    if no_fetch:
        return "rebuilt from stored data"
    g = fetched.get(b["key"])
    if g is None:
        return "not fetched (an earlier group stopped)"
    return "ok" if g["status"] == "ok" else "fetch stopped"


def display_path(path: Path) -> str:
    """Repo-relative when inside the repo, else absolute."""
    p = Path(path).resolve()
    return str(p.relative_to(ROOT.resolve())) if ROOT.resolve() in p.parents else str(p)


def rel(path: Path, start: Path) -> str:
    return os.path.relpath(path, start)


def fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:g}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def build_index(project: dict, manifest: dict, paths: projects.RunPaths) -> str:
    here = paths.index.parent
    fixture = is_fixture(manifest)
    L = [f"# Research run: {project.get('title') or project['project']} - {manifest['run_date']}", ""]
    if fixture:
        L += ["> **SYNTHETIC DATA.** Built from test fixtures, not from Meta. Nothing below describes a real "
              "advertiser.", ""]
    calls = sum(g["calls"] for g in manifest["groups"])
    L += [f"- Project: `{project['project']}` ({manifest['project_file']}); goal "
          f"{project['goal']}; layout {project['layout']}",
          f"- Window: {manifest['window']['since']} to {manifest['window']['until']} "
          f"({manifest['window']['months']} month{'s' if manifest['window']['months'] != 1 else ''})",
          f"- Fetch: {manifest['mode']}, {calls} ads_archive call(s) in {len(manifest['groups'])} query group(s)"
          f"{' (outputs rebuilt since, without new calls)' if manifest.get('rebuilt_without_fetch') else ''}; "
          f"run status **{manifest['status']}**",
          f"- Generated: {manifest['generated_at']} by `scripts/run_project.py`", ""]
    L += ["## Brands", "",
          "| Brand | Countries | Route | Status | Ads in window | Active | Median days running | With % off "
          "| Creative-only | UK reach (sum) | LLM | Report |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in manifest["brands"]:
        s = b["stats"]
        route = " + ".join(filter(None, [
            f"ads_archive {','.join(b['routes']['ads_archive'])}" if b["routes"]["ads_archive"] else "",
            f"MCP {','.join(b['routes']['mcp'])} (not connected)" if b["routes"]["mcp"] else ""]))
        L.append(f"| {b['name']} | {', '.join(b['countries'])} | {route} | {b['status']} | {s['ads_in_window']} "
                 f"| {s['active_at_fetch']} | {fmt(s['median_days_running'])} | {s['with_percent_off']} "
                 f"| {s['creative_only']} | {fmt(s['uk_reach_sum'])} | {s['llm_coverage']}/{s['ads_in_window']} "
                 f"| [report]({rel(paths.root / b['report'], here)}) |")
    L += ["", "## Coverage per brand", ""]
    L += [f"- **{b['name']}**: {b['coverage_plan']}" for b in manifest["brands"]]
    L += ["- Outside the EU/UK, 0 commercial ads from `ads_archive` is coverage, not an error.", ""]
    if manifest["unresolved"] or manifest["mcp_not_sent"]:
        L += ["## Not fetched in this run", ""]
        L += [f"- {k}: Page ID unresolved. Run `python scripts/discover_pages.py --project {project['project']}` "
              "(1 call per brand) and confirm the Page with `project.py set-page`." for k in manifest["unresolved"]]
        L += [f"- {r['brand']}: Meta Ads MCP `{r['tool']}` for {', '.join(r['countries'])} - not connected in this "
              "stage, so nothing was requested." for r in manifest["mcp_not_sent"]]
        L.append("")
    stopped = [g for g in manifest["groups"] if g["status"] != "ok"]
    if stopped:
        L += ["## Fetch stopped", ""]
        L += [f"- Group {g['group']} ({', '.join(g['brands'])}): {g['stop_reason']}. Ledger `data/{g['ledger']}`. "
              "The guard decides when a new run may start; do not retry by hand." for g in stopped]
        L.append("")
    L += ["## Critic", "", CRITIC_START, render_block(paths), CRITIC_END, "", "## Files", ""]
    for name, p in (("Workbook", paths.workbook), ("CSV folder", paths.csv_dir), ("Comparison", paths.comparison)):
        if p.exists():
            L.append(f"- {name}: [{rel(p, here)}]({rel(p, here)})")
    L += [f"- {b['name']}: [{rel(paths.root / b['report'], here)}]({rel(paths.root / b['report'], here)}), "
          f"[{rel(paths.root / b['csv'], here)}]({rel(paths.root / b['csv'], here)})" for b in manifest["brands"]]
    L += ["", "## Not built in this stage", "",
          f"- Visuals (stage 3): saved preference `{(project.get('visuals') or {}).get('route', '-')}`; nothing is downloaded.",
          f"- Monitoring (stage 4): saved preference `{(project.get('monitor') or {}).get('cadence', '-')}`; nothing is scheduled."]
    later = [k for k in ("html", "drive") if project["outputs"].get(k)]
    if later:
        L.append(f"- Requested outputs not built yet: {', '.join(later)} (saved for a later stage).")
    L += ["", "## Caveats", ""] + [f"- {c}" for c in STANDARD_CAVEATS] + [""]
    return "\n".join(L)


def window_text(w: dict) -> str:
    return f"{w['since']} to {w['until']}" if "since" in w else f"the last {w['months']} months, ending with the run month"


def is_fixture(manifest: dict) -> bool:
    return manifest["mode"] == "fixture" or any("fixture" in b["stats"]["coverage_basis"] for b in manifest["brands"])


def build_comparison(project: dict, manifest: dict, data_dir: Path) -> str:
    """Cross-brand tables from the same numbers as the brand reports, plus the report-writer block."""
    since, until = manifest["window"]["since"], manifest["window"]["until"]
    L = [f"# Comparison: {project.get('title') or project['project']} - {manifest['run_date']}", ""]
    if is_fixture(manifest):
        L += ["> **SYNTHETIC DATA.** Built from test fixtures, not from Meta. Nothing below describes a real "
              "advertiser.", ""]
    L += [f"Window {since} to {until}. Every number comes from `run.json` and `data/ads/`; brands on the Meta Ads "
          "MCP route (outside the EU/UK) have no fetched ads in this stage and are listed for coverage only.", "",
          "## Side by side", "",
          "| Brand | Countries (ads_archive) | Ads | Active | Median days running | Ads with % off "
          "| Median words per body | Creative-only | Top platforms | UK reach (sum) | LLM |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    llm_rows = []
    for e in manifest["brands"]:
        ads = brand_ads(data_dir, e["key"], since, until)
        s = e["stats"]
        words = [x for a in ads for x in a["derived"]["body_words"]]
        plats = Counter(p for a in ads for p in a["platforms"]).most_common(2)
        L.append(f"| {e['name']} | {', '.join(e['routes']['ads_archive']) or 'none'} | {s['ads_in_window']} "
                 f"| {s['active_at_fetch']} | {fmt(s['median_days_running'])} | {s['with_percent_off']} "
                 f"| {fmt(statistics.median(words)) if words else 'n/a'} | {s['creative_only']} "
                 f"| {', '.join(f'{p} {n}' for p, n in plats) or '-'} | {fmt(s['uk_reach_sum'])} "
                 f"| {s['llm_coverage']}/{s['ads_in_window']} |")
        with_llm = [a for a in ads if a.get("llm")]
        if with_llm:
            offers = Counter(a["llm"]["offer"]["type"] for a in with_llm).most_common(3)
            tones = Counter(t for a in with_llm for t in a["llm"]["tone"]).most_common(3)
            llm_rows.append(f"| {e['name']} | {len(with_llm)} | {', '.join(f'{k} {v}' for k, v in offers)} "
                            f"| {', '.join(f'{k} {v}' for k, v in tones)} |")
    if llm_rows:
        L += ["", "## Messaging mix (LLM extraction)", "", "| Brand | Ads extracted | Offer types | Tone |",
              "|---|---|---|---|"] + llm_rows
    L += ["", "## Coverage", ""] + [f"- **{e['name']}**: {e['coverage_plan']}" for e in manifest["brands"]]
    L += ["", "## Comparison notes", "", build_report.NARRATIVE_START,
          "_Not written yet. The report-writer sub-agent fills this block from `prompts/comparison-template.md`. "
          "Numbers above are authoritative._", build_report.NARRATIVE_END, "",
          "## Caveats", ""] + [f"- {c}" for c in STANDARD_CAVEATS] + [""]
    return "\n".join(L)


def build_project_md(project: dict, paths: projects.RunPaths, project_file: str) -> str:
    runs = []
    for m in sorted(paths.project_dir.rglob("*run.json")):
        try:
            doc = json.loads(m.read_text())
        except ValueError:
            continue
        if doc.get("project") == project["project"]:
            runs.append((doc["run_date"], doc["status"], rel(m.parent, paths.project_dir), doc.get("index")))
    L = [f"# Project: {project.get('title') or project['project']}", "",
         f"Saved intake: `{project_file}` (created {project['created']}, "
         f"{project.get('intake_version', '?')}). Re-run with `python scripts/run_project.py {project['project']}`.", "",
         "## Scope", "",
         f"- Brands: {', '.join(b['name'] for b in project['brands'])}",
         f"- Default markets: {', '.join(project['markets'])}",
         f"- Window: {window_text(project['window'])}; goal: {project['goal']}; layout: {project['layout']}; "
         f"output root: {project['output_root']}",
         "- Outputs: " + ", ".join(k for k, v in project["outputs"].items() if v),
         "", "## Coverage basis per brand", ""]
    L += [f"- **{b['name']}** ({', '.join(projects.brand_countries(b, project))}): {b['coverage']['plan']}"
          for b in project["brands"]]
    L += ["", "## Runs", ""]
    L += [f"- {d}: {s} ([index]({os.path.join(folder, os.path.basename(idx)) if idx else folder}))"
          for d, s, folder, idx in sorted(set(runs), reverse=True)] or ["_none yet_"]
    c = project.get("critic")
    L += ["", "## Critic", "",
          f"Last verdict: **{c.get('last_verdict')}** at the {c.get('stage')} gate ({c.get('at')}, "
          f"{c.get('prompt_version')}). Open findings are in the run's index." if c else "_No critic verdict yet._", ""]
    return "\n".join(L)


def write_outputs(project: dict, manifest: dict, paths: projects.RunPaths, data_dir: Path, since: str,
                  until: str, today: date) -> None:
    competitors = load_competitors()
    for b, entry in zip(project["brands"], manifest["brands"]):
        ads = brand_ads(data_dir, b["key"], since, until)
        caveats = [f"Coverage plan for this brand: {b['coverage']['plan']}"]
        if entry["status"] == "fetch stopped" or entry["status"].startswith("not fetched (an earlier"):
            caveats.append(f"This run's fetch did not complete for this brand ({entry['status']}); numbers come "
                           "from earlier runs, if any.")
        if entry["status"].startswith("unresolved"):
            caveats.append("The Page ID is unresolved, so nothing was fetched. Run discover_pages.py and confirm the Page.")
        comp = {"name": b["name"], "countries": entry["countries"]}
        if competitors.get(b["key"], {}).get("coverage_note"):
            caveats.append(f"Config note: {competitors[b['key']]['coverage_note']}")
        runs = tuple(g["run_id"] for g in manifest["groups"] if b["key"] in g["brands"] and g.get("run_id"))
        text = build_report.build(ads, b["key"], since, until, comp, today, countries=entry["countries"],
                                  queried=b["coverage"]["ads_archive"], caveats=tuple(caveats),
                                  page_ids=tuple(b.get("page_ids") or ()), fetch_runs=runs)
        build_report.write_report(paths.root / entry["report"], text)
        csv_text = build_report.ads_csv(ads, b["name"])
        assert_no_token(csv_text, entry["csv"])
        (paths.root / entry["csv"]).write_text(csv_text)
    build_report.write_report(paths.comparison, build_comparison(project, manifest, data_dir))
    if project["outputs"].get("xlsx") or project["outputs"].get("csv"):
        import export_xlsx  # openpyxl is only needed when the project asks for exports
        export_xlsx.export_run(project, manifest, paths, data_dir)
    # The critic block is rendered from the run's critique-<stage>.json files, so it is rebuilt, not kept.
    build_report.write_report(paths.index, build_index(project, manifest, paths), blocks=())
    projects.point_latest(paths, [b["key"] for b in project["brands"]])
    write_json(paths.manifest, manifest)
    build_report.write_report(paths.project_md, build_project_md(project, paths, manifest["project_file"]))


def make_manifest(project: dict, paths: projects.RunPaths, groups_run: list[dict], *, mode: str, since: str,
                  until: str, today: date, data_dir: Path, no_fetch: bool, single_window: bool,
                  project_file: str) -> dict:
    fetched = {k: g for g in groups_run for k in g["brands"]}
    brands = []
    for b in project["brands"]:
        countries = projects.brand_countries(b, project)
        ads = brand_ads(data_dir, b["key"], since, until)
        brands.append({
            "key": b["key"], "name": b["name"], "page_ids": b["page_ids"],
            "page_id_source": b.get("page_id_source"), "countries": countries,
            "routes": {"ads_archive": b["coverage"]["ads_archive"], "mcp": b["coverage"]["mcp"]},
            "coverage_plan": b["coverage"]["plan"], "status": brand_status(b, fetched, no_fetch),
            "report": rel(paths.brand_report(b["key"]), paths.root), "csv": rel(paths.brand_csv(b["key"]), paths.root),
            "stats": brand_stats(ads),
        })
    statuses = {g["status"] for g in groups_run}
    return {
        "project": project["project"], "project_file": project_file, "run_date": paths.date, "generated_at": iso_z(utc_now()),
        "mode": mode, "rebuilt_without_fetch": no_fetch,
        "window": {"since": since, "until": until, "months": len(month_windows(since, until)),
                   "single_window": single_window},
        "layout": project["layout"], "output_root": str(paths.root),
        "index": rel(paths.index, paths.root),
        "workbook": rel(paths.workbook, paths.root) if project["outputs"].get("xlsx") else None,
        "csv_dir": rel(paths.csv_dir, paths.root) if project["outputs"].get("csv") else None,
        "groups": groups_run, "brands": brands,
        "unresolved": [b["key"] for b in projects.unresolved(project)],
        "mcp_not_sent": projects.mcp_plan(project),
        "status": "stopped" if "stopped" in statuses else "ok",
        "today": today.isoformat(),
    }


def main(argv=None, *, transport=None, guard: Guard | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project", help="project slug (config/projects/<slug>.yaml) or a path to a project file")
    ap.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR,
                    help="what output_root 'reports' means (tests); an absolute output_root is used as is")
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default: today, UTC); also the window's end month")
    ap.add_argument("--single-window", action="store_true",
                    help="one query per group for the whole window instead of one per calendar month")
    ap.add_argument("--limit", type=int, default=100,
                    help="page size (API max 5000; Meta returns ~25-40 ads per page with the full field list)")
    ap.add_argument("--dry-run", action="store_true", help="print the request plan and call count, send nothing")
    ap.add_argument("--fixture", type=Path, help="serve responses from a synthetic fixture (offline)")
    ap.add_argument("--no-fetch", action="store_true", help="rebuild outputs from data/ads only (no API calls)")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else utc_now().date()
    try:
        project = projects.load_project(args.project, args.projects_dir)
        paths = projects.run_paths(project, today.isoformat(), args.reports_dir)
        groups = projects.archive_groups(project)
        since, until, windows = windows_for(project, today, args.single_window)
    except projects.ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    api_version = load_env(args.env_file).get("META_API_VERSION") or DEFAULT_API_VERSION
    url = f"https://graph.facebook.com/{api_version}/ads_archive"

    if args.dry_run:
        print_dry_run(project, groups, windows, url, args.limit, guard or Guard(args.data_dir))
        return 0

    groups_run: list[dict] = []
    mode = "no-fetch"
    if not args.no_fetch and groups:
        if transport is not None:
            session = Session(transport, "TEST-NOT-A-REAL-TOKEN", "live", guard or Guard(args.data_dir), False)
        else:
            session = open_session(fixture=args.fixture, env_file=args.env_file, data_dir=args.data_dir, guard=guard)
        mode = session.mode
        try:
            groups_run = fetch_groups(project, groups, session=session, since=since, until=until, windows=windows,
                                      limit=args.limit, api_version=api_version, data_dir=args.data_dir)
        except GuardStop as exc:
            print(f"guard refused the run: {exc}", file=sys.stderr)
            return 3
    elif args.no_fetch and paths.manifest.exists():
        prev = json.loads(paths.manifest.read_text())
        groups_run, mode = prev.get("groups", []), prev.get("mode", mode)

    manifest = make_manifest(project, paths, groups_run, mode=mode, since=since, until=until, today=today,
                             data_dir=args.data_dir, no_fetch=args.no_fetch, single_window=args.single_window,
                             project_file=display_path(projects.project_path(args.project, args.projects_dir)))
    write_outputs(project, manifest, paths, args.data_dir, since, until, today)
    calls = sum(g["calls"] for g in groups_run)
    print(f"{project['project']} {paths.date}: {len(manifest['brands'])} brand(s), {calls} call(s), "
          f"status {manifest['status']}", file=sys.stderr)
    print(paths.index)
    for b in manifest["brands"]:
        print(paths.root / b["report"])
    for p in (paths.workbook, paths.csv_dir):
        if p.exists():
            print(p)
    return max([g["rc"] for g in groups_run] or [0])


if __name__ == "__main__":
    sys.exit(main())
