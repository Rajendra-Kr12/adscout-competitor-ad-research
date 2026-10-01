#!/usr/bin/env python3
"""Multi-brand research projects: config/projects/<slug>.yaml (schemas/project.schema.json).

A project holds the intake answers (brands, markets, window, goal, layout, outputs, and saved
preferences for later stages), so re-runs and scheduled runs never ask again. Brand names and
public Page IDs only, never Platform Data.

Any country per brand: EU/UK countries are routed to ads_archive (full detail). Any other country
goes to the Meta Ads MCP route, which gives discovery only (headline, start date, permalink for
live ads) and is not connected in this stage. Each brand's `coverage` block says this plainly and
is re-derived on every save.

CLI (the research-brands skill drives these; every answer has the plan's recommended default):
  python scripts/project.py new <slug> --brand forest-essentials-uk --brand "Pukka Herbs:GB" [--defaults]
         [--markets GB,DE] [--months 6 | --since 2026-01 --until 2026-09] [--goal messaging-creative]
         [--layout project/date/brand] [--output-root reports] [--outputs markdown,xlsx,csv]
         [--visuals inbox] [--monitor daily+weekly-digest] [--force]
  python scripts/project.py plan <slug>           # the plan screen: brands, routes, coverage, call estimate
  python scripts/project.py show <slug>
  python scripts/project.py validate <slug|path>
  python scripts/project.py set-page <slug> <brand-key> <page_id>[,<page_id>] [--source discovery|user]
  python scripts/project.py set-countries <slug> <brand-key> IN,GB

Brand spec: NAME_OR_KEY[:CC[+CC]][@PAGE_ID[+PAGE_ID]], for example
  forest-essentials-uk            a key from config/competitors.yaml
  "Kapiva:IN"                     a known brand with its own market (Meta Ads MCP route)
  "Some Brand:GB+DE@1234567890"   a new brand with its countries and Page ID
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import jsonschema
import yaml

from common import (
    DATA_DIR,
    EU_UK,
    REPORTS_DIR,
    ROOT,
    assert_no_token,
    country_code,
    load_competitors,
    month_windows,
    slug,
    utc_now,
)
import mcp_source

PROJECTS_DIR = ROOT / "config" / "projects"
PROJECT_SCHEMA_PATH = ROOT / "schemas" / "project.schema.json"
SCHEMA_VERSION = "project-v1"
INTAKE_VERSION = "intake-v1"
MAX_PAGE_IDS_PER_QUERY = 10  # ads_archive search_page_ids limit
LAYOUTS = ("project/date/brand", "brand/date", "flat")
GOALS = ("messaging-creative", "offers-calendar", "head-to-head", "all")

# The plan's recommended answers (firstmate fbcm-p1-plan §1.2). C1/C3/C4 are still open captain
# calls, so the user picks at intake and these are only the defaults and the fast path.
DEFAULTS = {
    "markets": ["GB"],
    "window": {"months": 6},
    "goal": "messaging-creative",
    "layout": "project/date/brand",
    "output_root": "reports",
    "outputs": {"markdown": True, "xlsx": True, "csv": True, "html": False, "drive": False},
    "visuals": {"route": "inbox", "window_days": 90, "max_ads_per_brand": 30, "video": "keyframes"},
    "monitor": {"cadence": "daily+weekly-digest", "time": "08:00", "watch": ["new_ads", "stopped_ads", "offers"],
                "alerts": ["digest", "macos"]},
}
FIELD_ORDER = ("schema_version", "project", "title", "created", "intake_version", "brands", "markets", "window",
               "goal", "layout", "output_root", "outputs", "visuals", "monitor", "critic", "notes")
BRAND_ORDER = ("key", "name", "page_ids", "page_id_source", "search_terms", "countries", "coverage")
HEADER = ("# Research project, written by scripts/project.py (research-brands intake; schemas/project.schema.json).\n"
          "# Brand names, public Page IDs and preferences only - never Platform Data. `coverage` is derived:\n"
          "# edit `countries` (or run project.py set-countries) instead.\n")

ARCHIVE_DETAIL = ("primary text, headline, link description, caption, start and stop dates, platforms, "
                  "languages, UK/EU reach, age x gender reach and targeting")


class ProjectError(ValueError):
    pass


@lru_cache(maxsize=1)
def validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(PROJECT_SCHEMA_PATH.read_text()))


# --- brands and coverage --------------------------------------------------------

def parse_countries(value) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[+,\s]+", str(value))
    out: list[str] = []
    for item in items:
        if str(item).strip():
            try:
                code = country_code(item)
            except ValueError as exc:
                raise ProjectError(str(exc)) from None
            if code not in out:
                out.append(code)
    return out


SPEC_RE = re.compile(r"^(?P<name>[^:@]+?)\s*(?::\s*(?P<cc>[A-Za-z+, ]+))?\s*(?:@\s*(?P<pages>[0-9+, ]+))?$")


def brand_from_spec(spec: str, competitors: dict[str, dict]) -> dict:
    """NAME_OR_KEY[:CC[+CC]][@PAGE_ID[+PAGE_ID]] -> a project brand (countries only when given)."""
    m = SPEC_RE.match(spec.strip())
    if not m:
        raise ProjectError(f"bad brand spec {spec!r}; use NAME_OR_KEY[:CC+CC][@PAGE_ID]")
    name = m["name"].strip()
    comp = competitors.get(name) or next(
        (c for c in competitors.values() if c.get("name", "").lower() == name.lower()), None)
    brand: dict = {"key": comp["key"] if comp else slug(name), "name": comp["name"] if comp else name,
                   "page_ids": []}
    if comp and comp.get("page_id"):
        brand["page_ids"] = [str(comp["page_id"])]
        brand["page_id_source"] = "config"
    if comp and comp.get("search_terms"):
        brand["search_terms"] = comp["search_terms"]
    elif not comp:
        brand["search_terms"] = name
    if m["pages"]:
        brand["page_ids"] = [p for p in re.split(r"[+,\s]+", m["pages"]) if p]
        brand["page_id_source"] = "user"
    if m["cc"]:
        brand["countries"] = parse_countries(m["cc"])
    return brand


def brand_countries(brand: dict, project: dict) -> list[str]:
    return list(brand.get("countries") or project["markets"])


def coverage(brand: dict, countries: list[str]) -> dict:
    """Route each country and say plainly what it gives. EU/UK -> ads_archive; else -> MCP (stub)."""
    archive = [c for c in countries if c in EU_UK]
    mcp = mcp_source.mcp_countries(countries)
    parts = []
    if archive:
        part = (f"{', '.join(archive)}: ads_archive (official API) gives full detail for commercial ads "
                f"delivered there in the past year: {ARCHIVE_DETAIL}. No spend, CTA button or images/video.")
        if not brand.get("page_ids"):
            part += " The Page ID is not resolved yet: run discover_pages.py (1 call) and confirm the Page."
        parts.append(part)
    if mcp:
        parts.append(
            f"{', '.join(mcp)}: outside the EU/UK, so ads_archive returns no commercial ads there. Meta's Ads MCP "
            f"(ads_library_search) can list {mcp_source.AVAILABLE}; it has no {mcp_source.NOT_AVAILABLE}. "
            f"That route is not connected in this stage ({mcp_source.NOT_CONNECTED}), so nothing is fetched "
            f"for {', '.join(mcp)}. Full text and visuals there need the screenshot checklist planned for stage 3.")
    return {"ads_archive": archive, "mcp": mcp, "plan": " ".join(parts)}


# --- load / save -----------------------------------------------------------------

def _ordered(doc: dict) -> dict:
    out = {k: doc[k] for k in FIELD_ORDER if k in doc}
    out["brands"] = [{k: b[k] for k in BRAND_ORDER if k in b} for b in doc["brands"]]
    return out


def finalize(doc: dict) -> dict:
    """Normalize countries, re-derive coverage, check cross-field rules, validate. Returns a new dict."""
    doc = json.loads(json.dumps(doc))
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc["markets"] = parse_countries(doc.get("markets") or [])
    if not doc["markets"]:
        raise ProjectError("markets must list at least one country (ISO-2)")
    keys = set()
    for b in doc.get("brands") or []:
        if b.get("countries") is not None:
            b["countries"] = parse_countries(b["countries"])
            if not b["countries"]:
                del b["countries"]
        b["page_ids"] = [str(p) for p in b.get("page_ids") or []]
        b["coverage"] = coverage(b, brand_countries(b, doc))
        if b.get("key") in keys:
            raise ProjectError(f"brand key {b.get('key')!r} appears twice")
        keys.add(b.get("key"))
    errors = sorted(validator().iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        raise ProjectError(f"project fails schema: {e.message} at {list(e.path)}")
    if doc["project"] in keys and doc["layout"] == "flat":
        raise ProjectError("with the flat layout the project slug must differ from every brand key")
    w = doc["window"]
    if "since" in w and w["until"] < w["since"]:
        raise ProjectError(f"window until {w['until']} is before since {w['since']}")
    output_root(doc)  # raises on a disallowed path
    return _ordered(doc)


def project_path(ref: str | Path, projects_dir: Path = PROJECTS_DIR) -> Path:
    p = Path(ref)
    if p.suffix in (".yaml", ".yml") or p.exists():
        return p
    return projects_dir / f"{ref}.yaml"


def load_project(ref: str | Path, projects_dir: Path = PROJECTS_DIR) -> dict:
    path = project_path(ref, projects_dir)
    if not path.exists():
        raise ProjectError(f"no project file {path}")
    doc = yaml.safe_load(path.read_text()) or {}
    return finalize(doc)


def dump(doc: dict) -> str:
    text = HEADER + yaml.safe_dump(_ordered(doc), sort_keys=False, allow_unicode=True, width=110)
    assert_no_token(text, "a project file")
    return text


def save_project(doc: dict, projects_dir: Path = PROJECTS_DIR) -> Path:
    doc = finalize(doc)
    path = projects_dir / f"{doc['project']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(dump(doc))
    os.replace(tmp, path)
    return path


def new_project(project: str, brand_specs: list[str], *, today: date, competitors: dict | None = None,
                title: str | None = None, **answers) -> dict:
    """A project from brand specs plus intake answers; unanswered fields take the recommended defaults."""
    competitors = load_competitors() if competitors is None else competitors
    if not brand_specs:
        raise ProjectError("a project needs at least one brand")
    doc = {"schema_version": SCHEMA_VERSION, "project": slug(project), "created": today.isoformat(),
           "intake_version": INTAKE_VERSION,
           "brands": [brand_from_spec(s, competitors) for s in brand_specs]}
    if title:
        doc["title"] = title
    for k, v in DEFAULTS.items():
        doc[k] = json.loads(json.dumps(answers[k] if answers.get(k) is not None else v))
    return finalize(doc)


# --- window, output root and run paths --------------------------------------------

def resolve_window(project: dict, today: date) -> tuple[str, str]:
    """(since, until) as YYYY-MM. {months: N} ends with the current month."""
    w = project["window"]
    if "since" in w:
        return w["since"], w["until"]
    first = date(today.year, today.month, 1)
    for _ in range(w["months"] - 1):
        first = date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)
    return first.strftime("%Y-%m"), today.strftime("%Y-%m")


def output_root(project: dict, reports_dir: Path = REPORTS_DIR) -> Path:
    """'reports' (or reports/<sub>) inside the repo, or an absolute path outside it.

    Anything else inside the repo is refused: only reports/ is git-ignored for Platform Data.
    """
    raw = str(project.get("output_root") or "reports")
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        parts = p.parts
        if not parts or parts[0] != "reports" or ".." in parts:
            raise ProjectError(f"output_root {raw!r}: use 'reports' or an absolute path outside the repo")
        return reports_dir.joinpath(*parts[1:])
    resolved = p.resolve()
    repo = ROOT.resolve()
    if resolved == repo or repo in resolved.parents:
        if not (resolved == (repo / "reports") or (repo / "reports") in resolved.parents):
            raise ProjectError(f"output_root {raw!r} is inside the repo but not under reports/ (not git-ignored)")
    return resolved


@dataclass
class RunPaths:
    """Where one dated run writes, per layout (C3: the user picks at intake)."""
    root: Path
    project: str
    date: str
    layout: str

    @property
    def project_dir(self) -> Path:
        return self.root / self.project

    @property
    def project_md(self) -> Path:
        return self.project_dir / "project.md"

    @property
    def run_dir(self) -> Path:
        if self.layout == "project/date/brand":
            return self.project_dir / self.date
        if self.layout == "brand/date":
            return self.project_dir / "runs" / self.date
        return self.project_dir

    def run_file(self, name: str) -> Path:
        return self.run_dir / (f"{self.date}-{name}" if self.layout == "flat" else name)

    @property
    def index(self) -> Path:
        return self.run_file("index.md")

    @property
    def comparison(self) -> Path:
        return self.run_file("comparison.md")

    @property
    def workbook(self) -> Path:
        return self.run_file("workbook.xlsx")

    @property
    def csv_dir(self) -> Path:
        return self.run_file("csv")

    @property
    def manifest(self) -> Path:
        return self.run_file("run.json")

    def critique(self, stage: str) -> Path:
        return self.run_file(f"critique-{stage}.json")

    def brand_dir(self, brand: str) -> Path:
        if self.layout == "project/date/brand":
            return self.run_dir / brand
        if self.layout == "brand/date":
            return self.project_dir / brand / self.date
        return self.root / brand

    def brand_report(self, brand: str) -> Path:
        if self.layout == "flat":
            return self.brand_dir(brand) / f"{self.date}-competitor-report.md"
        return self.brand_dir(brand) / "report.md"

    def brand_csv(self, brand: str) -> Path:
        if self.layout == "flat":
            return self.brand_dir(brand) / f"{self.date}-ads.csv"
        return self.brand_dir(brand) / "ads.csv"

    def latest_links(self, brands: list[str]) -> list[tuple[Path, str]]:
        """(symlink, relative target) pairs pointing `latest` at this run's dated folder."""
        if self.layout == "project/date/brand":
            return [(self.project_dir / "latest", self.date)]
        if self.layout == "brand/date":
            return [(self.project_dir / "runs" / "latest", self.date)] + [
                (self.project_dir / b / "latest", self.date) for b in brands]
        return []


