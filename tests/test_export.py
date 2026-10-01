"""P1-06: workbook + CSV exports for a project run (openpyxl round-trip, full text, no token, no images)."""
import csv
import json

import pytest
from openpyxl import load_workbook

import export_xlsx
import llm_batches
import project as projects
from conftest import FIXTURES, MULTI_PAGE, SYNTHETIC, UK_PAGE
from test_project import TODAY, make, run

SHEETS = ["About", "Ads", "Variants", "Monthly", "Offers", "Reach", "Changes", "Coverage"]


def fixture_run(tmp_path, **answers):
    make(tmp_path, [f"Synthetic UK:GB@{UK_PAGE}", f"Synthetic Multi:GB@{MULTI_PAGE}", "kapiva:IN"],
         window={"since": "2026-01", "until": "2026-09"}, **answers)
    assert run(tmp_path, "--fixture", str(SYNTHETIC)) == 0
    return tmp_path / "reports" / "synth" / "2026-09-29"


def sheet_rows(ws):
    rows = list(ws.iter_rows(values_only=True))
    return [dict(zip(rows[0], r)) for r in rows[1:]]


def test_workbook_round_trips_with_full_text_for_every_ad(tmp_path):
    run_dir = fixture_run(tmp_path)
    wb = load_workbook(run_dir / "workbook.xlsx")
    assert wb.sheetnames == SHEETS
    ads = sheet_rows(wb["Ads"])
    stored = {f.stem: json.loads(f.read_text()) for b in ("synthetic-uk", "synthetic-multi")
              for f in (tmp_path / "data" / "ads" / b).glob("*.json")}
    assert sorted(r["library_id"] for r in ads) == sorted(stored)
    for r in ads:
        ad = stored[r["library_id"]]
        for v in ad["derived"]["variants_unique"]["bodies"]:
            assert v["text"] in (r["primary_text"] or ""), r["library_id"]  # full text, not a first line
        assert r["text_status"] == ad["derived"]["text_status"]
        assert r["permalink"] == f"https://www.facebook.com/ads/library/?id={r['library_id']}"
    assert {r["brand"] for r in ads} == {"Synthetic UK", "Synthetic Multi"}
    ws = wb["Ads"]
    link_col = list(next(ws.iter_rows(max_row=1, values_only=True))).index("permalink") + 1
    assert all(ws.cell(row=i, column=link_col).hyperlink.target.startswith("https://www.facebook.com/ads/library/?id=")
               for i in range(2, ws.max_row + 1))
    variants = sheet_rows(wb["Variants"])
    assert {"body", "title", "description", "caption"} <= {v["field"] for v in variants}
    for ws in wb.worksheets:
        assert not ws._images and not ws._charts  # no embedded competitor media
        for row in ws.iter_rows(values_only=True):
            assert not any(isinstance(v, str) and "access_token" in v.lower() for v in row)
    about = dict(wb["About"].iter_rows(values_only=True))
    assert "SYNTHETIC DATA" in about and "spend" in about["Not available"]


def test_coverage_monthly_offers_and_reach_sheets(tmp_path):
    run_dir = fixture_run(tmp_path)
    data = tmp_path / "data"
    assert llm_batches.main(["merge", "--target", "synthetic-uk", str(FIXTURES / "llm_output_synthetic.json"),
                             "--data-dir", str(data)]) == 0
    assert run(tmp_path, "--no-fetch") == 0  # rebuild after the LLM merge, no API calls
    wb = load_workbook(run_dir / "workbook.xlsx")
    cov = {r["brand"]: r for r in sheet_rows(wb["Coverage"])}
    assert cov["Kapiva"]["mcp_countries"] == "IN" and "not connected" in cov["Kapiva"]["zero_ads_explanation"]
    assert "headline" in cov["Kapiva"]["coverage_plan"]
    assert cov["Synthetic UK"]["llm_coverage"] == "6/12" and cov["Synthetic UK"]["zero_ads_explanation"] is None
    monthly = [r for r in sheet_rows(wb["Monthly"]) if r["brand"] == "Synthetic UK"]
    assert len(monthly) == 9 and next(r for r in monthly if r["month"] == "2026-02")["new_ads"] == 2
    offers = sheet_rows(wb["Offers"])
    assert any(o["source"] == "llm" and o["offer_type"] == "discount" for o in offers)
    assert all(o["library_ids"] for o in offers)
    reach = sheet_rows(wb["Reach"])
    assert reach and {"male", "female", "unknown"} <= set(reach[0])
    assert sheet_rows(wb["Changes"]) == []


def test_csv_folder_matches_the_sheets(tmp_path):
    run_dir = fixture_run(tmp_path)
    files = sorted(p.name for p in (run_dir / "csv").iterdir())
    assert files == ["ads.csv", "changes.csv", "coverage.csv", "monthly.csv", "offers.csv", "reach.csv", "variants.csv"]
    with (run_dir / "csv" / "ads.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 14 and list(rows[0]) == export_xlsx.ADS_COLUMNS
    assert "workbook.xlsx" in (run_dir / "index.md").read_text()


def test_formula_like_text_stays_text_and_a_token_blocks_the_export(tmp_path):
    run_dir = fixture_run(tmp_path)
    f = tmp_path / "data" / "ads" / "synthetic-uk" / "990000000000102.json"
    ad = json.loads(f.read_text())
    ad["text"]["bodies"] = ['=HYPERLINK("https://example.com","x")']
    ad["derived"]["variants_unique"]["bodies"] = [{"text": ad["text"]["bodies"][0], "count": 1}]
    f.write_text(json.dumps(ad))
    assert run(tmp_path, "--no-fetch") == 0
    ws = load_workbook(run_dir / "workbook.xlsx")["Ads"]
    col = list(next(ws.iter_rows(max_row=1, values_only=True))).index("primary_text") + 1
    cell = next(ws.cell(row=i, column=col) for i in range(2, ws.max_row + 1)
                if (ws.cell(row=i, column=col).value or "").startswith("=HYPERLINK"))
    assert cell.data_type == "s"
    assert "'=HYPERLINK" in (run_dir / "csv" / "ads.csv").read_text()

    before = (run_dir / "workbook.xlsx").read_bytes()
    ad["derived"]["variants_unique"]["bodies"] = [{"text": "see x?access_token=LEAK", "count": 1}]
    f.write_text(json.dumps(ad))
    project = projects.load_project("synth", tmp_path / "projects")
    paths = projects.run_paths(project, TODAY.isoformat(), tmp_path / "reports")
    manifest = json.loads(paths.manifest.read_text())
    with pytest.raises(export_xlsx.ExportError, match="access_token"):
        export_xlsx.export_run(project, manifest, paths, tmp_path / "data")
    assert (run_dir / "workbook.xlsx").read_bytes() == before


def test_outputs_off_means_no_exports(tmp_path):
    run_dir = fixture_run(tmp_path, outputs={"markdown": True, "xlsx": False, "csv": False})
    assert not (run_dir / "workbook.xlsx").exists() and not (run_dir / "csv").exists()
    assert (run_dir / "index.md").exists()
