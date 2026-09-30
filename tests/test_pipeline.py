"""End-to-end offline pipeline: fetch (fixture) -> normalize -> llm merge -> report.

Also the token-scrubbing guarantee: the API returns access_token inside ad_snapshot_url
(the fixture does too), fetch strips it before writing raw, and nothing on disk has it.
"""
import json
import shutil

import build_report
import llm_batches
import normalize
from conftest import IN_PAGE, UK_PAGE, run_fixture_fetch


def all_text(root):
    return {p: p.read_text() for p in root.rglob("*") if p.is_file()}


def pipeline(tmp_path, fixtures_dir, page=UK_PAGE, country="GB"):
    key, raw = run_fixture_fetch(tmp_path, page, country)
    normalize.normalize_run(raw, tmp_path)
    return key, raw


def test_end_to_end_report(tmp_path, fixtures_dir, capsys):
    key, raw = pipeline(tmp_path, fixtures_dir)
    capsys.readouterr()
    # plan batches for the sub-agent
    assert llm_batches.main(["plan", "--target", key, "--size", "5", "--data-dir", str(tmp_path)]) == 0
    plan_lines = capsys.readouterr().out.strip().splitlines()
    assert len(plan_lines) == 3  # 12 ads / 5 per batch
    first_input = json.loads(open(plan_lines[0].split(" -> ")[0]).read())
    assert len(first_input["ads"]) == 5 and "permalink" not in first_input["ads"][0]
    # merge the (synthetic) sub-agent output
    assert llm_batches.main(["merge", "--target", key, str(fixtures_dir / "llm_output_synthetic.json"),
                             "--data-dir", str(tmp_path)]) == 0
    reports = tmp_path / "reports"
    assert build_report.main(["--target", key, "--since", "2026-01", "--until", "2026-09",
                              "--data-dir", str(tmp_path), "--reports-dir", str(reports),
                              "--date", "2026-09-29"]) == 0
    report = (reports / key / "2026-09-29-competitor-report.md").read_text()
    assert "SYNTHETIC DATA" in report
    assert "only if they were delivered to the EU or UK" in report
    for f in (tmp_path / "ads" / key).glob("*.json"):
        assert f"https://www.facebook.com/ads/library/?id={f.stem}" in report  # every ad in the ledger
    assert "| 2026-02 | 2 | 4 |" in report  # ads per month: new / active
    assert "LLM extraction coverage: 6/12 ads (prompt extract-v1)" in report
    assert "Offer types:" in report and "discount 2" in report
    assert "990000000000101" in report and "Started before 2026-01" in report


def test_no_access_token_outside_raw(tmp_path, fixtures_dir):
    key, raw = pipeline(tmp_path, fixtures_dir)
    reports = tmp_path / "reports"
    build_report.main(["--target", key, "--data-dir", str(tmp_path), "--reports-dir", str(reports)])
    assert "render_ad/?id=" in raw.read_text()  # raw keeps snapshot URLs, minus the token
    for root in (tmp_path / "raw", tmp_path / "runs", tmp_path / "state", tmp_path / "ads", reports):
        files = all_text(root)
        assert files
        for path, text in files.items():
            assert "access_token" not in text.lower(), path
            assert "FIXTURE-NOT-A-REAL-TOKEN" not in text, path
            if root.name != "raw":
                assert "render_ad" not in text, path


def test_report_refuses_to_write_token(tmp_path, fixtures_dir):
    key, _ = pipeline(tmp_path, fixtures_dir)
    f = next((tmp_path / "ads" / key).glob("*.json"))
    ad = json.loads(f.read_text())
    ad["derived"]["first_line"] = "see https://x/render_ad/?id=1&access_token=LEAK"
    f.write_text(json.dumps(ad))
    try:
        build_report.main(["--target", key, "--data-dir", str(tmp_path), "--reports-dir", str(tmp_path / "r")])
    except RuntimeError as exc:
        assert "access_token" in str(exc)
    else:
        raise AssertionError("report with a token was written")
    assert not (tmp_path / "r").exists() or not any((tmp_path / "r").rglob("*.md"))


def test_merge_rejects_invalid_llm(tmp_path, fixtures_dir):
    key, _ = pipeline(tmp_path, fixtures_dir)
    bad = tmp_path / "bad.json"
    good = json.loads((fixtures_dir / "llm_output_synthetic.json").read_text())["results"][0]
    wrong = json.loads(json.dumps(good))
    wrong["llm"]["tone"] = ["grumpy"]
    wrong["library_id"] = "990000000000103"
    bad.write_text(json.dumps({"prompt_version": "extract-v1", "results": [
        good, wrong, {"library_id": "1", "llm": good["llm"]}]}))
    assert llm_batches.main(["merge", "--target", key, str(bad), "--data-dir", str(tmp_path)]) == 1
    ads = tmp_path / "ads" / key
    assert json.loads((ads / "990000000000102.json").read_text())["llm"] is not None
    assert json.loads((ads / "990000000000103.json").read_text())["llm"] is None
    assert not list(normalize.validator().iter_errors(json.loads((ads / "990000000000102.json").read_text())))


def test_india_only_report_explains_empty_result(tmp_path, fixtures_dir):
    key, _ = pipeline(tmp_path, fixtures_dir, page=IN_PAGE, country="IN")
    reports = tmp_path / "reports"
    build_report.main(["--target", key, "--since", "2026-01", "--until", "2026-09",
                       "--data-dir", str(tmp_path), "--reports-dir", str(reports), "--date", "2026-09-29"])
    report = (reports / key / "2026-09-29-competitor-report.md").read_text()
    assert "No ads were returned for this window" in report
    assert "| Ads in window | 0 |" in report


def test_rebuild_keeps_analyst_notes(tmp_path, fixtures_dir):
    key, _ = pipeline(tmp_path, fixtures_dir)
    reports = tmp_path / "reports"
    args = ["--target", key, "--data-dir", str(tmp_path), "--reports-dir", str(reports), "--date", "2026-09-29"]
    build_report.main(args)
    path = reports / key / "2026-09-29-competitor-report.md"
    text = path.read_text()
    start, end = build_report.NARRATIVE_START, build_report.NARRATIVE_END
    notes = "\nTheme: winter glow copy was reused (990000000000106).\n"
    path.write_text(text.split(start)[0] + start + notes + end + text.split(end)[1])
    build_report.main(args)
    assert notes in path.read_text()
