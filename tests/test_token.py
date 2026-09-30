"""P1-03: token store backends, token_cli status/rotate/scan. Fully offline: a fake `security`
binary stands in for the Keychain and fake transports stand in for Graph."""
import argparse
import io
import json
import os
import stat
import sys
import time

import pytest

import fetch_ads
import token_cli
from common import scrub_tokens
from guard import Guard, load_config
from test_fetch import FakeResp, FakeSession, err
from token_store import (
    EnvFileStore,
    KeychainStore,
    TokenStoreError,
    active_store_name,
    fingerprint,
    get_token,
    read_meta,
)

TOKEN = "EAA" + "Synthetic0Test0Token0Value0Not0Real" * 2
NEW_TOKEN = "EAA" + "Rotated0Synthetic0Token0Not0Real" * 2
SECRET = "synthetic0app0secret0value"

FAKE_SECURITY = r'''import json, os, shlex, sys
store = os.environ["FAKE_KEYCHAIN"]
db = json.load(open(store)) if os.path.exists(store) else {{}}
with open(store + ".argv", "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
def opt(args, flag):
    return args[args.index(flag) + 1]
if sys.argv[1] == "find-generic-password":
    key = opt(sys.argv, "-s") + "/" + opt(sys.argv, "-a")
    if key not in db:
        sys.exit(44)
    print(db[key])
elif sys.argv[1] == "-i":
    for line in sys.stdin:
        args = shlex.split(line)
        if args and args[0] == "add-generic-password":
            db[opt(args, "-s") + "/" + opt(args, "-a")] = opt(args, "-w")
    json.dump(db, open(store, "w"))
else:
    sys.exit(2)
'''


@pytest.fixture
def security(tmp_path, monkeypatch):
    impl = tmp_path / "fake_security.py"
    impl.write_text(FAKE_SECURITY.format())
    path = tmp_path / "security"  # a shell wrapper: the venv path contains spaces, which a shebang cannot
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n')
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_KEYCHAIN", str(tmp_path / "keychain.json"))
    return str(path)


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text(f"META_APP_ID=1122\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_API_VERSION=v26.0\n")
    p.chmod(0o600)
    return p


@pytest.fixture(autouse=True)
def no_env_token(monkeypatch):
    for k in ("META_ACCESS_TOKEN", "FBCM_TOKEN_STORE", "FBCM_KILL"):
        monkeypatch.delenv(k, raising=False)


class FakeGraph:
    """Answers debug_token and the long-lived exchange; records every call."""

    def __init__(self, expires_in_days=6, valid=True, error=None):
        self.calls = []
        self.expires_in_days, self.valid, self.error = expires_in_days, valid, error

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        if url.endswith("/oauth/access_token"):
            return {"access_token": NEW_TOKEN, "token_type": "bearer", "expires_in": 5184000}
        now = int(time.time())
        data = {"app_id": "1122", "type": "USER", "application": "FBCM Ad Library Test",
                "issued_at": now - 86400, "expires_at": now + int(self.expires_in_days * 86400),
                "data_access_expires_at": now + 90 * 86400, "is_valid": self.valid,
                "scopes": ["public_profile"], "user_id": "42"}
        if self.error:
            data["error"] = self.error
        return {"data": data}


def ns(tmp_path, env_file, **kw):
    return argparse.Namespace(data_dir=tmp_path / "data", env_file=env_file, refresh=False, to=None, paths=[], **kw)


# --- backends -------------------------------------------------------------------

def test_env_store_requires_0600_and_rewrites_in_place(tmp_path, env_file):
    s = EnvFileStore(env_file)
    assert s.get() == TOKEN
    env_file.chmod(0o644)
    with pytest.raises(TokenStoreError, match="chmod 600"):
        s.get()
    env_file.chmod(0o600)
    s.set(NEW_TOKEN)
    text = env_file.read_text()
    assert f"META_ACCESS_TOKEN={NEW_TOKEN}" in text and "META_APP_ID=1122" in text and TOKEN not in text
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    with pytest.raises(TokenStoreError):
        s.set("not a token; rm -rf /")


