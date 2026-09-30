#!/usr/bin/env python3
"""Claude Code PreToolUse hook: blocks tool calls that would break AGENTS.md hard rules.

Reads the hook JSON on stdin. Exit 2 with a reason on stderr denies the call; exit 0 allows it.
Standard library only, so it works without the project venv.

Denied:
- WebFetch / MCP tools aimed at facebook.com or fbcdn.net hosts (graph.facebook.com and
  developers.facebook.com stay allowed), and network commands (curl, wget, open,
  chrome-devtools-axi, ...) aimed at them.
- Bash / WebFetch / MCP input containing `access_token=` or reading the Keychain token.
- Reading `.env` (Read/Grep/Glob, or Bash readers such as cat/grep/source) and reading
  `data/raw/` directly (the pipeline scripts may still take raw paths as arguments).
- `lavish-axi share` (public third-party hosting) and `claude mcp add` of a Meta/Ad Library
  server other than Meta's own https://mcp.facebook.com/.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from urllib.parse import urlparse

BLOCKED_SUFFIXES = ("facebook.com", "fbcdn.net", "fb.com", "fb.me")
ALLOWED_HOSTS = {"graph.facebook.com", "developers.facebook.com", "mcp.facebook.com"}
NETWORK_TOOLS = {"curl", "wget", "http", "https", "httpie", "aria2c", "lynx", "links", "w3m", "open",
                 "chrome-devtools-axi", "playwright", "puppeteer", "xh", "nc"}
READERS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "rg", "ag", "sed", "awk", "jq",
           "strings", "xxd", "od", "hexdump", "bat", "source", ".", "cut", "sort", "uniq", "diff",
           "base64", "tee", "cp", "mv", "scp", "rsync", "vim", "nano", "code", "open", "pbcopy", "xargs",
           "find", "zip", "tar"}
# Interpreters may take data/raw/... or .env paths as arguments to the project's own scripts;
# only inline code (-c / -e) is treated as a direct read.
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "bash", "sh", "zsh"}
BROWSER_MCP_RE = re.compile(r"chrome|browser|playwright|puppeteer|computer|devtools|fetch|scrape|crawl|apify",
                            re.IGNORECASE)
HOST_RE = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.IGNORECASE)
ENV_RE = re.compile(r"(?:^|[\s/'\"=<(])\.env(?![\w.])")
PATH_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "TodoWrite"}


def blocked_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in ALLOWED_HOSTS:
        return False
    return any(host == s or host.endswith("." + s) for s in BLOCKED_SUFFIXES)


def blocked_hosts_in(text: str) -> list[str]:
    return [h for h in HOST_RE.findall(text or "") if blocked_host(h)]


def secret_reason(text: str) -> str | None:
    low = (text or "").lower()
    if "access_token=" in low:
        return "input contains 'access_token='. Tokens must never pass through tool calls or chat"
    if "find-generic-password" in low and "fbcm" in low or "security find-generic-password" in low:
        return "reading the Keychain token from a tool call is not allowed; token_store.py reads it in-process"
    return None


def _segments(command: str) -> list[list[str]]:
    out = []
    for part in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        # skip leading VAR=value assignments and common wrappers
        while words and (re.match(r"^\w+=", words[0]) or words[0] in {"sudo", "command", "exec", "time", "nohup"}):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _prog(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def bash_reason(command: str) -> str | None:
    if r := secret_reason(command):
        return r
    for words in _segments(command):
        prog = _prog(words[0])
        text = " ".join(words)
        if prog in NETWORK_TOOLS and (hosts := blocked_hosts_in(text)):
            return (f"{prog} to {hosts[0]}: automated access to facebook.com/fbcdn.net is forbidden "
                    "(AGENTS.md). Only graph.facebook.com through scripts/fetch_ads.py")
        if prog == "lavish-axi" and len(words) > 1 and words[1] == "share":
            return "lavish-axi share publishes to a public third-party host; this project's data stays private"
        if prog == "claude" and words[1:3] == ["mcp", "add"]:
            if re.search(r"meta|facebook|fb[-_ ]?ads|ad[-_ ]?library|adlibrary", text, re.IGNORECASE) \
                    and "https://mcp.facebook.com/" not in text:
                return "only Meta's own Ads MCP (https://mcp.facebook.com/ads) may be added; no third-party Meta Ads MCPs"
        inline = prog.startswith(tuple(INTERPRETERS)) and any(w in ("-c", "-e") for w in words[1:])
        readers = READERS | ({prog} if inline else set())
        mentions_env = any(ENV_RE.search(" " + w) for w in words[1:])
        if mentions_env and prog in readers:
            if prog == "cp" and words[1:] == [".env.example", ".env"]:
                continue
            return ".env holds the Meta token; do not read or copy it (use scripts/token_cli.py status)"
        if prog in readers and any("data/raw/" in w or w.rstrip("/").endswith("data/raw") for w in words[1:]):
            return "data/raw/ is read only by the pipeline scripts (normalize.py), not directly"
    return None


def path_reason(tool: str, ti: dict) -> str | None:
    for key in ("file_path", "path", "notebook_path", "pattern"):
        v = str(ti.get(key) or "")
        if not v:
            continue
        name = v.rstrip("/").rsplit("/", 1)[-1]
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return ".env holds the Meta token; do not read it (use scripts/token_cli.py status)"
        if "data/raw/" in v or v.rstrip("/").endswith("data/raw"):
            return "data/raw/ is read only by the pipeline scripts (normalize.py), not by the agent"
    if tool == "Grep" and (g := str(ti.get("glob") or "")) and ".env" == g.rsplit("/", 1)[-1]:
        return ".env holds the Meta token; do not read it"
    return None


def decide(event: dict) -> str | None:
    tool = event.get("tool_name") or ""
    ti = event.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        return None  # scripts/ and tests/ legitimately contain the scrubbing code and fake tokens
    if tool in PATH_TOOLS:
        return path_reason(tool, ti)
    if tool == "Bash":
        return bash_reason(str(ti.get("command") or ""))
    if tool == "WebFetch":
        url = str(ti.get("url") or "")
        host = urlparse(url if "://" in url else "https://" + url).hostname or ""
        if blocked_host(host):
            return (f"WebFetch to {host}: automated access to facebook.com/fbcdn.net is forbidden (AGENTS.md). "
                    "A human can look; the agent can't")
        return secret_reason(url + " " + str(ti.get("prompt") or ""))
    if tool.startswith("mcp__"):
        text = json.dumps(ti)
        if BROWSER_MCP_RE.search(tool) and (hosts := blocked_hosts_in(text)):
            return f"{tool} aimed at {hosts[0]}: browser/MCP access to facebook.com is forbidden (AGENTS.md)"
        return secret_reason(text)
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw or "{}")
        reason = decide(event)
    except Exception as exc:  # noqa: BLE001 - fail closed for tools that can reach the network
        tool = ""
        try:
            tool = json.loads(raw).get("tool_name", "")
        except Exception:  # noqa: BLE001
            pass
        if tool in EDIT_TOOLS or tool in PATH_TOOLS:
            return 0
        print(f"guard_pretool: could not check this call ({type(exc).__name__}); denied to be safe", file=sys.stderr)
        return 2
    if reason:
        print(f"Blocked by .claude/hooks/guard_pretool.py: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
