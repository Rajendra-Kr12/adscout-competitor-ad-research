#!/usr/bin/env python3
"""Critic gates for research projects: deterministic checks, validation, and recording verdicts.

The research-critic sub-agent (.claude/agents/research-critic.md, prompts/critic.md) is the judge, but
some failures must never depend on an LLM noticing them. `check` computes those findings (source
"check") for one gate; the critic starts from them, adds its own (source "critic"), and may not drop
or soften a check blocker. `record` enforces that, then stores the verdict in the run folder, the
project file and the run's index.md.

Gates:
  plan    coverage per brand and country (EU/UK vs Meta Ads MCP route), config market mismatches,
          unresolved/unverified Page IDs, windows over 12 months, call estimate vs guard caps,
          visuals route compliance, outputs not built yet, contradictory preferences
  data    stopped runs, brands with 0 ads, other Pages' ads (keyword pollution), creative-only share,
          reach missing everywhere, near-duplicate share, LLM coverage
  report  spend/CPM/CPA/ROAS claims, non-permalink or token-bearing links, unknown cited library IDs,
          narrative numbers not in the tables, non-EU/UK claims that imply full detail, SYNTHETIC banner

Usage:
  python scripts/critique.py check <project> --stage plan|data|report [--date YYYY-MM-DD]
  python scripts/critique.py validate <critique.json>
  python scripts/critique.py record <critique.json> <project> [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

import jsonschema

import build_report
import project as projects
from common import DATA_DIR, EU_UK, REPORTS_DIR, ROOT, iso_z, load_competitors, utc_now, write_json

CRITIQUE_SCHEMA_PATH = ROOT / "schemas" / "critique.schema.json"
PROMPT_VERSION = "critic-v1"
STAGES = ("plan", "data", "report")
CRITIC_START = "<!-- critic:start -->"
CRITIC_END = "<!-- critic:end -->"
CRITIC_PLACEHOLDER = ("_Not reviewed yet. The research-critic gates (plan, data, report) record their verdicts "
                      "here through `scripts/critique.py record`._")

SPEND_RE = re.compile(r"\b(spend|spends|spent|spending|budgets?|cpm|cpa|cpc|roas|cost per \w+|impressions|"
                      r"ad dollars|media buy)\b", re.IGNORECASE)
NEGATION_RE = re.compile(r"\b(no|not|never|without|unavailable|isn't|aren't|doesn't|don't|cannot|can't|missing|"
                         r"lacks?|only|none)\b", re.IGNORECASE)
FULL_DETAIL_RE = re.compile(r"\b(primary text|body (?:copy|text)|full (?:text|copy)|ad copy|copy|cta|call to action|"
                            r"buttons?|reach|targeting|demographics?|age|gender|stop(?:ped)? dates?|ran for|"
                            r"days running|long-running|images?|visuals?|videos?|creatives?|landing pages?|"
                            r"descriptions?|captions?)\b", re.IGNORECASE)
PERMALINK_RE = re.compile(r"^https://www\.facebook\.com/ads/library/\?id=(\d+|<library_id)$")  # or the documented template
URL_RE = re.compile(r"https?://[^\s)\]>\"'|]+", re.IGNORECASE)
LIBRARY_ID_RE = re.compile(r"(?<!\d)\d{15,}(?!\d)")
NUMBER_RE = re.compile(r"(?<![\w.,-])(\d{1,3}(?:,\d{3})+|\d{2,})(?:\.\d+)?%?(?![\w-])")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
COUNTRY_NAMES = {"IN": ["India", "Indian"], "US": ["United States", "USA", "US", "American"], "AU": ["Australia"],
                 "CA": ["Canada"], "AE": ["UAE", "Emirates"], "SG": ["Singapore"], "JP": ["Japan"],
                 "BR": ["Brazil"], "MX": ["Mexico"], "ZA": ["South Africa"], "NZ": ["New Zealand"],
                 "CH": ["Switzerland", "Swiss"], "NO": ["Norway"], "SA": ["Saudi"], "PK": ["Pakistan"],
                 "BD": ["Bangladesh"], "LK": ["Sri Lanka"], "NG": ["Nigeria"], "KE": ["Kenya"],
                 "ID": ["Indonesia"], "MY": ["Malaysia"], "PH": ["Philippines"], "TR": ["Turkey", "Türkiye"]}


@lru_cache(maxsize=1)
def validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(CRITIQUE_SCHEMA_PATH.read_text()))


def validate(doc: dict) -> list[str]:
    """Schema errors plus the rules JSON Schema can't express."""
    errs = [f"{e.message} at {list(e.path)}" for e in validator().iter_errors(doc)]
    if errs:
        return errs
    ids = [f["id"] for f in doc["findings"]]
    if len(ids) != len(set(ids)):
        errs.append("finding ids must be unique")
    for f in doc["findings"]:
        a = f.get("ask_user")
        if a and a["recommended"] not in a["options"]:
            errs.append(f"{f['id']}: ask_user.recommended must be one of its options")
    sev = {f["severity"] for f in doc["findings"]}
    if "blocker" in sev and doc["verdict"] == "approve":
        errs.append("verdict cannot be approve while a blocker finding is open")
    return errs


