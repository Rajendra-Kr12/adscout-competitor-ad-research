"""P1-01: every ad's sub-elements reach the report and ads.csv (full text, headline,
description, caption, UK/EU reach, age x gender, targeting), for any queried country."""
import csv
import io
import json

import build_report
import normalize
from conftest import MULTI_PAGE, UK_PAGE, run_fixture_fetch
from text_metrics import compute, location_reach, uk_reach, unique_variants


def report_for(tmp_path, page, country, date="2026-09-30"):
    key, raw = run_fixture_fetch(tmp_path, page, country)
    normalize.normalize_run(raw, tmp_path)
    reports = tmp_path / "reports"
    assert build_report.main(["--target", key, "--since", "2026-01", "--until", "2026-09",
                              "--data-dir", str(tmp_path), "--reports-dir", str(reports), "--date", date]) == 0
    md = (reports / key / f"{date}-competitor-report.md").read_text()
    rows = list(csv.DictReader(io.StringIO((reports / key / f"{date}-ads.csv").read_text())))
    return key, md, rows


def test_demo_report_shows_headline_description_caption_and_full_body(tmp_path):
    _, md, rows = report_for(tmp_path, UK_PAGE, "GB")
    # Ad 990000000000101 (started before the window, active in it).
    for needle in ("Kumkumadi Night Oil 15ml", "Free UK delivery over £40", "synthetic-ayurveda.example",
                   "Made with saffron and 21 herbs."):
        assert needle in md, needle
    assert "## Ad details" in md and "### 990000000000101" in md
    assert "Card 3" in md  # every carousel card body, not only the first line
    assert "| Targeting | ages 25-54; gender All; locations United Kingdom |" in md
    row = next(r for r in rows if r["library_id"] == "990000000000101")
    assert row["primary_text"].endswith("Made with saffron and 21 herbs.")
    assert row["headlines"] == "Kumkumadi Night Oil 15ml" and row["captions"] == "synthetic-ayurveda.example"
    assert len(rows) == 12


def test_uk_reach_for_gb_only_ad_and_creative_only_label(tmp_path):
    _, md, rows = report_for(tmp_path, MULTI_PAGE, "GB")
    card = md.split("### 990000000000301", 1)[1].split("### ", 1)[0]
    assert "| UK reach | 52,000 |" in card and "| EU reach | n/a |" in card
    assert "| Headline | Rose Glow Overnight Serum (×3) |" in card
    assert "(returned ×3, identical)" in card and card.count("> Cold-pressed rosehip") == 1
    assert "excluded Northern Ireland" in card
    assert "| GB | 35-44 | 900 | 18,400 | 100 |" in card
    creative = md.split("### 990000000000302", 1)[1].split("### ", 1)[0]
    assert "creative-only" in creative and "4,000 (sum of the GB age x gender breakdown)" in creative
    assert "UK reach (sum / median, 2 ads; 1 from GB breakdown sums) | 56,000" in md
    by_id = {r["library_id"]: r for r in rows}
    assert by_id["990000000000301"]["uk_reach"] == "52000"
    assert by_id["990000000000302"]["text_status"] == "creative-only"
    assert by_id["990000000000302"]["uk_reach_basis"] == "breakdown_sum"
    assert "990000000000303" not in by_id and "990000000000304" not in by_id  # DE/AT and US-only ads


def test_any_eu_country_works(tmp_path):
    _, md, rows = report_for(tmp_path, MULTI_PAGE, "DE")
    assert [r["library_id"] for r in rows] == ["990000000000303"]
    assert "countries queried: DE" in md
    assert "| EU reach | 15,000 |" in md and "Synthetic Botanicals GmbH" in md
    assert "Gratis Versand ab 40 €" in md and "Jetzt 15% Rabatt" in md


def test_non_eu_country_zero_ads_says_coverage_plainly(tmp_path, capsys):
    _, md, rows = report_for(tmp_path, MULTI_PAGE, "US")
    assert rows == []
    assert "No ads were returned for this window" in md
    assert "Meta's Ad Library API only covers commercial ads delivered to the EU/UK" in md
    assert "asked for US only" in md and "coverage limit, not an error" in md
    assert "US is outside the EU/UK" in md
    assert "coverage limit, not an error" in capsys.readouterr().err  # fetch says so too


def test_outputs_have_no_token_and_csv_is_formula_safe(tmp_path):
    key, _, _ = report_for(tmp_path, MULTI_PAGE, "GB")
    f = tmp_path / "ads" / key / "990000000000301.json"
    ad = json.loads(f.read_text())
    ad["text"]["link_titles"] = ["=HYPERLINK(\"http://evil\")"]
    ad["derived"] = compute(ad["text"], ad["eu_metrics"])
    f.write_text(json.dumps(ad))
    build_report.main(["--target", key, "--since", "2026-01", "--until", "2026-09", "--data-dir", str(tmp_path),
                       "--reports-dir", str(tmp_path / "r2"), "--date", "2026-09-30"])
    text = (tmp_path / "r2" / key / "2026-09-30-ads.csv").read_text()
    assert "'=HYPERLINK" in text and "access_token" not in text.lower()


def test_schema_02_and_upgrade_of_01_files(tmp_path):
    key, raw = run_fixture_fetch(tmp_path, MULTI_PAGE, "GB")
    out = normalize.normalize_run(raw, tmp_path)
    old = out / "990000000000301.json"
    ad = json.loads(old.read_text())
    assert ad["schema_version"] == "0.2" and not list(normalize.validator().iter_errors(ad))
    ad["schema_version"] = "0.1"
    for k in ("text_status", "variants_unique", "uk_reach", "reach_by_country"):
        del ad["derived"][k]
    old.write_text(json.dumps(ad))
    # A run that no longer contains this ad still upgrades it in place.
    other = tmp_path / "raw" / key / "other.jsonl"
    other.write_text("\n".join(l for l in raw.read_text().splitlines() if "990000000000301" not in l))
    normalize.normalize_run(other, tmp_path)
    again = json.loads(old.read_text())
    assert again["schema_version"] == "0.2" and again["derived"]["uk_reach"]["value"] == 52000


def test_markdown_quote_neutralises_syntax():
    lines = build_report.md_quote("# not a heading\n- not a list\n<script>x</script>\n\nend")
    assert lines[0] == "> \\# not a heading\\" and lines[1] == "> \\- not a list\\"
    assert "<script>" not in "".join(lines) and lines[-1] == "> end"


def test_reach_helpers_accept_documented_and_defensive_shapes():
    assert location_reach([{"key": "UK", "value": 5}, {"key": "EU", "value": 7}]) == {"UK": 5, "EU": 7}
    assert location_reach({"uk": 3}) == {"UK": 3}
    assert location_reach(None) == {} and location_reach([{"key": "UK", "value": "x"}]) == {}
    assert uk_reach({"total_reach_by_location": [{"key": "UK", "value": 9}]}) == {
        "value": 9, "basis": "total_reach_by_location"}
    assert uk_reach({"eu_total_reach": 100}) is None
    assert unique_variants(["a  b", "a b", "", "c"]) == [{"text": "a  b", "count": 2}, {"text": "c", "count": 1}]