def test_keychain_store_round_trip_keeps_token_off_argv(tmp_path, security):
    k = KeychainStore(security)
    assert k.get() is None  # exit 44: not found
    k.set(TOKEN)
    assert k.get() == TOKEN
    argv_log = (tmp_path / "keychain.json.argv").read_text()
    assert TOKEN not in argv_log and "find-generic-password -s fbcm.meta -a user_token -w" in argv_log


def test_active_store_precedence(tmp_path, env_file):
    cfg = load_config()
    data = tmp_path / "data"
    assert active_store_name(cfg, data, env={}) == "env"  # .env stays the default
    (data / "state" / "guard").mkdir(parents=True)
    (data / "state" / "guard" / "token.json").write_text('{"store": "keychain"}')
    assert active_store_name(cfg, data, env={}) == "keychain"
    assert active_store_name(cfg, data, env={"FBCM_TOKEN_STORE": "env"}) == "env"
    with pytest.raises(TokenStoreError):
        active_store_name(cfg, data, env={"FBCM_TOKEN_STORE": "cloud"})
    assert get_token(cfg, data, env={"META_ACCESS_TOKEN": "X"}, env_path=env_file) == ("X", "environment")


# --- status -------------------------------------------------------------------

def test_status_never_prints_the_token_and_warns_before_expiry(tmp_path, env_file, capsys):
    g = FakeGraph(expires_in_days=6)
    assert token_cli.cmd_status(ns(tmp_path, env_file), load_config(), transport=g) == 0
    out, errout = capsys.readouterr()
    assert TOKEN not in out + errout
    status = json.loads(out)
    assert status["fingerprint"] == fingerprint(TOKEN) and status["store"] == "env"
    assert status["is_valid"] is True and 5 < status["days_left"] < 7
    assert "warning level 7 days" in errout
    meta_text = (tmp_path / "data" / "state" / "guard" / "token.json").read_text()
    assert TOKEN not in meta_text and "user_id" not in meta_text
    assert stat.S_IMODE((tmp_path / "data" / "state" / "guard" / "token.json").stat().st_mode) == 0o600
    # cached for a day
    token_cli.cmd_status(ns(tmp_path, env_file), load_config(), transport=g)
    assert len(g.calls) == 1
    args = ns(tmp_path, env_file)
    args.refresh = True
    token_cli.cmd_status(args, load_config(), transport=g)
    assert len(g.calls) == 2


@pytest.mark.parametrize("days,level", [(13, 14), (2.5, 3), (30, None)])
def test_expiry_warning_levels(days, level):
    now = time.time()
    w = token_cli.expiry_warnings({"expires_at": now + days * 86400}, [14, 7, 3], now)
    assert (f"warning level {level} days" in w[0]) if level else w == []
    assert "EXPIRED" in token_cli.expiry_warnings({"expires_at": now - 10}, [14], now)[0]
    assert token_cli.expiry_warnings({"expires_at": 0}, [14], now) == []  # never expires


def test_status_invalid_token_trips_the_kill_switch(tmp_path, env_file, capsys):
    g = FakeGraph(valid=False, error={"code": 190, "subcode": 463, "message": "Session has expired"})
    assert token_cli.cmd_status(ns(tmp_path, env_file), load_config(), transport=g) == 1
    errout = capsys.readouterr().err
    assert "190/463" in errout and TOKEN not in errout
    assert Guard(tmp_path / "data", env={}).kill_reason().startswith("token")


@pytest.mark.parametrize("code,subcode,needle,killed", [
    (190, 460, "password changed", True), (459, None, "checkpointed", True), (464, None, "unconfirmed", True),
    (10, None, "debug_token failed", False),  # debug_token permission denial is not an account risk
])
def test_status_maps_api_errors(tmp_path, env_file, capsys, code, subcode, needle, killed):
    g = Guard(tmp_path / "data", load_config(), env={}, sleep=lambda s: None, log=lambda m: None)
    t = fetch_ads.GraphTransport(FakeSession([err(code, subcode)]), guard=g, sleep=lambda s: None,
                                 log=lambda m: None, soft_kinds=("permission",))
    assert token_cli.cmd_status(ns(tmp_path, env_file), load_config(), transport=t) == 2
    assert needle in capsys.readouterr().err
    assert (g.kill_reason() is not None) == killed