def verdict_for(findings: list[dict]) -> str:
    sev = {f["severity"] for f in findings}
    return "block" if "blocker" in sev else "revise" if "major" in sev else "approve"


class Findings:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, severity: str, category: str, claim: str, evidence: str, reasoning: str, fix: str,
            brands: list[str] | None = None, ask_user: dict | None = None) -> None:
        f = {"id": f"D{len(self.items) + 1}", "severity": severity, "category": category, "claim": claim,
             "evidence": evidence, "reasoning": reasoning, "fix": fix, "source": "check"}
        if brands:
            f["brands"] = brands
        if ask_user:
            f["ask_user"] = ask_user
        self.items.append(f)


# --- plan gate ------------------------------------------------------------------------------

def plan_checks(project: dict, today: date, guard=None, competitors: dict | None = None) -> list[dict]:
    competitors = load_competitors() if competitors is None else competitors
    out = Findings()
    src = f"config/projects/{project['project']}.yaml"
    for b in project["brands"]:
        countries = projects.brand_countries(b, project)
        cov = b["coverage"]
        if cov["mcp"] and not cov["ads_archive"]:
            out.add("major", "coverage",
                    f"{b['name']} is only on markets outside the EU/UK ({', '.join(cov['mcp'])}); ads_archive returns "
                    "no commercial ads there and the Meta Ads MCP route is not connected, so nothing will be fetched.",
                    f"{src}: brands[{b['key']}].coverage.mcp = {cov['mcp']}",
                    "ads_archive only holds commercial ads delivered to the EU/UK. Meta's Ads MCP could list live ads "
                    "there, but only headline, start date and permalink, and it needs the captain's login. A report "
                    "for this brand would therefore be empty, and must not read as 'no ads'.",
                    "Keep it as an explained coverage probe, drop it, or add an EU/UK market it delivers to.",
                    [b["key"]],
                    {"question": f"{b['name']} only targets {', '.join(cov['mcp'])}, where the official API has no "
                                 "commercial ads and the Meta Ads MCP route isn't connected yet. What should I do?",
                     "options": ["Keep it as a coverage probe (0 ads, explained)", "Drop it from this project",
                                 "Also check its UK presence (add GB)"],
                     "recommended": "Keep it as a coverage probe (0 ads, explained)"})
        elif cov["mcp"]:
            out.add("minor", "coverage",
                    f"{b['name']}: {', '.join(cov['mcp'])} will not be fetched; only {', '.join(cov['ads_archive'])} "
                    "has full detail.", f"{src}: brands[{b['key']}].coverage",
                    "The non-EU/UK part of this brand needs the Meta Ads MCP route (discovery only, not connected). "
                    "Its report covers the EU/UK delivery only.",
                    "Say so in the report caveats; no change needed.", [b["key"]])
        home = [str(c).upper() for c in (competitors.get(b["key"]) or {}).get("countries") or []]
        if home and not set(home) & set(countries):
            out.add("major", "coverage",
                    f"config/competitors.yaml lists {b['name']}'s market as {', '.join(home)}, but this project "
                    f"queries {', '.join(countries)}.",
                    f"config/competitors.yaml: {b['key']}.countries = {home}",
                    "Querying a market the brand may not deliver to will likely return 0 ads, which is easy to "
                    "misread as 'the brand runs no ads'.",
                    f"Add {', '.join(home)} for this brand, or keep {', '.join(countries)} as a deliberate probe.",
                    [b["key"]],
                    {"question": f"{b['name']} is listed as a {', '.join(home)} brand, but you picked "
                                 f"{', '.join(countries)}. Which markets should I use for it?",
                     "options": [f"Add {', '.join(home)} as well", f"Keep {', '.join(countries)} only (probe)",
                                 f"Use {', '.join(home)} only"],
                     "recommended": f"Add {', '.join(home)} as well"})
        if cov["ads_archive"] and not b["page_ids"]:
            out.add("major", "identity", f"{b['name']} has no Page ID, so it will not be fetched.",
                    f"{src}: brands[{b['key']}].page_ids = []",
                    "Brands are queried by Page ID; a keyword query returns other Pages' ads too, so guessing is "
                    "not safe. Discovery costs one call.",
                    f"python scripts/discover_pages.py --project {project['project']}, then project.py set-page.",
                    [b["key"]],
                    {"question": f"Which Facebook Page is '{b['name']}'? I can look it up with one API call.",
                     "options": ["Look it up now (1 call)", "I'll paste the Page ID or URL", "Drop this brand"],
                     "recommended": "Look it up now (1 call)"})
        elif b["page_ids"] and b.get("page_id_source") == "config" and \
                not (competitors.get(b["key"]) or {}).get("page_id_verified"):
            out.add("minor", "identity", f"{b['name']}'s Page ID comes from config and is not verified.",
                    f"config/competitors.yaml: {b['key']}.page_id_verified = false",
                    "A wrong Page ID returns another advertiser's ads or none at all.",
                    "Check the Page name in the first run's results; set page_id_verified once confirmed.", [b["key"]])
    months = projects.months_in_window(project, today)
    if months > 12:
        out.add("major", "method", f"The window is {months} months; commercial ads older than a year are not in the API.",
                f"{src}: window = {json.dumps(project['window'])}",
                "ads_archive keeps commercial ads for one year after delivery, so the earliest months will be "
                "partial and trends across them would be wrong.",
                "Use 12 months or less, or label the early months as partial.", None,
                {"question": f"You picked {months} months, but commercial ads older than a year aren't in the API. "
                             "Keep it or shorten it?",
                 "options": ["Use 12 months", f"Keep {months} months (early months partial)", "Use 6 months"],
                 "recommended": "Use 12 months"})
    est = projects.estimate_calls(project, today)
    planned = est["archive_queries"] + est["discovery_calls"] + est["token_check"]
    if guard is not None:
        c = guard.counts()
        cap, hour_left = guard.api["max_calls_per_run"], guard.api["max_calls_per_hour"] - c["hour"]
        day_left = guard.api["max_calls_per_day"] - c["day"]
        if est["archive_queries"] > cap:
            out.add("blocker", "budget", f"About {est['archive_queries']} queries exceed the per-run cap of {cap}.",
                    f"config/guard.yaml api.max_calls_per_run = {cap}",
                    "The guard would stop the run part-way, leaving brands unfetched.",
                    "Shorten the window, use --single-window, or split the project.")
        elif planned > min(hour_left, day_left):
            out.add("major", "budget", f"About {planned} calls planned but only {min(hour_left, day_left)} left "
                    "under the hourly/daily caps.", f"guard counts: {c['hour']} this hour, {c['day']} in 24h",
                    "The guard would refuse calls part-way through the run.", "Run later, or with --single-window.")
    v = project.get("visuals") or {}
    if v.get("route") == "snapshot":
        out.add("blocker", "compliance", "Visuals route 'snapshot' (scripted fetching of ad snapshots) is not approved.",
                f"{src}: visuals.route = snapshot",
                "Per-ad snapshot fetching by script is a grey zone under Meta Terms §3.2 and needs the captain's "
                "written opt-in (plan §3.2-3.3); a scheduled version would be batch download, which is prohibited.",
                "Use 'inbox' (human screenshots) or 'none'.")
    later = [k for k in ("html", "drive") if project["outputs"].get(k)]
    if later:
        out.add("minor", "method", f"Requested output(s) {', '.join(later)} are not built yet; they are saved as a preference.",
                f"{src}: outputs", "The user should not expect these files from this run.",
                "Mention it on the plan screen.")
    m = project.get("monitor") or {}
    if m.get("cadence") == "none" and m.get("alerts"):
        out.add("minor", "method", "Alerts are set but monitoring is off.", f"{src}: monitor",
                "Alerts only come from monitoring runs, so these would never fire.", "Turn monitoring on or clear the alerts.")
    return out.items


