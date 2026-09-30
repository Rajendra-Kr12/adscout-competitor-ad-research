"""P1-04: the PreToolUse guard hook (JSON on stdin) and the pre-commit token check."""
import json
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT

HOOK = ROOT / ".claude" / "hooks" / "guard_pretool.py"
PRE_COMMIT = ROOT / ".githooks" / "pre-commit"


def hook(tool, **tool_input):
    event = {"session_id": "t", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True)
    return r.returncode, r.stderr


@pytest.mark.parametrize("tool,tool_input", [
    ("WebFetch", {"url": "https://www.facebook.com/ads/library/?id=123", "prompt": "x"}),
    ("WebFetch", {"url": "https://scontent-lhr8-1.xx.fbcdn.net/v/t39/abc.jpg", "prompt": "x"}),
    ("Bash", {"command": "curl -s 'https://graph.facebook.com/v26.0/ads_archive?access_token=abc'"}),
    ("Bash", {"command": "wget https://www.facebook.com/ads/archive/render_ad/?id=1"}),
    ("Bash", {"command": "chrome-devtools-axi open https://m.facebook.com/somepage"}),
    ("Bash", {"command": "cd x && open 'https://facebook.com/ads/library'"}),
    ("Bash", {"command": "security find-generic-password -s fbcm.meta -a user_token -w"}),
    ("Bash", {"command": "cat .env"}),
    ("Bash", {"command": "grep META_ACCESS_TOKEN ./.env | head -1"}),
    ("Bash", {"command": "source .env && echo ok"}),
    ("Bash", {"command": "python3 -c \"print(open('.env').read())\""}),
    ("Bash", {"command": "head -3 data/raw/forest-essentials-uk/20260930T080154Z.jsonl"}),
    ("Bash", {"command": "lavish-axi share x.html"}),
    ("Bash", {"command": "claude mcp add ad-library-scraper -- npx some-meta-ads-mcp"}),
    ("Read", {"file_path": "/repo/data/raw/fe/20260930T000000Z.jsonl"}),
    ("Read", {"file_path": "/repo/.env"}),
    ("Grep", {"pattern": "TOKEN", "path": "/repo/.env"}),
    ("mcp__claude-in-chrome__navigate", {"url": "https://www.facebook.com/ads/library/"}),
    ("mcp__some_server__call", {"q": "x?access_token=abc"}),
])
def test_hook_denies(tool, tool_input):
    rc, err = hook(tool, **tool_input)
    assert rc == 2, (tool, tool_input)
    assert "Blocked by .claude/hooks/guard_pretool.py" in err


@pytest.mark.parametrize("tool,tool_input", [
    ("Bash", {"command": ".venv/bin/python scripts/fetch_ads.py --competitor forest-essentials-uk "
                         "--since 2026-01 --until 2026-09"}),
    ("Bash", {"command": ".venv/bin/python scripts/normalize.py data/raw/fe/20260930T080154Z.jsonl"}),
    ("Bash", {"command": ".venv/bin/python -m pytest -q"}),
    ("Bash", {"command": "grep -rl access_token data/ads reports"}),
    ("Bash", {"command": "cp .env.example .env && chmod 600 .env && ls -l .env"}),
    ("Bash", {"command": "git check-ignore data/state/x data/runs/x logs/x .env"}),
    ("Bash", {"command": "ls data/raw/fe"}),
    ("Bash", {"command": "cat .env.example"}),
    ("WebFetch", {"url": "https://developers.facebook.com/docs/graph-api/reference/ads_archive/", "prompt": "x"}),
    ("WebFetch", {"url": "https://graph.facebook.com/v26.0/", "prompt": "x"}),
    ("Read", {"file_path": "/repo/.env.example"}),
    ("Read", {"file_path": "/repo/reports/fe/2026-09-30-competitor-report.md"}),
    ("Edit", {"file_path": "/repo/scripts/fixture_api.py", "old_string": "a", "new_string": "&access_token={token}"}),
    ("Write", {"file_path": "/repo/tests/x.py", "content": "u = 'x?access_token=FAKE'"}),
    ("mcp__claude_ai_Gmail__create_draft", {"body": "See https://www.facebook.com/ads/library/?id=1"}),
    ("Bash", {"command": "claude mcp add --transport http meta-ads https://mcp.facebook.com/ads"}),
])
def test_hook_allows(tool, tool_input):
    rc, err = hook(tool, **tool_input)
    assert rc == 0, (tool, tool_input, err)


def test_hook_fails_closed_for_network_tools_on_bad_input():
    r = subprocess.run([sys.executable, str(HOOK)], input='{"tool_name": "Bash", "tool_input": 5}',
                       capture_output=True, text=True)
    assert r.returncode == 2


def test_settings_json_registers_the_hook_and_deny_rules():
    s = json.loads((ROOT / ".claude" / "settings.json").read_text())
    deny = s["permissions"]["deny"]
    for rule in ("Read(./.env)", "Read(./data/raw/**)", "WebFetch(domain:www.facebook.com)",
                 "mcp__claude-in-chrome__*", "Bash(lavish-axi share*)"):
        assert rule in deny
    cmd = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "guard_pretool.py" in cmd and '"${CLAUDE_PROJECT_DIR}' in cmd  # quoted: the path has spaces


# --- pre-commit -------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True, check=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (tmp_path / ".githooks").mkdir()
    shutil.copy(PRE_COMMIT, tmp_path / ".githooks" / "pre-commit")
    run("config", "core.hooksPath", ".githooks")
    return tmp_path, run


def commit(repo_dir, *paths):
    subprocess.run(["git", "add", *paths], cwd=repo_dir, check=True)
    return subprocess.run(["git", "commit", "-qm", "t"], cwd=repo_dir, capture_output=True, text=True)


def test_pre_commit_rejects_a_staged_token(repo):
    d, _ = repo
    fake = "EAA" + "B" * 40
    (d / "notes.md").write_text(f"line one\nMETA_ACCESS_TOKEN={fake}\n")
    r = commit(d, "notes.md")
    assert r.returncode != 0 and "notes.md:2: looks like a Meta access token" in r.stderr
    assert fake not in r.stderr


def test_pre_commit_rejects_platform_data_and_env(repo):
    d, _ = repo
    for rel in ("data/raw/fe/run.jsonl", "reports/fe/2026-09-30-competitor-report.md", "logs/x.log", ".env"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x\n")
        r = commit(d, "-f", rel)
        assert r.returncode != 0, rel
        subprocess.run(["git", "reset", "-q"], cwd=d, check=True)


def test_pre_commit_allows_clean_code(repo):
    d, _ = repo
    (d / "scripts").mkdir()
    (d / "scripts" / "x.py").write_text('URL = "https://x/?id=1&access_token={token}"\nP = "EAA" + "x" * 40\n')
    (d / ".env.example").write_text("META_ACCESS_TOKEN=\n")
    assert commit(d, "scripts/x.py", ".env.example").returncode == 0


def test_repository_itself_passes_the_token_check():
    r = subprocess.run(["git", "grep", "-nE", r"EAA[A-Za-z0-9]{20,}"], cwd=ROOT, capture_output=True, text=True)
    assert r.stdout == ""
