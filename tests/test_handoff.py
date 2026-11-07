"""Handoff brief: offline, one page, tagged, no spend/token/snapshot, coverage gaps stated."""
import re

import export_handoff
from conftest import SYNTHETIC

PERMALINK = re.compile(r"https://www\.facebook\.com/ads/library/\?id=\d+")


def test_fixture_handoff_is_one_tagged_page_without_secrets(tmp_path):
    assert export_handoff.main(["demo", "--fixture", str(SYNTHETIC), "--out-dir", str(tmp_path), "--date", "2026-11-07"]) == 0
    text = (tmp_path / "demo.md").read_text()
    assert text.count("\n") <= export_handoff.MAX_LINES
    assert "SYNTHETIC DATA" in text
    bullets = [l for l in text.splitlines() if l.startswith("- ")]
    assert bullets and all(re.match(r"- (Observed|Pattern|Hypothesis|Unknown):", l) for l in bullets if not l.startswith(("- Spend", "- Images", "- [ ]")))
    # a market outside the EU/UK is a stated coverage gap, not an error and not "no ads"
    assert "Synthetic Ayurveda IN (IN): 0 ads" in text and "coverage gap" in text
    assert PERMALINK.search(text)
    # visual direction: tagged, states the API has no image fields, and the reference block is an empty human placeholder
    vd = text.split("## Visual direction")[1].split("## Reference links (human picks)")[0]
    assert "not image fields" in vd and re.search(r"- (Observed|Pattern|Hypothesis|Unknown):", vd)
    refs = text.split("## Reference links (human picks)")[1].split("## ")[0]
    assert 3 <= len([l for l in refs.splitlines() if l.startswith("- [ ]")]) <= 5
    assert "pinterest" not in refs.lower().replace("no pinterest", "") and "http" not in refs
    low = text.lower()
    for bad in ("access_token", "ad_snapshot_url", "snapshot", "eaa"):
        assert bad not in low
    assert "not available from official sources" in low  # spend/CPM/ROAS only named as unavailable
    assert not re.search(r"(spent|cpm of|roas of|cpa of)", low)


def test_dry_run_writes_nothing_and_unknown_slug_is_a_clean_error(tmp_path, capsys):
    assert export_handoff.main(["demo", "--fixture", str(SYNTHETIC), "--out-dir", str(tmp_path), "--dry-run"]) == 0
    assert not list(tmp_path.iterdir()) and "No Meta call" in capsys.readouterr().out
    assert export_handoff.main(["no-such-brand", "--data-dir", str(tmp_path), "--out-dir", str(tmp_path)]) == 2