# --- data gate ---------------------------------------------------------------------------------

def data_checks(manifest: dict, data_dir: Path, project: dict) -> list[dict]:
    out = Findings()
    for g in manifest["groups"]:
        if g["status"] != "ok":
            out.add("blocker", "data-quality", f"Query group {g['group']} ({', '.join(g['brands'])}) stopped: "
                    f"{g['stop_reason']}", f"data/{g['ledger']}",
                    "Its raw run has no summary line, so these brands' numbers are incomplete or from earlier runs.",
                    "Wait until the guard allows a new run (never retry by hand), then re-run the project.",
                    g["brands"])
    by_key = {b["key"]: b for b in project["brands"]}
    for e in manifest["brands"]:
        s, key = e["stats"], e["key"]
        if e["routes"]["ads_archive"] and e["status"] in ("ok", "rebuilt from stored data") and s["ads_in_window"] == 0:
            out.add("major", "data-quality", f"{e['name']} returned 0 ads for {', '.join(e['routes']['ads_archive'])}.",
                    f"run.json brands[{key}].stats.ads_in_window = 0",
                    "Either the brand did not deliver commercial ads there in the window, or the Page ID is wrong. "
                    "An empty section is easy to misread as 'runs no ads'.",
                    "Confirm the Page ID (discover_pages.py) and say in the report that 0 is a coverage result.", [key],
                    {"question": f"{e['name']} returned no ads in {', '.join(e['routes']['ads_archive'])}. What next?",
                     "options": ["Keep it, explained as a coverage result", "Re-check its Page ID (1 call)",
                                 "Drop it from the comparison"],
                     "recommended": "Keep it, explained as a coverage result"})
        others = sorted(set(s["pages_seen"]) - set((by_key.get(key) or {}).get("page_ids") or []))
        if others:
            out.add("major", "identity", f"{e['name']}'s ads include other Pages: {', '.join(others)}.",
                    f"run.json brands[{key}].stats.pages_seen", "Mixed Pages distort every per-brand number.",
                    "Keep only the brand's Page IDs (normalize --only-page-id) or add the Pages to the brand.", [key])
        n = s["ads_in_window"]
        if n and s["creative_only"]:
            out.add("minor", "data-quality", f"{e['name']}: {s['creative_only']}/{n} ads have no text (creative-only).",
                    f"run.json brands[{key}].stats.creative_only",
                    "They are labelled, not dropped; text analysis cannot cover them until visuals exist (stage 3).",
                    "Mention the share in the caveats.", [key])
        if n and not s["uk_reach_ads"] and not s["eu_reach_ads"]:
            out.add("minor", "data-quality", f"{e['name']}: no reach figure on any ad.",
                    f"run.json brands[{key}].stats.uk_reach_ads = 0, eu_reach_ads = 0",
                    "Reach-based statements are impossible for this brand.", "Avoid reach comparisons for it.", [key])
        if n >= 4 and s["near_duplicates"] / n > 0.5:
            out.add("minor", "data-quality", f"{e['name']}: {s['near_duplicates']}/{n} ads repeat earlier copy.",
                    f"run.json brands[{key}].stats.near_duplicates",
                    "Counts of 'new' messages overstate variety when most ads reuse copy.",
                    "Count unique messages, not ads, in the narrative.", [key])
    return out.items


