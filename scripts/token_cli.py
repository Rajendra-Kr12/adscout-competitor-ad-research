#!/usr/bin/env python3
"""Meta token lifecycle: status (debug_token, expiry warnings), rotate, leak scan.

The token is never printed, logged or stored outside its store (see token_store.py).
Output shows only its fingerprint (sha256[:8]); data/state/guard/token.json holds metadata.

  python scripts/token_cli.py status [--refresh]   # debug_token at most once a day unless --refresh
  python scripts/token_cli.py rotate [--to env|keychain]
      Captain only, in your own Terminal (never in a Claude chat): asks for the short-lived
      Graph API Explorer token and the App Secret (not echoed), exchanges them for a long-lived
      token, stores it and prints its expiry. The App Secret is never stored.
  python scripts/token_cli.py scan [PATH ...]       # files containing the token: paths only
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import DATA_DIR, DEFAULT_API_VERSION, ROOT, iso_z, load_env, scrub_tokens, utc_now, write_private
from guard import Guard, GuardStop, load_config
from token_store import (
    STORES,
    USER_TOKEN_RE,
    TokenStoreError,
    active_store_name,
    fingerprint,
    get_token,
    make_store,
    meta_path,
    read_meta,
)

# Error meanings for 190/459/464 (Graph API error handling guide). Guard trips the kill switch on these.
ERROR_HELP = {
    190: "token invalid or expired: mint a new Explorer token and run `token_cli.py rotate` in your Terminal",
    458: "the app was removed from your account (190/458): re-add it, then rotate",
    460: "your Facebook password changed (190/460): mint a new token and rotate",
    463: "the token expired (190/463): mint a new token and rotate",
    467: "the token is invalid (190/467): mint a new token and rotate",
    459: "account checkpointed (459): log in at facebook.com yourself and resolve it; the agent never does",
    464: "unconfirmed user (464): confirm the account at facebook.com yourself",
}
META_FIELDS = ("app_id", "type", "application", "issued_at", "expires_at", "data_access_expires_at",
               "is_valid", "scopes")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


def error_help(code, subcode) -> str | None:
    return ERROR_HELP.get(subcode) or ERROR_HELP.get(code)


def _day(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "never"


def expiry_warnings(meta: dict, warn_days, now: float) -> list[str]:
    out = []
    if meta.get("is_valid") is False:
        err = meta.get("error") or {}
        out.append("TOKEN INVALID: " + (error_help(err.get("code"), err.get("subcode"))
                                        or scrub_tokens(str(err.get("message") or "see debug_token"))))
    for key, label in (("expires_at", "token"), ("data_access_expires_at", "data access")):
        ts = meta.get(key)
        if not ts:  # 0 = never expires; None = unknown
            continue
        days = (ts - now) / 86400
        if days <= 0:
            out.append(f"{label} EXPIRED on {_day(ts)}: rotate the token")
            continue
        levels = [d for d in warn_days if days <= d]
        if levels:
            out.append(f"{label} expires in {days:.0f} day(s), on {_day(ts)} (warning level {min(levels)} days): "
                       "rotate soon")
    return out


def debug_token(token: str, transport, api_version: str) -> dict:
    body = transport.get(f"https://graph.facebook.com/{api_version}/debug_token",
                         {"input_token": token, "access_token": token})
    return body.get("data") or {}


def record_meta(data_dir: Path, token: str, data: dict, now: float, store: str | None = None) -> dict:
    old = read_meta(data_dir)
    meta = {"fingerprint": fingerprint(token), "store": store or old.get("store"),
            **{k: data.get(k) for k in META_FIELDS}, "checked_at": now, "checked_at_iso": iso_z(utc_now())}
    if data.get("error"):
        e = data["error"]
        meta["error"] = {"code": e.get("code"), "subcode": e.get("subcode"),
                         "message": scrub_tokens(str(e.get("message") or ""))}
    if meta["store"] is None:
        meta.pop("store")
    write_private(meta_path(data_dir), json.dumps(meta, indent=2) + "\n")
    return meta


def ensure_status(token: str, guard: Guard, transport, api_version: str, data_dir: Path,
                  *, refresh: bool = False, now: float | None = None, log=None) -> dict:
    """Cached debug_token metadata (at most one call per status_cache_hours); prints warnings.

    Trips the kill switch when Meta says the token is invalid.
    """
    log = log or (lambda m: print(m, file=sys.stderr))
    now = time.time() if now is None else now
    cfg = guard.cfg["token"]
    meta = read_meta(data_dir)
    fresh = meta.get("fingerprint") == fingerprint(token) and \
        now - float(meta.get("checked_at") or 0) < float(cfg["status_cache_hours"]) * 3600
    if refresh or not fresh:
        meta = record_meta(data_dir, token, debug_token(token, transport, api_version), now)
    for w in expiry_warnings(meta, cfg["warn_days"], now):
        log(f"WARNING: {w}")
    if meta.get("is_valid") is False and guard.kill_reason() is None:
        err = meta.get("error") or {}
        guard.kill(f"debug_token reports the token invalid: {error_help(err.get('code'), err.get('subcode')) or 'rotate it'}",
                   kind="token", code=err.get("code"), subcode=err.get("subcode"))
    return meta


def _transport(guard: Guard, soft_kinds=()):
    from fetch_ads import GraphTransport
    return GraphTransport(guard=guard, retries=guard.api["transient_retries"],
                          delays=guard.api["transient_delays_s"], soft_kinds=soft_kinds)


def cmd_status(args, cfg, transport=None, out=None) -> int:
    out = out or sys.stdout
    try:
        token, source = get_token(cfg, args.data_dir, env_path=args.env_file)
    except TokenStoreError as exc:
        print(f"token store error: {exc}", file=sys.stderr)
        return 2
    if not token:
        print(f"no token in the {source} store. See README 'Long-lived token'.", file=sys.stderr)
        return 1
    guard = Guard(args.data_dir, cfg)
    api_version = load_env(args.env_file).get("META_API_VERSION") or DEFAULT_API_VERSION
    try:
        meta = ensure_status(token, guard, transport or _transport(guard, ("permission",)), api_version,
                             args.data_dir, refresh=args.refresh)
    except GuardStop as exc:
        print(f"guard refused the status check: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - report any API failure without the token
        code, sub = getattr(exc, "code", None), getattr(exc, "subcode", None)
        print(f"debug_token failed: {scrub_tokens(str(exc))}", file=sys.stderr)
        if hint := error_help(code, sub):
            print(f"meaning: {hint}", file=sys.stderr)
        return 2
    now = time.time()
    days = (meta["expires_at"] - now) / 86400 if meta.get("expires_at") else None
    print(json.dumps({
        "fingerprint": fingerprint(token), "store": source, "app_id": meta.get("app_id"),
        "type": meta.get("type"), "is_valid": meta.get("is_valid"),
        "expires": _day(meta.get("expires_at")) if meta.get("expires_at") is not None else None,
        "days_left": round(days, 1) if days is not None else None,
        "data_access_expires": _day(meta.get("data_access_expires_at"))
        if meta.get("data_access_expires_at") is not None else None,
        "checked_at": meta.get("checked_at_iso"),
    }, indent=2), file=out)
    return 0 if meta.get("is_valid") is not False else 1


def cmd_rotate(args, cfg, *, stdin=None, prompt=getpass.getpass, transport=None, out=None) -> int:
    stdin, out = stdin or sys.stdin, out or sys.stdout
    if not stdin.isatty():
        print("refused: rotate asks for secrets and must run in your own interactive Terminal, "
              "never from Claude, a pipe or a schedule.", file=sys.stderr)
        return 2
    target = args.to or active_store_name(cfg, args.data_dir)
    env = load_env(args.env_file)
    api_version = env.get("META_API_VERSION") or DEFAULT_API_VERSION
    app_id = env.get("META_APP_ID") or input("Meta App ID: ").strip()
    short = prompt("Short-lived user token from Graph API Explorer (not shown): ").strip()
    secret = prompt("App Secret (not shown, never stored): ").strip()
    if not (app_id and short and secret):
        print("App ID, token and App Secret are all required.", file=sys.stderr)
        return 2
    guard = Guard(args.data_dir, cfg, respect_kill=False)
    t = transport or _transport(guard)
    try:
        body = t.get(f"https://graph.facebook.com/{api_version}/oauth/access_token",
                     {"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
                      "fb_exchange_token": short})
    except Exception as exc:  # noqa: BLE001
        print(f"exchange failed: {scrub_tokens(str(exc))}", file=sys.stderr)
        return 2
    token = body.get("access_token")
    if not token:
        print("exchange returned no access_token", file=sys.stderr)
        return 2
    try:
        make_store(target, env_path=args.env_file).set(token)
    except TokenStoreError as exc:
        print(f"could not store the token: {exc}", file=sys.stderr)
        return 2
    meta = record_meta(args.data_dir, token, {}, time.time(), store=target)
    try:
        meta = ensure_status(token, guard, t, api_version, args.data_dir, refresh=True)
    except Exception as exc:  # noqa: BLE001
        print(f"stored, but debug_token failed: {scrub_tokens(str(exc))}", file=sys.stderr)
    days = (meta["expires_at"] - time.time()) / 86400 if meta.get("expires_at") else None
    print(f"stored in {target}: fingerprint {fingerprint(token)}, expires {_day(meta.get('expires_at'))}"
          + (f" ({days:.0f} days)" if days is not None else ""), file=out)
    if target != "env":
        print("The old token in .env was left in place. Remove the META_ACCESS_TOKEN line yourself once "
              "the new store works.", file=out)
    if guard.kill_reason():
        print(f"The kill switch is still on ({guard.kill_reason()}). Clear it with "
              "`python scripts/guard.py unkill` when you are ready.", file=out)
    return 0


def _walk(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def scan_paths(paths: list[Path], token: str | None, skip: set[Path] = frozenset()) -> list[tuple[Path, str]]:
    """Files that contain the token (exact) or any Meta-user-token-shaped string. No contents returned."""
    hits = []
    needle = token.encode() if token else None
    for root in paths:
        for f in _walk(root):
            if f.resolve() in skip or not f.is_file():
                continue
            try:
                if f.stat().st_size > 50_000_000:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if needle and needle in data:
                hits.append((f, "exact token"))
            elif USER_TOKEN_RE.search(data.decode("utf-8", "ignore")):
                hits.append((f, "token-shaped string"))
    return hits


def cmd_scan(args, cfg, out=None) -> int:
    out = out or sys.stdout
    try:
        token, _ = get_token(cfg, args.data_dir, env_path=args.env_file)
    except TokenStoreError:
        token = None
    paths = [Path(p) for p in args.paths] or [ROOT]
    hits = scan_paths(paths, token, skip={args.env_file.resolve()})
    for path, kind in hits:
        print(f"{path}: {kind}", file=out)
    print(f"{len(hits)} file(s) flagged" + ("" if token else " (no stored token; shape check only)"),
          file=sys.stderr)
    return 1 if hits else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Meta token status, rotation and leak scan")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--refresh", action="store_true", help="call debug_token even if checked in the last day")
    r = sub.add_parser("rotate")
    r.add_argument("--to", choices=STORES, help="store to write (default: the active store)")
    sc = sub.add_parser("scan")
    sc.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    cfg = load_config()
    return {"status": cmd_status, "rotate": cmd_rotate, "scan": cmd_scan}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
