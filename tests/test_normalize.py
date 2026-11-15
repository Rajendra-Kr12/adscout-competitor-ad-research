import json

import pytest

import normalize
from common import permalink, write_json
from conftest import UK_PAGE, run_fixture_fetch


def test_permalink_format():
    assert permalink("123456789012345") == "https://www.facebook.com/ads/library/?id=123456789012345"
    assert permalink(42) == "https://www.facebook.com/ads/library/?id=42"
    for bad in ["12a", "", "1&access_token=x"]:
        with pytest.raises(ValueError):
            permalink(bad)


def test_normalize_fixture_run(tmp_path):
    key, raw = run_fixture_fetch(tmp_path, UK_PAGE, "GB")
    out = normalize.normalize_run(raw, tmp_path)
    files = sorted(out.glob("*.json"))
    assert len(files) == 12  # deduplicated across the 9 month windows
    v = normalize.validator()
    for f in files:
        ad = json.loads(f.read_text())
        assert not list(v.iter_errors(ad))
        assert ad["permalink"] == f"https://www.facebook.com/ads/library/?id={f.stem}"
        assert "ad_snapshot_url" not in f.read_text()
        assert ad["coverage_basis"] == "fixture"
    carousel = json.loads((out / "990000000000103.json").read_text())
    assert carousel["derived"]["variant_count"] == 3 and len(carousel["text"]["bodies"]) == 3
    assert carousel["eu_metrics"]["eu_total_reach"] == 30500
    dup = json.loads((out / "990000000000106.json").read_text())
    assert dup["derived"]["near_duplicate_of"] == "990000000000104"


def test_media_type_hint_from_per_type_queries(tmp_path):
    _, raw = run_fixture_fetch(tmp_path, UK_PAGE, "GB", extra=["--media-types", "IMAGE,VIDEO"])
    out = normalize.normalize_run(raw, tmp_path)
    hints = {f.stem: json.loads(f.read_text())["media_type_hint"] for f in out.glob("*.json")}
    assert hints["990000000000104"] == "VIDEO" and hints["990000000000102"] == "IMAGE"
    assert "990000000000110" not in hints  # MEME ad was not queried


def test_renormalize_keeps_llm(tmp_path):
    _, raw = run_fixture_fetch(tmp_path, UK_PAGE, "GB")
    out = normalize.normalize_run(raw, tmp_path)
    f = out / "990000000000102.json"
    ad = json.loads(f.read_text())
    ad["llm"] = {"hook": "h"}  # content irrelevant here; only preservation is tested
    ad["provenance"].update(extractor_model="m", prompt_version="extract-v1")
    f.write_text(json.dumps(ad))
    header, records = normalize.read_run(raw)
    rec = next(r for r in records if r["ad"]["id"] == "990000000000102")
    again = normalize.normalize_ad(rec, header, None, ad)
    assert again["llm"] == {"hook": "h"} and again["provenance"]["prompt_version"] == "extract-v1"


def test_read_run_survives_unicode_line_separators_in_ad_text(tmp_path):
    """Ad text can hold U+2028 / U+0085; str.splitlines() would cut such a record in half."""
    _, raw = run_fixture_fetch(tmp_path, UK_PAGE, "GB")
    _, before = normalize.read_run(raw)
    lines = raw.read_text(encoding="utf-8").split("\n")
    i = next(n for n, l in enumerate(lines) if '"type": "ad"' in l or '"type":"ad"' in l)
    rec = json.loads(lines[i])
    rec["ad"]["ad_creative_bodies"] = ["first line second line\u0085third"]
    lines[i] = json.dumps(rec, ensure_ascii=False)
    raw.write_text("\n".join(lines), encoding="utf-8")
    _, after = normalize.read_run(raw)
    assert len(after) == len(before)


def test_coverage_basis_rules():
    live = {"mode": "live", "target": {"countries": ["GB"]}}
    assert normalize.coverage_basis({}, live) == "eu_uk_delivery"
    assert normalize.coverage_basis({"spend": {"lower_bound": "0"}}, live) == "political_issue"
    assert normalize.coverage_basis({}, {"mode": "live", "target": {"countries": ["IN"]}}) == "political_issue"


def test_write_json_refuses_tokens(tmp_path):
    with pytest.raises(RuntimeError, match="access_token"):
        write_json(tmp_path / "x.json", {"u": "https://x/?id=1&access_token=abc"})
    assert not (tmp_path / "x.json").exists()