# --- report gate -------------------------------------------------------------------------------

def narrative(text: str) -> str:
    s, e = build_report.NARRATIVE_START, build_report.NARRATIVE_END
    return text.split(s, 1)[1].split(e, 1)[0] if s in text and e in text else ""


def outside_narrative(text: str) -> str:
    s, e = build_report.NARRATIVE_START, build_report.NARRATIVE_END
    return text.split(s, 1)[0] + text.split(e, 1)[1] if s in text and e in text else text


def sentences(text: str) -> list[str]:
    return [x.strip() for x in SENTENCE_SPLIT.split(text) if x.strip()]


def _mentions(sentence: str, words: list[str]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", sentence, re.IGNORECASE if len(w) > 2 else 0)
               for w in words if w)


def report_files(manifest: dict, paths: projects.RunPaths) -> list[tuple[str, Path, list[str]]]:
    """(label, path, brand keys) for every written report of the run."""
    out = [("index", paths.index, []), ("comparison", paths.comparison, [b["key"] for b in manifest["brands"]])]
    out += [(b["key"], paths.root / b["report"], [b["key"]]) for b in manifest["brands"]]
    return [(l, p, k) for l, p, k in out if p.exists()]


def report_checks(manifest: dict, paths: projects.RunPaths, data_dir: Path) -> list[dict]:
    out = Findings()
    entries = {b["key"]: b for b in manifest["brands"]}
    known_ids = {a["library_id"] for k in entries for a in build_report.load(data_dir, k)}
    fixture = manifest["mode"] == "fixture" or any("fixture" in b["stats"]["coverage_basis"] for b in entries.values())
    for k, e in entries.items():
        n, done = e["stats"]["ads_in_window"], e["stats"]["llm_coverage"]
        if n and done < n:
            out.add("minor", "method", f"{e['name']}: LLM extraction covers {done}/{n} ads.",
                    f"run.json brands[{k}].stats.llm_coverage",
                    "Messaging and offer findings only describe the extracted ads.",
                    "Run ad-extractor for the rest, or state the coverage in the notes.", [k])
    for label, path, keys in report_files(manifest, paths):
        text = path.read_text()
        rel = path.relative_to(paths.root)
        for url in sorted(set(URL_RE.findall(text))):
            host_fb = re.search(r"facebook\.com|fbcdn\.net|fb\.com", url, re.IGNORECASE)
            if "access_token" in url.lower() or (host_fb and not PERMALINK_RE.match(url.rstrip(".,;"))):
                out.add("blocker", "compliance", f"{rel} links to a non-permalink Meta URL.", f"{rel}: {url[:80]}",
                        "Outputs may cite ads only as https://www.facebook.com/ads/library/?id=<id>; snapshot and CDN "
                        "URLs can carry the token or Platform media.", "Replace it with the public permalink.", keys or None)
        needs_banner = fixture if label in ("index", "comparison") else \
            "fixture" in entries[label]["stats"]["coverage_basis"]
        if needs_banner and "SYNTHETIC DATA" not in text:
            out.add("blocker", "claim", f"{rel} is built from fixture data but has no SYNTHETIC DATA banner.", str(rel),
                    "A synthetic report without its banner can be mistaken for real competitor data.",
                    "Rebuild it with run_project.py --no-fetch, or add the banner.", keys or None)
        story = narrative(text)
        if not story:
            continue
        rest = outside_narrative(text) + (paths.index.read_text() if label == "comparison" and paths.index.exists() else "")
        for sent in sentences(story):
            if SPEND_RE.search(sent) and not NEGATION_RE.search(sent):
                out.add("blocker", "claim", f"{rel} makes a spend/efficiency claim: \"{sent[:120]}\"",
                        f"{rel} (analyst notes)",
                        "Competitor spend, impressions, CPM, CPA and ROAS are not available from any official source "
                        "for commercial ads, so any such statement is invented.",
                        "Delete the claim, or replace it with an observable proxy labelled as such (e.g. days running).",
                        keys or None)
        for lid in sorted(set(LIBRARY_ID_RE.findall(story)) - known_ids):
            out.add("major", "claim", f"{rel} cites library ID {lid}, which is not in this run's data.",
                    f"{rel} (analyst notes)", "A citation that does not resolve to a fetched ad cannot be checked.",
                    "Cite only ads from data/ads/<brand>/ for this run.", keys or None)
        for num in sorted(set(NUMBER_RE.findall(story))):
            plain = num.replace(",", "")
            if len(plain) >= 15 or re.fullmatch(r"20\d\d", plain):
                continue
            if num not in rest and plain not in rest.replace(",", ""):
                out.add("major", "claim", f"{rel}: the number {num} in the notes is not in the report's tables.",
                        f"{rel} (analyst notes)", "Every number in the narrative must come from the deterministic tables.",
                        "Use the tables' figure, or show how it is counted.", keys or None)
        for k in keys:
            e = entries[k]
            mcp = e["routes"]["mcp"]
            if not mcp:
                continue
            words = [c for c in mcp] + [n for c in mcp for n in COUNTRY_NAMES.get(c, [])]
            if not e["routes"]["ads_archive"]:
                words.append(e["name"])
            for sent in sentences(story):
                if _mentions(sent, words) and FULL_DETAIL_RE.search(sent) and not NEGATION_RE.search(sent):
                    out.add("blocker", "claim",
                            f"{rel} implies full ad detail for {e['name']} outside the EU/UK: \"{sent[:120]}\"",
                            f"run.json brands[{k}].routes.mcp = {mcp}; {rel} (analyst notes)",
                            f"No full-detail data exists for {', '.join(mcp)}: ads_archive has no commercial ads there, "
                            "and the Meta Ads MCP route (not connected) would give only headlines, start dates and "
                            "permalinks. Text, CTA, reach, targeting, stop dates and visuals are unknown.",
                            "Remove the statement, or restate it as unknown / pending the screenshot checklist.", [k])
    return out.items