def run_paths(project: dict, run_date: str, reports_dir: Path = REPORTS_DIR) -> RunPaths:
    return RunPaths(output_root(project, reports_dir), project["project"], run_date, project["layout"])


def point_latest(paths: RunPaths, brands: list[str]) -> None:
    for link, target in paths.latest_links(brands):
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() and not link.is_symlink():
            raise ProjectError(f"{link} exists and is not a symlink; refusing to replace it")
        tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
        tmp.unlink(missing_ok=True)
        os.symlink(target, tmp)
        os.replace(tmp, link)


# --- query plan ----------------------------------------------------------------------

def unresolved(project: dict) -> list[dict]:
    """Brands that need ads_archive but have no Page ID yet."""
    return [b for b in project["brands"] if b["coverage"]["ads_archive"] and not b["page_ids"]]


def archive_groups(project: dict) -> list[dict]:
    """Brands batched into ads_archive queries: same EU/UK country set, at most 10 Page IDs each.

    A brand is never split across groups. Brands without a Page ID are left out (see unresolved()).
    """
    by_countries: dict[tuple, list[dict]] = {}
    for b in project["brands"]:
        if b["coverage"]["ads_archive"] and b["page_ids"]:
            by_countries.setdefault(tuple(b["coverage"]["ads_archive"]), []).append(b)
    groups = []
    for countries, brands in by_countries.items():
        cur: list[dict] = []
        for b in brands:
            if len(b["page_ids"]) > MAX_PAGE_IDS_PER_QUERY:
                raise ProjectError(f"brand {b['key']} has more than {MAX_PAGE_IDS_PER_QUERY} Page IDs")
            if sum(len(x["page_ids"]) for x in cur) + len(b["page_ids"]) > MAX_PAGE_IDS_PER_QUERY:
                groups.append(cur)
                cur = []
            cur.append(b)
        if cur:
            groups.append(cur)
    out = []
    for i, brands in enumerate(groups, 1):
        page_map = {p: b["key"] for b in brands for p in b["page_ids"]}
        out.append({"group": i, "countries": list(brands[0]["coverage"]["ads_archive"]),
                    "brands": [b["key"] for b in brands], "page_ids": list(page_map), "page_map": page_map})
    return out


