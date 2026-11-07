"""Pinterest Trends is a disabled stub: no network code, always raises, nothing in the project calls it."""
import ast
from pathlib import Path

import pytest

import pinterest_trends

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
NETWORK = {"requests", "urllib", "urllib3", "http", "httpx", "aiohttp", "socket", "ssl", "subprocess", "webbrowser",
           "playwright", "selenium", "pinterest", "yaml", "os"}


def test_stub_is_disabled_and_always_raises():
    assert pinterest_trends.ENABLED is False
    with pytest.raises(pinterest_trends.PinterestTrendsDisabled, match="not switched on"):
        pinterest_trends.fetch_trends("GB", "growing")
    assert pinterest_trends.main([]) == 3


def test_stub_has_no_network_or_credential_code():
    tree = ast.parse((SCRIPTS / "pinterest_trends.py").read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & NETWORK
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any(s.startswith(("http://", "https://")) or "api.pinterest" in s for s in strings)


def test_nothing_else_in_scripts_reaches_pinterest():
    for f in SCRIPTS.glob("*.py"):
        if f.name == "pinterest_trends.py":
            continue
        low = f.read_text().lower()
        assert "api.pinterest.com" not in low and "import pinterest_trends" not in low, f.name