# --- documents, recording and rendering -------------------------------------------------------

def checks_doc(stage: str, project: dict, findings: list[dict]) -> dict:
    v = verdict_for(findings)
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("blocker", "major", "minor")}
    return {"prompt_version": PROMPT_VERSION, "stage": stage, "verdict": v, "project": project["project"],
            "summary": f"Deterministic {stage}-gate checks: {counts['blocker']} blocker, {counts['major']} major, "
                       f"{counts['minor']} minor. The research-critic adds its own judgement on top.",
            "findings": findings}


def checks_path(paths: projects.RunPaths, stage: str) -> Path:
    return paths.run_file(f"checks-{stage}.json")


def run_checks(stage: str, project: dict, paths: projects.RunPaths, data_dir: Path, today: date, guard=None) -> dict:
    if stage == "plan":
        findings = plan_checks(project, today, guard)
    else:
        if not paths.manifest.exists():
            raise projects.ProjectError(f"no run manifest {paths.manifest}; run scripts/run_project.py first")
        manifest = json.loads(paths.manifest.read_text())
        findings = data_checks(manifest, data_dir, project) if stage == "data" else report_checks(manifest, paths, data_dir)
    doc = checks_doc(stage, project, findings)
    errs = validate(doc)
    if errs:
        raise RuntimeError(f"internal: check output fails the critique schema: {errs[0]}")
    return doc


