"""Where the Unsplash API key lives, and the only code that reads it.

The key is UNSPLASH_ACCESS_KEY: an UNSPLASH_ACCESS_KEY environment variable wins, otherwise the project
.env, which is refused unless it is mode 0600 (no group or world access), exactly like token_store.py.
This is a different credential from the Meta token and has its own store; it is a public-data photo
search key, but it is still never printed, logged or copied. Only a fingerprint (sha256[:8]) is shown.

Nothing here writes the key. The captain creates it at https://unsplash.com/oauth/applications and adds
the line `UNSPLASH_ACCESS_KEY=...` to .env themselves.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from common import ROOT
from token_store import fingerprint

VAR = "UNSPLASH_ACCESS_KEY"
KEY_SHAPE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


class UnsplashKeyError(RuntimeError):
    pass


def _from_env_file(path: Path) -> str | None:
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise UnsplashKeyError(f"{path.name} is readable by other users (mode {mode:o}). Run: chmod 600 '{path}'")
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == VAR:
            return value.strip().strip("'\"") or None
    return None


def get_key(*, env=None, env_path: Path | None = None) -> tuple[str | None, str]:
    """Return (key, source), or (None, "none") when no key is set. The key is for in-memory use only."""
    env = os.environ if env is None else env
    raw, source = env.get(VAR), "environment"
    if not raw:
        raw, source = _from_env_file(Path(env_path or ROOT / ".env")), ".env"
    if not raw:
        return None, "none"
    raw = raw.strip()
    if not KEY_SHAPE.match(raw):
        # Placeholder text such as "your-key-here" is not a key; never echo what was found.
        raise UnsplashKeyError(f"{VAR} is set but does not look like an Unsplash access key "
                               "(20-128 letters, digits, - or _). Check the value in .env.")
    return raw, source


def describe(*, env=None, env_path: Path | None = None) -> str:
    """One safe line for status output: fingerprint and source, never the key."""
    key, source = get_key(env=env, env_path=env_path)
    return f"{VAR}: not set (sample results only)" if key is None else \
        f"{VAR}: set, fingerprint {fingerprint(key)}, from {source}"
