"""P1-05: multi-brand projects - project files, any-country routing, grouped queries, dated folders."""
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

import discover_pages
import fetch_ads
import mcp_source
import normalize
import project as projects
import run_project
from conftest import IN_PAGE, MULTI_PAGE, SYNTHETIC, UK_PAGE

TODAY = date(2026, 9, 29)
COMPETITORS = {
    "kapiva": {"key": "kapiva", "name": "Kapiva", "page_id": None, "search_terms": "Kapiva", "countries": ["IN"]},
    "fe": {"key": "fe", "name": "Forest Essentials UK", "page_id": "100331625781604", "search_terms": "Forest Essentials",
           "countries": ["GB"]},
}


def make(tmp_path, specs, **answers):
    doc = projects.new_project("synth", specs, today=TODAY, competitors=COMPETITORS, **answers)
    return projects.save_project(doc, tmp_path / "projects")


def synth_specs():
    return [f"Synthetic UK:GB@{UK_PAGE}", f"Synthetic Multi:GB@{MULTI_PAGE}", f"Synthetic IN Page:GB@{IN_PAGE}"]


def run(tmp_path, *extra, transport=None):
    return run_project.main(["synth", "--projects-dir", str(tmp_path / "projects"), "--data-dir", str(tmp_path / "data"),
                             "--reports-dir", str(tmp_path / "reports"), "--date", TODAY.isoformat(), *extra],
                            transport=transport)


# --- project files and routing ---------------------------------------------------------

def test_defaults_write_a_valid_project_file(tmp_path):
    path = make(tmp_path, ["fe", "Pukka Herbs"])
    doc = yaml.safe_load(path.read_text())
    assert not list(projects.validator().iter_errors(doc))
    assert doc["markets"] == ["GB"] and doc["window"] == {"months": 6} and doc["layout"] == "project/date/brand"
    assert doc["outputs"] == {"markdown": True, "xlsx": True, "csv": True, "html": False, "drive": False}
    fe, pukka = doc["brands"]
    assert fe["page_ids"] == ["100331625781604"] and fe["page_id_source"] == "config"
    assert pukka == {"key": "pukka-herbs", "name": "Pukka Herbs", "page_ids": [], "search_terms": "Pukka Herbs",
                     "coverage": pukka["coverage"]}
    assert "not resolved" in pukka["coverage"]["plan"]
    assert projects.load_project("synth", tmp_path / "projects") == projects.finalize(doc)
    assert "access_token" not in path.read_text()


def test_any_country_per_brand_is_routed(tmp_path):
    doc = projects.load_project(make(tmp_path, ["kapiva:IN", "Mixed Co:uk+in+de@123", "fe"], markets=["US"]))
    kap, mixed, fe = doc["brands"]
    assert kap["coverage"]["ads_archive"] == [] and kap["coverage"]["mcp"] == ["IN"]
    assert "headline" in kap["coverage"]["plan"] and "not connected" in kap["coverage"]["plan"]
    assert "screenshot checklist" in kap["coverage"]["plan"] and "no primary text" in kap["coverage"]["plan"]
    assert mixed["countries"] == ["GB", "IN", "DE"]  # UK is Meta's GB
    assert mixed["coverage"]["ads_archive"] == ["GB", "DE"] and mixed["coverage"]["mcp"] == ["IN"]
    assert fe["coverage"]["mcp"] == ["US"]  # no own countries: the project markets apply
    with pytest.raises(projects.ProjectError, match="unknown country"):
        projects.new_project("x", ["kapiva:XX"], today=TODAY, competitors=COMPETITORS)


@pytest.mark.parametrize("root,ok", [("reports", True), ("reports/clients", True), ("data/out", False),
                                     ("../elsewhere", False), (str(projects.ROOT / "scripts"), False),
                                     (str(projects.ROOT / "reports" / "x"), True), ("/tmp/fbcm-out", True)])
def test_output_root_must_be_reports_or_outside_the_repo(root, ok):
    doc = projects.new_project("x", ["fe"], today=TODAY, competitors=COMPETITORS)
    doc["output_root"] = root
    if ok:
        projects.finalize(doc)
    else:
        with pytest.raises(projects.ProjectError):
            projects.finalize(doc)


def test_groups_hold_at_most_ten_page_ids_and_never_split_a_brand():
    specs = [f"B{i}:GB@{1000 + i}" for i in range(9)] + ["Two Pages:GB@2001+2002", "German:DE@3001"]
    doc = projects.new_project("x", specs, today=TODAY, competitors={})
    groups = projects.archive_groups(doc)
    assert [len(g["page_ids"]) for g in groups] == [9, 2, 1]
    assert groups[1]["brands"] == ["two-pages"] and groups[2]["countries"] == ["DE"]
    assert groups[1]["page_map"] == {"2001": "two-pages", "2002": "two-pages"}


def test_window_months_end_with_the_run_month():
    doc = projects.new_project("x", ["fe"], today=TODAY, competitors=COMPETITORS, window={"months": 12})
    assert projects.resolve_window(doc, date(2026, 2, 3)) == ("2025-03", "2026-02")
    doc["window"] = {"since": "2026-01", "until": "2026-03"}
    assert projects.resolve_window(doc, TODAY) == ("2026-01", "2026-03")