def missing_check_blockers(critique: dict, checks: dict | None) -> list[str]:
    if not checks:
        return []
    kept = {f["id"]: f for f in critique["findings"]}
    return [f["id"] for f in checks["findings"]
            if f["severity"] == "blocker" and (f["id"] not in kept or kept[f["id"]]["severity"] != "blocker")]


def render_block(paths: projects.RunPaths) -> str:
    parts = []
    for stage in STAGES:
        f = paths.critique(stage)
        if not f.exists():
            continue
        doc = json.loads(f.read_text())
        parts += [f"**{stage.title()} gate: {doc['verdict']}** ({doc['prompt_version']}). {doc['summary']}", ""]
        if doc["findings"]:
            parts += ["| ID | Severity | Category | Finding | Fix | Ask the user |", "|---|---|---|---|---|---|"]
            for x in doc["findings"]:
                ask = x.get("ask_user")
                parts.append(f"| {x['id']} | {x['severity']} | {x['category']} | {build_report.cell(x['claim'])} "
                             f"| {build_report.cell(x['fix'])} | "
                             f"{build_report.cell(ask['question']) + ' (recommended: ' + build_report.cell(ask['recommended']) + ')' if ask else ''} |")
            parts.append("")
    return "\n".join(parts).strip() or CRITIC_PLACEHOLDER


