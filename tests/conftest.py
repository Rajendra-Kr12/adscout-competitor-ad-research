import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = ROOT / "tests" / "fixtures"
SYNTHETIC = FIXTURES / "ads_archive_synthetic.json"
UK_PAGE, IN_PAGE, MULTI_PAGE = "900000000000001", "900000000000002", "900000000000003"


@pytest.fixture
def fixtures_dir():
    return FIXTURES


def run_fixture_fetch(data_dir, page_id, country, since="2026-01", until="2026-09", extra=()):
    """Run fetch_ads.main against the synthetic fixture; return the raw JSONL path."""
    import fetch_ads

    key = f"t-{page_id}-{country}".lower()
    rc = fetch_ads.main(["--page-id", page_id, "--country", country, "--since", since, "--until", until,
                         "--out-key", key, "--fixture", str(SYNTHETIC), "--data-dir", str(data_dir),
                         "--limit", "3", *extra])
    assert rc == 0
    runs = sorted((data_dir / "raw" / key).glob("*.jsonl"))
    return key, runs[-1]
