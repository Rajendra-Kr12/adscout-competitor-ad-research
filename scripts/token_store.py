"""Where the Meta user access token lives, and the only code that reads or writes it.

Backends:
- env:      META_ACCESS_TOKEN in the project .env. Refused unless the file is 0600
            (no group or world access). This is the active default.
- keychain: macOS Keychain generic password, service "fbcm.meta", account "user_token",
            read and written with /usr/bin/security. The token never goes on a command
            line: writes use `security -i` with the command on stdin.

The active backend is, in order: FBCM_TOKEN_STORE in the environment, the "store" recorded
by `token_cli.py rotate --to ...` in data/state/guard/token.json, then `token.store` in
config/guard.yaml. A META_ACCESS_TOKEN environment variable overrides every backend.

Only a fingerprint (sha256[:8]) is ever printed or stored; the token itself stays in memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from common import DATA_DIR, ROOT

KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "fbcm.meta", "user_token"
STORES = ("env", "keychain")
# A Meta user token is a long run of [A-Za-z0-9] starting with EAA. Only this shape is accepted
# for storage, which also keeps it safe inside the quoted `security -i` command.
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9]{20,1024}$")
USER_TOKEN_RE = re.compile(r"EAA[A-Za-z0-9]{20,}")


class TokenStoreError(RuntimeError):
    pass


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def _check_shape(token: str) -> str:
    token = token.strip()
    if not TOKEN_SHAPE.match(token):
        raise TokenStoreError("that does not look like a Meta access token (letters and digits only)")
    return token


class EnvFileStore:
    name = "env"

    def __init__(self, path: Path = ROOT / ".env"):
        self.path = Path(path)

    def _check_mode(self) -> None:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise TokenStoreError(f"{self.path.name} is readable by other users (mode {mode:o}). "
                                  f"Run: chmod 600 '{self.path}'")

    def get(self) -> str | None:
        if not self.path.exists():
            return None
        self._check_mode()
        for line in self.path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "META_ACCESS_TOKEN":
                return value.strip().strip("'\"") or None
        return None

    def set(self, token: str) -> None:
        token = _check_shape(token)
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        if self.path.exists():
            self._check_mode()
        out, done = [], False
        for line in lines:
            if line.strip().partition("=")[0].strip() == "META_ACCESS_TOKEN":
                out.append(f"META_ACCESS_TOKEN={token}")
                done = True
            else:
                out.append(line)
        if not done:
            out.append(f"META_ACCESS_TOKEN={token}")
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, self.path)


class KeychainStore:
    name = "keychain"

    def __init__(self, security: str | None = None, service: str = KEYCHAIN_SERVICE,
                 account: str = KEYCHAIN_ACCOUNT):
        self.security = security or os.environ.get("FBCM_SECURITY_BIN") or "/usr/bin/security"
        self.service, self.account = service, account

    def get(self) -> str | None:
        try:
            r = subprocess.run([self.security, "find-generic-password", "-s", self.service,
                                "-a", self.account, "-w"], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise TokenStoreError(f"Keychain tool not found at {self.security}") from None
        if r.returncode == 44:  # errSecItemNotFound
            return None
        if r.returncode != 0:
            raise TokenStoreError(f"Keychain read failed (exit {r.returncode}); is the login keychain unlocked?")
        return r.stdout.strip() or None

    def set(self, token: str) -> None:
        token = _check_shape(token)
        cmd = f'add-generic-password -U -s "{self.service}" -a "{self.account}" -w "{token}"\n'
        r = subprocess.run([self.security, "-i"], input=cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            raise TokenStoreError(f"Keychain write failed (exit {r.returncode})")


def meta_path(data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / "state" / "guard" / "token.json"


def read_meta(data_dir: Path = DATA_DIR) -> dict:
    try:
        return json.loads(meta_path(data_dir).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def active_store_name(config: dict | None = None, data_dir: Path = DATA_DIR, env=None) -> str:
    env = os.environ if env is None else env
    name = env.get("FBCM_TOKEN_STORE") or read_meta(data_dir).get("store") \
        or ((config or {}).get("token") or {}).get("store") or "env"
    if name not in STORES:
        raise TokenStoreError(f"unknown token store {name!r}; use one of {', '.join(STORES)}")
    return name


def make_store(name: str, *, env_path: Path | None = None, security: str | None = None):
    if name == "env":
        return EnvFileStore(env_path or ROOT / ".env")
    if name == "keychain":
        return KeychainStore(security)
    raise TokenStoreError(f"unknown token store {name!r}")


def get_token(config: dict | None = None, data_dir: Path = DATA_DIR, *, env=None,
              env_path: Path | None = None, security: str | None = None) -> tuple[str | None, str]:
    """Return (token, source). The token is for in-memory use only; never print it."""
    env = os.environ if env is None else env
    if env.get("META_ACCESS_TOKEN"):
        return env["META_ACCESS_TOKEN"], "environment"
    name = active_store_name(config, data_dir, env)
    return make_store(name, env_path=env_path, security=security).get(), name