@pytest.mark.parametrize("layout,report,index,links", [
    ("project/date/brand", "synth/2026-09-29/b/report.md", "synth/2026-09-29/index.md", ["synth/latest"]),
    ("brand/date", "synth/b/2026-09-29/report.md", "synth/runs/2026-09-29/index.md",
     ["synth/runs/latest", "synth/b/latest"]),
    ("flat", "b/2026-09-29-competitor-report.md", "synth/2026-09-29-index.md", []),
])
def test_layouts(tmp_path, layout, report, index, links):
    p = projects.RunPaths(tmp_path, "synth", "2026-09-29", layout)
    assert p.brand_report("b") == tmp_path / report
    assert p.index == tmp_path / index
    assert [str(l.relative_to(tmp_path)) for l, _ in p.latest_links(["b"])] == links


# --- run_project ----------------------------------------------------------------------------

def test_dry_run_prints_one_grouped_query_per_window(tmp_path, capsys):
    make(tmp_path, synth_specs() + ["kapiva:IN", "Pukka Herbs"], window={"since": "2026-01", "until": "2026-09"})
    assert run(tmp_path, "--dry-run") == 0
    out, err = capsys.readouterr()
    gets = [l for l in out.splitlines() if l.startswith("GET ")]
    assert len(gets) == 9
    assert all(f"%22{UK_PAGE}%22%2C+%22{MULTI_PAGE}%22%2C+%22{IN_PAGE}%22" in g for g in gets)
    assert "9 ads_archive queries (1 group(s) x 9 window(s))" in err and "Nothing was sent" in err
    assert "pukka-herbs: Page ID unresolved" in out and "kapiva: Meta Ads MCP" in out
    assert not (tmp_path / "data" / "runs").exists() and not (tmp_path / "reports").exists()


def test_fixture_run_writes_dated_per_brand_folders(tmp_path, capsys):
    make(tmp_path, synth_specs() + ["kapiva:IN"], window={"since": "2026-01", "until": "2026-09"})
    assert run(tmp_path, "--fixture", str(SYNTHETIC)) == 0
    reports, data = tmp_path / "reports" / "synth", tmp_path / "data"
    run_dir = reports / "2026-09-29"
    for b in ("synthetic-uk", "synthetic-multi", "synthetic-in-page", "kapiva"):
        assert (run_dir / b / "report.md").exists() and (run_dir / b / "ads.csv").exists()
    assert (reports / "latest").is_symlink() and (reports / "latest").resolve() == run_dir.resolve()
    manifest = json.loads((run_dir / "run.json").read_text())
    (g,) = manifest["groups"]
    assert g["calls"] == 9 and g["page_ids"] == [UK_PAGE, MULTI_PAGE, IN_PAGE] and g["status"] == "ok"
    ledger = json.loads((data / g["ledger"]).read_text())
    assert ledger["calls"] == 9 and ledger["status"] == "ok"
    # one raw file for the group, split per brand at normalize time
    assert len(list((data / "ads" / "synthetic-uk").glob("*.json"))) == 12
    assert {json.loads(f.read_text())["page"]["id"] for f in (data / "ads" / "synthetic-multi").glob("*.json")} == {MULTI_PAGE}
    stats = {b["key"]: b for b in manifest["brands"]}
    assert stats["synthetic-uk"]["stats"]["ads_in_window"] == 12
    assert stats["synthetic-in-page"]["stats"]["ads_in_window"] == 0
    assert stats["kapiva"]["status"] == "mcp-only (not fetched)"
    index = (run_dir / "index.md").read_text()
    assert "SYNTHETIC DATA" in index and "[report](synthetic-uk/report.md)" in index
    assert "MCP IN (not connected)" in index and "<!-- critic:start -->" in index
    kap = (run_dir / "kapiva" / "report.md").read_text()
    assert "sent to ads_archive: none" in kap and "No ads were fetched" in kap
    assert "Runs" in (reports / "project.md").read_text()
    for root in (tmp_path / "reports", data / "ads", data / "runs"):
        for f in root.rglob("*"):
            if f.is_file() and f.suffix != ".xlsx":  # the workbook is checked cell by cell in test_export.py
                assert "access_token" not in f.read_text().lower(), f


def test_rebuild_keeps_notes_and_critic_block(tmp_path):
    make(tmp_path, synth_specs()[:1])
    assert run(tmp_path, "--fixture", str(SYNTHETIC)) == 0
    run_dir = tmp_path / "reports" / "synth" / "2026-09-29"
    idx = run_dir / "index.md"
    idx.write_text(idx.read_text().replace(run_project.CRITIC_PLACEHOLDER, "Verdict: approve (seeded)"))
    rep = run_dir / "synthetic-uk" / "report.md"
    rep.write_text(rep.read_text().replace("_Not written yet.", "Seeded notes. _Not written yet."))
    calls_before = json.loads((run_dir / "run.json").read_text())["groups"][0]["calls"]
    assert run(tmp_path, "--no-fetch") == 0
    assert "Verdict: approve (seeded)" in idx.read_text() and "Seeded notes." in rep.read_text()
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["mode"] == "no-fetch" and manifest["groups"][0]["calls"] == calls_before
    assert len(list((tmp_path / "data" / "runs").glob("*.json"))) == 1  # no new fetch