def refresh_index(paths: projects.RunPaths) -> None:
    if not paths.index.exists():
        return
    text = paths.index.read_text()
    if CRITIC_START in text and CRITIC_END in text:
        head, rest = text.split(CRITIC_START, 1)
        text = head + CRITIC_START + "\n" + render_block(paths) + "\n" + CRITIC_END + rest.split(CRITIC_END, 1)[1]
        build_report.write_report(paths.index, text, blocks=())


def record(critique: dict, project: dict, paths: projects.RunPaths, projects_dir: Path) -> Path:
    errs = validate(critique)
    if errs:
        raise projects.ProjectError(f"critique is invalid: {errs[0]}")
    cp = checks_path(paths, critique["stage"])
    checks = json.loads(cp.read_text()) if cp.exists() else None
    missing = missing_check_blockers(critique, checks)
    if missing:
        raise projects.ProjectError(f"the critique drops or softens check blocker(s) {', '.join(missing)} from {cp.name}; "
                                    "a check blocker can only be resolved by fixing it and re-running the check")
    out = paths.critique(critique["stage"])
    write_json(out, critique)
    project = dict(project, critic={"last_verdict": critique["verdict"], "stage": critique["stage"],
                                    "at": iso_z(utc_now()), "prompt_version": critique["prompt_version"]})
    projects.save_project(project, projects_dir)
    refresh_index(paths)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="research-critic gates: check, validate, record")
    ap.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("project")
    c.add_argument("--stage", choices=STAGES, required=True)
    c.add_argument("--date", help="run date YYYY-MM-DD (default: today, UTC)")
    v = sub.add_parser("validate")
    v.add_argument("file", type=Path)
    r = sub.add_parser("record")
    r.add_argument("file", type=Path)
    r.add_argument("project")
    r.add_argument("--date")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "validate":
            errs = validate(json.loads(args.file.read_text()))
            for e in errs:
                print(f"invalid: {e}", file=sys.stderr)
            if not errs:
                print(f"ok: {args.file}")
            return 1 if errs else 0
        today = date.fromisoformat(args.date) if args.date else utc_now().date()
        project = projects.load_project(args.project, args.projects_dir)
        paths = projects.run_paths(project, today.isoformat(), args.reports_dir)
        if args.cmd == "check":
            from guard import Guard
            doc = run_checks(args.stage, project, paths, args.data_dir, today, Guard(args.data_dir))
            write_json(checks_path(paths, args.stage), doc)
            print(json.dumps(doc, indent=2, ensure_ascii=False))
            print(f"# {checks_path(paths, args.stage)}: verdict {doc['verdict']}", file=sys.stderr)
            return 0
        print(record(json.loads(args.file.read_text()), project, paths, args.projects_dir))
        return 0
    except projects.ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