def mcp_plan(project: dict) -> list[dict]:
    return [r | {"brand": b["key"]} for b in project["brands"]
            for r in mcp_source.plan_requests(b, b["coverage"]["mcp"])]


def estimate_calls(project: dict, today: date, *, single_window: bool = False) -> dict:
    since, until = resolve_window(project, today)
    windows = 1 if single_window else len(month_windows(since, until))
    groups = archive_groups(project)
    return {"since": since, "until": until, "windows": windows, "groups": len(groups),
            "archive_queries": windows * len(groups), "discovery_calls": len(unresolved(project)),
            "token_check": 1, "mcp_calls_not_sent": len(mcp_plan(project))}


def months_in_window(project: dict, today: date) -> int:
    since, until = resolve_window(project, today)
    return len(month_windows(since, until))


def plan_screen(project: dict, today: date, guard=None, *, single_window: bool = False) -> str:
    """Round 5 plan screen (markdown): brands, routes, coverage, call estimate vs caps, retention."""
    est = estimate_calls(project, today, single_window=single_window)
    L = [f"# Plan: {project.get('title') or project['project']}", "",
         f"- Window: {est['since']} to {est['until']} ({months_in_window(project, today)} months); "
         f"goal: {project['goal']}; layout: {project['layout']}; output root: {project['output_root']}",
         "- Outputs: markdown" + "".join(f", {k}" for k in ("xlsx", "csv") if project["outputs"].get(k)), ""]
    later = [k for k in ("html", "drive") if project["outputs"].get(k)]
    if later:
        L += [f"- Saved for a later stage (not produced now): {', '.join(later)}", ""]
    L += ["| Brand | Page ID(s) | Countries | ads_archive (full detail) | Meta Ads MCP (discovery only, not connected) |",
          "|---|---|---|---|---|"]
    for b in project["brands"]:
        cov = b["coverage"]
        pages = ", ".join(b["page_ids"]) or "_unresolved_"
        if b["page_ids"] and b.get("page_id_source"):
            pages += f" ({b['page_id_source']})"
        L.append(f"| {b['name']} | {pages} | {', '.join(brand_countries(b, project))} "
                 f"| {', '.join(cov['ads_archive']) or '-'} | {', '.join(cov['mcp']) or '-'} |")
    L += ["", "## Coverage per brand", ""]
    L += [f"- **{b['name']}**: {b['coverage']['plan']}" for b in project["brands"]]
    L += ["", "## API budget", "",
          f"- ads_archive: {est['groups']} query group(s) x {est['windows']} window(s) = "
          f"**{est['archive_queries']} queries** (+ paging; up to 10 Page IDs share one query)",
          f"- Page discovery: {est['discovery_calls']} keyword call(s) (KEYWORD_EXACT_PHRASE, 1 per unresolved brand)",
          f"- Token check: at most {est['token_check']} debug_token call per day",
          f"- Meta Ads MCP: {est['mcp_calls_not_sent']} call(s) would be needed; none are sent (not connected)"]
    if guard is not None:
        c = guard.counts()
        L.append(f"- Guard caps: {guard.api['max_calls_per_run']} per run; {c['hour']}/{guard.api['max_calls_per_hour']} "
                 f"used this hour; {c['day']}/{guard.api['max_calls_per_day']} used in 24h")
    v, m = project.get("visuals") or {}, project.get("monitor") or {}
    L += ["", "## Saved preferences for later stages (nothing runs in this stage)", "",
          f"- Visuals: route {v.get('route', '-')}, last {v.get('window_days', '-')} days, max "
          f"{v.get('max_ads_per_brand', '-')} ads per brand, video {v.get('video', '-')} (stage 3)",
          f"- Monitoring: {m.get('cadence', '-')} at {m.get('time', '-')}, watch {', '.join(m.get('watch') or []) or '-'}, "
          f"alerts {', '.join(m.get('alerts') or []) or '-'} (stage 4; nothing is scheduled)",
          "", "## Retention", "",
          "- Raw pulls and LLM batches 180 days, media 90 days, reports kept (scripts/purge_expired.py)."]
    if project.get("critic"):
        c = project["critic"]
        L += ["", f"Last critic verdict: **{c.get('last_verdict')}** ({c.get('stage')}, {c.get('at')})"]
    return "\n".join(L) + "\n"