class Throttled:
    def __init__(self):
        self.calls = 0

    def get(self, url, params):
        self.calls += 1
        raise fetch_ads.MetaApiError("HTTP 400 code=4 subcode=None: rate limited", 4)


def test_a_stopped_group_ends_the_run_and_is_reported(tmp_path):
    make(tmp_path, synth_specs()[:2] + ["German:DE@900000000000009"])
    t = Throttled()
    assert run(tmp_path, transport=t) == 2
    assert t.calls == 1  # the first failure stops everything; the DE group is never queried
    run_dir = tmp_path / "reports" / "synth" / "2026-09-29"
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["status"] == "stopped" and len(manifest["groups"]) == 1
    statuses = {b["key"]: b["status"] for b in manifest["brands"]}
    assert statuses == {"synthetic-uk": "fetch stopped", "synthetic-multi": "fetch stopped",
                        "german": "not fetched (an earlier group stopped)"}
    assert "## Fetch stopped" in (run_dir / "index.md").read_text()
    with pytest.raises(normalize.PartialRunError):
        normalize.read_run(tmp_path / "data" / manifest["groups"][0]["raw"])


# --- discovery and the MCP stub ---------------------------------------------------------------

def test_discovery_uses_exact_phrase_one_call_per_brand(tmp_path, capsys):
    path = make(tmp_path, ["Synthetic Ayurveda", "kapiva:IN"])
    args = ["--project", str(path), "--data-dir", str(tmp_path / "data"), "--today", TODAY.isoformat()]
    assert discover_pages.main(args + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    (get,) = [l for l in out.splitlines() if l.startswith("GET ")]
    assert "search_type=KEYWORD_EXACT_PHRASE" in get and "search_terms=Synthetic+Ayurveda" in get
    assert "fields=id%2Cpage_id%2Cpage_name&" in get
    assert discover_pages.main(args + ["--fixture", str(SYNTHETIC)]) == 0
    doc = json.loads(capsys.readouterr().out)
    (res,) = doc["brands"]
    assert res["brand"] == "synthetic-ayurveda" and res["countries"] == ["GB"]
    assert res["candidates"][0] == {"page_id": UK_PAGE, "page_name": "Synthetic Ayurveda UK (FIXTURE)",
                                    "ads_seen": 12, "name_match": True, "ads_seen_is_lower_bound": False}
    ledger = json.loads(next((tmp_path / "data" / "runs").glob("discover-*.json")).read_text())
    assert ledger["calls"] == 1 and ledger["status"] == "ok"
    assert (tmp_path / "data" / "state" / "discovery" / "synthetic-ayurveda.json").stat().st_mode & 0o777 == 0o600


def test_set_page_resolves_a_brand(tmp_path):
    make(tmp_path, ["Pukka Herbs"])
    assert projects.main(["--projects-dir", str(tmp_path / "projects"), "set-page", "synth", "pukka-herbs",
                          "123456", "--source", "discovery"]) == 0
    doc = projects.load_project("synth", tmp_path / "projects")
    assert doc["brands"][0]["page_ids"] == ["123456"] and doc["brands"][0]["page_id_source"] == "discovery"
    assert "not resolved" not in doc["brands"][0]["coverage"]["plan"]


def test_mcp_source_is_a_stub_and_maps_headline_only_records():
    with pytest.raises(mcp_source.McpNotConnected, match="not connected"):
        mcp_source.McpLibrarySource().search({"page_ids": ["1"]})
    # shape from the 2026-09-30 spike (synthetic values)
    ad = {"id": "990000000000901", "page_id": 900000000000009, "page_name": "Synthetic IN Brand",
          "ad_creative_link_title": "Payday = Extra 15% Off", "ad_creation_time": 1786719504,
          "ad_delivery_start_time": 1786732651, "ad_snapshot_url": "https://www.facebook.com/ads/library/?id=990000000000901",
          "currency": "INR"}
    out = mcp_source.normalize_mcp_ad(ad, fetched_at="2026-09-30T08:00:00Z", run_id="mcp-test", active_status="ACTIVE")
    assert not list(normalize.validator().iter_errors(out))
    assert out["coverage_basis"] == "mcp_library_search" and out["derived"]["text_status"] == "headline-only"
    assert out["text"]["bodies"] == [] and out["page"]["id"] == "900000000000009"
    assert out["dates"]["delivery_stop"] is None and out["dates"]["active_at_fetch"] is True
    assert out["derived"]["has_percent_off"] is True
    reqs = mcp_source.plan_requests({"name": "X", "page_ids": []}, ["IN"])
    assert reqs == [{"tool": "ads_library_search", "countries": ["IN"], "ad_active_status": "ACTIVE", "limit": 50,
                     "search_terms": "X"}]