def test_status_refuses_a_world_readable_env(tmp_path, env_file, capsys):
    env_file.chmod(0o644)
    assert token_cli.cmd_status(ns(tmp_path, env_file), load_config(), transport=FakeGraph()) == 2
    assert "chmod 600" in capsys.readouterr().err


# --- rotate -------------------------------------------------------------------

class TTY(io.StringIO):
    def isatty(self):
        return True


def test_rotate_refuses_without_a_terminal(tmp_path, env_file, capsys):
    assert token_cli.cmd_rotate(ns(tmp_path, env_file), load_config(), stdin=io.StringIO(),
                                transport=FakeGraph()) == 2
    assert "interactive Terminal" in capsys.readouterr().err


def test_rotate_to_keychain_stores_new_token_and_leaves_env(tmp_path, env_file, security, monkeypatch, capsys):
    monkeypatch.setenv("FBCM_SECURITY_BIN", security)
    answers = iter(["EAAshortLivedSyntheticToken0000000", SECRET])
    g = FakeGraph(expires_in_days=60)
    args = ns(tmp_path, env_file)
    args.to = "keychain"
    rc = token_cli.cmd_rotate(args, load_config(), stdin=TTY(), prompt=lambda _: next(answers), transport=g)
    out, errout = capsys.readouterr()
    assert rc == 0
    assert NEW_TOKEN not in out + errout and SECRET not in out + errout
    assert f"fingerprint {fingerprint(NEW_TOKEN)}" in out and "(60 days)" in out
    assert KeychainStore(security).get() == NEW_TOKEN
    assert f"META_ACCESS_TOKEN={TOKEN}" in env_file.read_text()  # old .env token untouched
    assert read_meta(tmp_path / "data")["store"] == "keychain"
    exchange = g.calls[0][1]
    assert exchange["grant_type"] == "fb_exchange_token" and exchange["client_id"] == "1122"
    assert SECRET not in (tmp_path / "data" / "state" / "guard" / "token.json").read_text()


def test_rotate_works_while_killed_for_token(tmp_path, env_file, capsys):
    Guard(tmp_path / "data", env={}).kill("token dead", kind="token")
    answers = iter(["EAAshortLivedSyntheticToken0000000", SECRET])
    rc = token_cli.cmd_rotate(ns(tmp_path, env_file), load_config(), stdin=TTY(), prompt=lambda _: next(answers),
                              transport=FakeGraph(expires_in_days=60))
    assert rc == 0 and EnvFileStore(env_file).get() == NEW_TOKEN
    assert "kill switch is still on" in capsys.readouterr().out


# --- leak scan and scrubbing ------------------------------------------------------

def test_scan_reports_paths_only(tmp_path, env_file, capsys):
    tree = tmp_path / "tree"
    (tree / "logs").mkdir(parents=True)
    (tree / "logs" / "leak.log").write_text(f"GET x?access_token={TOKEN}\n")
    (tree / "notes.md").write_text("an EAA" + "x" * 30 + " token-shaped string\n")
    (tree / "clean.md").write_text("nothing here\n")
    args = ns(tmp_path, env_file)
    args.paths = [str(tree)]
    assert token_cli.cmd_scan(args, load_config()) == 1
    out = capsys.readouterr().out
    assert "leak.log: exact token" in out and "notes.md: token-shaped string" in out
    assert "clean.md" not in out and TOKEN not in out and "EAAx" not in out


def test_scrub_covers_exchange_and_debug_params():
    s = scrub_tokens(f"oauth/access_token?grant_type=fb_exchange_token&client_secret={SECRET}"
                     f"&fb_exchange_token=SHORT&x=1 debug_token?input_token={TOKEN}")
    assert SECRET not in s and "SHORT" not in s and TOKEN not in s and "grant_type=fb_exchange_token" in s