# --- CLI ------------------------------------------------------------------------------

def _answers(args) -> dict:
    a: dict = {}
    if args.markets:
        a["markets"] = parse_countries(args.markets)
    if args.months or args.since:
        if args.since and not args.until:
            raise ProjectError("--since needs --until")
        a["window"] = {"since": args.since, "until": args.until} if args.since else {"months": args.months}
    for k in ("goal", "layout", "output_root"):
        if getattr(args, k):
            a[k] = getattr(args, k)
    if args.outputs:
        chosen = {o.strip() for o in args.outputs.split(",") if o.strip()}
        bad = chosen - {"markdown", "xlsx", "csv", "html", "drive"}
        if bad:
            raise ProjectError(f"unknown output(s) {sorted(bad)}")
        a["outputs"] = {"markdown": True, **{k: k in chosen for k in ("xlsx", "csv", "html", "drive")}}
    if args.visuals:
        a["visuals"] = {**DEFAULTS["visuals"], "route": args.visuals}
    if args.monitor:
        a["monitor"] = {**DEFAULTS["monitor"], "cadence": args.monitor}
    return a


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="research project files (config/projects/<slug>.yaml)")
    ap.add_argument("--projects-dir", type=Path, default=PROJECTS_DIR)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--today", help="YYYY-MM-DD (default: today, UTC)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="write a project from brand specs + answers (defaults fill the rest)")
    n.add_argument("project")
    n.add_argument("--brand", action="append", required=True, help="NAME_OR_KEY[:CC+CC][@PAGE_ID] (repeatable)")
    n.add_argument("--title")
    n.add_argument("--defaults", action="store_true", help="use the recommended defaults for every unanswered field")
    n.add_argument("--markets", help="default markets, comma list of ISO-2 codes (default GB)")
    n.add_argument("--months", type=int)
    n.add_argument("--since")
    n.add_argument("--until")
    n.add_argument("--goal", choices=GOALS)
    n.add_argument("--layout", choices=LAYOUTS)
    n.add_argument("--output-root")
    n.add_argument("--outputs", help="comma list of markdown,xlsx,csv,html,drive (markdown is always written)")
    n.add_argument("--visuals", choices=("inbox", "none"))
    n.add_argument("--monitor", choices=("daily+weekly-digest", "weekly", "daily-digest", "none"))
    n.add_argument("--force", action="store_true", help="overwrite an existing project file")
    for name in ("plan", "show", "validate"):
        p = sub.add_parser(name)
        p.add_argument("project")
        if name == "plan":
            p.add_argument("--single-window", action="store_true")
    sp = sub.add_parser("set-page")
    sp.add_argument("project")
    sp.add_argument("brand")
    sp.add_argument("page_ids", help="comma list of numeric Page IDs (max 10)")
    sp.add_argument("--source", choices=("discovery", "user"), default="user")
    sc = sub.add_parser("set-countries")
    sc.add_argument("project")
    sc.add_argument("brand")
    sc.add_argument("countries", help="comma list of ISO-2 codes, or 'default' for the project markets")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else utc_now().date()
    try:
        if args.cmd == "new":
            path = args.projects_dir / f"{slug(args.project)}.yaml"
            if path.exists() and not args.force:
                raise ProjectError(f"{path} exists; pass --force to overwrite, or edit it with set-page/set-countries")
            doc = new_project(args.project, args.brand, today=today, title=args.title, **_answers(args))
            print(save_project(doc, args.projects_dir))
            return 0
        doc = load_project(args.project, args.projects_dir)
        if args.cmd == "validate":
            print(f"ok: {project_path(args.project, args.projects_dir)} ({len(doc['brands'])} brands)")
        elif args.cmd == "show":
            print(dump(doc), end="")
        elif args.cmd == "plan":
            from guard import Guard
            print(plan_screen(doc, today, Guard(args.data_dir), single_window=args.single_window), end="")
        elif args.cmd in ("set-page", "set-countries"):
            brand = next((b for b in doc["brands"] if b["key"] == args.brand), None)
            if brand is None:
                raise ProjectError(f"no brand {args.brand!r} in {doc['project']}")
            if args.cmd == "set-page":
                brand["page_ids"] = [p.strip() for p in args.page_ids.split(",") if p.strip()]
                brand["page_id_source"] = args.source
            elif args.countries == "default":
                brand.pop("countries", None)
            else:
                brand["countries"] = parse_countries(args.countries)
            print(save_project(doc, args.projects_dir))
        return 0
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
