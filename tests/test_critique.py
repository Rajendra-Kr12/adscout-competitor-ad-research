"""P1-07: master intake defaults, critic schema, deterministic gate checks and recording."""
import json
from datetime import date

import pytest
import yaml

import build_report
import critique
import project as projects
from conftest import IN_PAGE, MULTI_PAGE, SYNTHETIC, UK_PAGE
from guard import Guard, load_config
from test_project import COMPETITORS, TODAY, make, run
from test_project import Throttled


def ids(doc, **match):
    return [f for f in doc["findings"] if all(f.get(k) == v for k, v in match.items())]


def paths_for(tmp_path):
    project = projects.load_project("synth", tmp_path / "projects")
    return project, projects.run_paths(project, TODAY.isoformat(), tmp_path / "reports")


def checks(tmp_path, stage, guard=None):
    project, paths = paths_for(tmp_path)
    doc = critique.run_checks(stage, project, paths, tmp_path / "data", TODAY, guard)
    assert critique.validate(doc) == []
    return doc


def seed_notes(path, notes):
    text = path.read_text()
    s, e = build_report.NARRATIVE_START, build_report.NARRATIVE_END
    path.write_text(text.split(s)[0] + s + "\n" + notes + "\n" + e + text.split(e)[1])


def fixture_project(tmp_path, specs):
    make(tmp_path, specs, window={"since": "2026-01", "until": "2026-09"})
    assert run(tmp_path, "--fixture", str(SYNTHETIC)) == 0
    return tmp_path / "reports" / "synth" / "2026-09-29"


# --- intake fast path --------------------------------------------------------------------------

def test_use_defaults_fast_path_writes_a_valid_project(tmp_path, capsys):
    assert projects.main(["--projects-dir", str(tmp_path / "projects"), "--today", TODAY.isoformat(), "new", "synth",
                          "--brand", f"Synthetic UK:GB@{UK_PAGE}", "--brand", "kapiva:IN", "--defaults"]) == 0
    doc = yaml.safe_load((tmp_path / "projects" / "synth.yaml").read_text())
    assert not list(projects.validator().iter_errors(doc))
    assert doc["intake_version"] == "intake-v1" and doc["goal"] == "messaging-creative"
    assert doc["visuals"]["route"] == "inbox" and doc["monitor"]["cadence"] == "daily+weekly-digest"
    assert run(tmp_path, "--dry-run") == 0
    assert "6 ads_archive queries" in capsys.readouterr().err


# --- schema ----------------------------------------------------------------------------------------

GOOD = {"prompt_version": "critic-v1", "stage": "plan", "verdict": "revise", "summary": "One gap.",
        "findings": [{"id": "F1", "severity": "major", "category": "coverage", "claim": "x", "evidence": "y",
                      "reasoning": "z", "fix": "w", "source": "critic",
                      "ask_user": {"question": "q?", "options": ["a", "b"], "recommended": "a"}}]}


def test_critique_schema_and_semantic_rules():
    assert critique.validate(GOOD) == []
    bad = json.loads(json.dumps(GOOD))
    bad["findings"][0]["ask_user"]["recommended"] = "c"
    assert any("recommended" in e for e in critique.validate(bad))
    bad = json.loads(json.dumps(GOOD))
    bad["findings"][0]["severity"], bad["verdict"] = "blocker", "approve"
    assert any("blocker" in e for e in critique.validate(bad))
    bad = json.loads(json.dumps(GOOD))
    del bad["findings"][0]["reasoning"]
    assert critique.validate(bad)


# --- plan gate ------------------------------------------------------------------------------------

def test_india_only_brand_gets_a_coverage_finding_with_ask_user(tmp_path):
    make(tmp_path, [f"Synthetic UK:GB@{UK_PAGE}", "kapiva:IN"])
    doc = checks(tmp_path, "plan")
    (f,) = ids(doc, category="coverage", brands=["kapiva"])
    assert f["severity"] == "major" and f["ask_user"]["recommended"] in f["ask_user"]["options"]
    assert "not connected" in f["claim"] and doc["verdict"] == "revise"


def test_plan_flags_market_mismatch_unresolved_page_long_window_and_snapshot(tmp_path):
    path = make(tmp_path, ["kapiva", "Pukka Herbs"], window={"months": 14})
    doc = yaml.safe_load(path.read_text())
    doc["visuals"]["route"] = "snapshot"
    path.write_text(yaml.safe_dump(doc))
    found = {(f["category"], f["severity"]) for f in checks(tmp_path, "plan")["findings"]}
    assert ("coverage", "major") in found       # kapiva's config market is IN, project queries GB
    assert ("identity", "major") in found       # Pukka Herbs has no Page ID
    assert ("method", "major") in found         # 14 months
    assert ("compliance", "blocker") in found   # scripted snapshot fetching


def test_plan_budget_over_the_run_cap_blocks(tmp_path):
    make(tmp_path, [f"B{i}:{c}@{1000 + i}" for i, c in enumerate(["GB", "DE", "FR", "IT", "ES", "IE"])],
         window={"months": 12})
    cfg = load_config()
    cfg["api"] = dict(cfg["api"], max_calls_per_run=40)
    doc = checks(tmp_path, "plan", Guard(tmp_path / "data", cfg, env={}))
    (f,) = ids(doc, category="budget")
    assert f["severity"] == "blocker" and doc["verdict"] == "block"


# --- data gate ------------------------------------------------------------------------------------

