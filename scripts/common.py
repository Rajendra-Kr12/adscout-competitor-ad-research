"""Shared helpers: paths, .env, config, dates/months, permalinks, token scrubbing."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
CONFIG_PATH = ROOT / "config" / "competitors.yaml"
GUARD_CONFIG_PATH = ROOT / "config" / "guard.yaml"
SCHEMA_PATH = ROOT / "schemas" / "ad.schema.json"

DEFAULT_API_VERSION = "v26.0"
PERMALINK_BASE = "https://www.facebook.com/ads/library/?id="

# ads_archive returns commercial ads only when they were delivered to these countries.
EU_UK = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "GB",
})

# access_token=<value> in a URL or text; also bare "access_token": "<value>" JSON keys.
_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE)
# The whole access_token query parameter, with its leading ? or & and any trailing &.
_TOKEN_PARAM_RE = re.compile(r"([?&])access_token=[^&\s\"']*(&?)", re.IGNORECASE)


# --- environment / config -------------------------------------------------

def load_env(path: Path | None = None) -> dict[str, str]:
    """Read KEY=VALUE lines from .env; real environment variables win."""
    values: dict[str, str] = {}
    path = path or ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    for key in ("META_APP_ID", "META_ACCESS_TOKEN", "META_API_VERSION"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def load_competitors(path: Path = CONFIG_PATH) -> dict[str, dict]:
    doc = yaml.safe_load(path.read_text()) or {}
    return {c["key"]: c for c in doc.get("competitors", [])}


# --- dates and months -----------------------------------------------------

def parse_ts(value: str | None) -> datetime | None:
    """Parse the Ad Library's date forms: 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS+0000'."""
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    v = v.replace("Z", "+00:00")
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def month_key(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def parse_month(value: str) -> date:
    """'YYYY-MM' -> first day of that month."""
    return datetime.strptime(value, "%Y-%m").date()


def next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def months_between(start: date, end: date) -> list[str]:
    """Inclusive list of 'YYYY-MM' keys from start's month to end's month."""
    out, cur = [], date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cur <= last:
        out.append(month_key(cur))
        cur = next_month(cur)
    return out


def month_windows(since: str, until: str) -> list[tuple[str, str]]:
    """Calendar-month (min, max) YYYY-MM-DD windows covering since..until (YYYY-MM, inclusive)."""
    start, end = parse_month(since), parse_month(until)
    if end < start:
        raise ValueError(f"--until {until} is before --since {since}")
    windows = []
    cur = start
    while cur <= end:
        nxt = next_month(cur)
        last_day = date.fromordinal(nxt.toordinal() - 1)
        windows.append((cur.isoformat(), last_day.isoformat()))
        cur = nxt
    return windows


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- links and secrets ----------------------------------------------------

def permalink(library_id: str) -> str:
    """Public Ad Library permalink. Never use ad_snapshot_url in outputs: it embeds the token."""
    lid = str(library_id).strip()
    if not lid.isdigit():
        raise ValueError(f"library id must be numeric, got {library_id!r}")
    return PERMALINK_BASE + lid


def scrub_tokens(text: str) -> str:
    return _TOKEN_RE.sub(r"\1REDACTED", text)


def strip_token_params(value):
    """Remove access_token query parameters from every string in a JSON-like value.

    Used before anything is written to data/raw/, so no file on disk holds the token.
    """
    if isinstance(value, str):
        return _TOKEN_PARAM_RE.sub(lambda m: m.group(1) if m.group(2) else "", value)
    if isinstance(value, list):
        return [strip_token_params(v) for v in value]
    if isinstance(value, dict):
        return {k: strip_token_params(v) for k, v in value.items()}
    return value


def contains_token(text: str) -> bool:
    return "access_token" in text.lower()


def assert_no_token(text: str, where: str) -> None:
    if contains_token(text):
        raise RuntimeError(f"refusing to write {where}: output contains 'access_token'")


def write_json(path: Path, obj) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    assert_no_token(text, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_private(path: Path, text: str) -> None:
    """Atomically write a 0600 file (state, ledgers). Refuses text containing access_token."""
    assert_no_token(text, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "target"