def test_data_gate_flags_zero_ads_and_stopped_runs(tmp_path):
    fixture_project(tmp_path, [f"Synthetic UK:GB@{UK_PAGE}", f"Synthetic IN Page:GB@{IN_PAGE}"])
    doc = checks(tmp_path, "data")
    (f,) = ids(doc, brands=["synthetic-in-page"], category="data-quality")
    assert f["severity"] == "major" and f["ask_user"]
    assert run(tmp_path, transport=Throttled()) == 2
    doc = checks(tmp_path, "data")
    assert ids(doc, severity="blocker", category="data-quality") and doc["verdict"] == "block"


# --- report gate ----------------------------------------------------------------------------------

def test_spend_claim_in_a_report_is_a_claim_blocker(tmp_path):
    run_dir = fixture_project(tmp_path, [f"Synthetic UK:GB@{UK_PAGE}"])
    report = run_dir / "synthetic-uk" / "report.md"
    seed_notes(report, "- Spend is not available from any official source.\n- Run length is a proxy.")
    assert not ids(checks(tmp_path, "report"), severity="blocker")
    seed_notes(report, "- The brand likely spent about £20k a month, with a CPM near £4.")
    doc = checks(tmp_path, "report")
    (f,) = ids(doc, severity="blocker", category="claim")
    assert "spend" in f["claim"] and f["brands"] == ["synthetic-uk"] and doc["verdict"] == "block"


def test_non_eu_claim_implying_full_detail_is_flagged(tmp_path):
    run_dir = fixture_project(tmp_path, [f"Synthetic UK:GB@{UK_PAGE}", f"Mixed:GB+IN@{MULTI_PAGE}", "kapiva:IN"])
    cmp = run_dir / "comparison.md"
    seed_notes(cmp, "- In India, Mixed leans on discount-led primary text.\n"
                    "- Kapiva's ads use a Shop Now button on every video.\n"
                    "- Kapiva: no copy or visuals are available (outside the EU/UK).")
    doc = checks(tmp_path, "report")
    flagged = ids(doc, severity="blocker", category="claim")
    assert {tuple(f["brands"]) for f in flagged} == {("mixed",), ("kapiva",)}
    assert all("outside the EU/UK" in f["claim"] for f in flagged)


def test_unknown_ids_untraced_numbers_and_snapshot_links(tmp_path):
    run_dir = fixture_project(tmp_path, [f"Synthetic UK:GB@{UK_PAGE}"])
    seed_notes(run_dir / "synthetic-uk" / "report.md",
               "- Ad [990000000000999](https://www.facebook.com/ads/library/?id=990000000000999) ran 77 days.\n"
               "- See https://www.facebook.com/ads/archive/render_ad/?id=990000000000101 for the creative.")
    doc = checks(tmp_path, "report")
    claims = [f["claim"] for f in doc["findings"]]
    assert any("990000000000999" in c for c in claims)
    assert any("number 77" in c for c in claims)
    assert ids(doc, severity="blocker", category="compliance")


# --- recording ------------------------------------------------------------------------------------

def test_record_keeps_check_blockers_and_updates_project_and_index(tmp_path):
    run_dir = fixture_project(tmp_path, [f"Synthetic UK:GB@{UK_PAGE}"])
    seed_notes(run_dir / "synthetic-uk" / "report.md", "- Estimated ROAS is 3x.")
    project, paths = paths_for(tmp_path)
    check_doc = checks(tmp_path, "report")
    critique.write_json(critique.checks_path(paths, "report"), check_doc)
    dropped = {"prompt_version": "critic-v1", "stage": "report", "verdict": "approve", "summary": "Looks fine.",
               "findings": []}
    with pytest.raises(projects.ProjectError, match="drops or softens"):
        critique.record(dropped, project, paths, tmp_path / "projects")
    kept = {**check_doc, "summary": "One invented ROAS figure must go.",
            "findings": check_doc["findings"] + [{**GOOD["findings"][0], "category": "claim"}]}
    out = critique.record(kept, project, paths, tmp_path / "projects")
    assert out == run_dir / "critique-report.json"
    saved = projects.load_project("synth", tmp_path / "projects")
    assert saved["critic"]["last_verdict"] == "block" and saved["critic"]["stage"] == "report"
    index = (run_dir / "index.md").read_text()
    assert "**Report gate: block**" in index and "One invented ROAS figure must go." in index
    assert run(tmp_path, "--no-fetch") == 0  # a rebuild re-renders the block from the critique file
    assert "**Report gate: block**" in (run_dir / "index.md").read_text()
    assert "Last critic verdict: **block**" in projects.plan_screen(saved, TODAY)
    from openpyxl import load_workbook
    cov = list(load_workbook(run_dir / "workbook.xlsx")["Coverage"].iter_rows(values_only=True))
    assert "[blocker]" in cov[1][-1] and "ROAS" in cov[1][-1]


def test_cli_check_writes_the_checks_file(tmp_path, capsys):
    make(tmp_path, ["kapiva:IN"])
    args = ["--projects-dir", str(tmp_path / "projects"), "--data-dir", str(tmp_path / "data"),
            "--reports-dir", str(tmp_path / "reports")]
    assert critique.main(args + ["check", "synth", "--stage", "plan", "--date", TODAY.isoformat()]) == 0
    out = json.loads(capsys.readouterr().out)
    f = tmp_path / "reports" / "synth" / "2026-09-29" / "checks-plan.json"
    assert json.loads(f.read_text()) == out
    assert critique.main(args + ["validate", str(f)]) == 0
    assert critique.main(args + ["check", "synth", "--stage", "data", "--date", TODAY.isoformat()]) == 2
