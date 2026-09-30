#!/usr/bin/env python3
"""Deterministic guard in front of every Meta Graph API call (policy: config/guard.yaml).

It enforces, with state persisted in data/state/guard/ so separate processes share it:
- a kill switch (data/state/guard/KILL, or FBCM_KILL=1) that only a human can clear;
- a cooldown after throttling, high usage headers or an open circuit breaker;
- per-run, per-hour and per-day call caps, and spacing between calls;
- one run at a time (flock on data/state/guard/run.lock);
- a run ledger per fetch in data/runs/<run_id>.json.

Error codes that mean "stop" trip the kill switch: 190 (token), 459/464 (account
checkpoint), 368 (policy block), 10 and 200-299 (permission), 2332002/2332004 (access),
613/1996 (inconsistent request volume). Throttle codes are never retried.

CLI:
  python scripts/guard.py status
  python scripts/guard.py preflight           # exit 0 when a live run may start
  python scripts/guard.py kill "<reason>"
  python scripts/guard.py unkill              # interactive terminal only (typed confirmation)
  python scripts/guard.py clear-cooldown      # interactive terminal only (typed confirmation)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from common import DATA_DIR, GUARD_CONFIG_PATH, iso_z, scrub_tokens, utc_now, write_private

THROTTLE_CODES = frozenset({4, 17, 32, 341, 613} | set(range(80000, 80015)))
TRANSIENT_CODES = frozenset({1, 2})
HOUR, DAY = 3600, 86400

DEFAULTS = {
    "api": {
        "min_interval_s": 1.5, "max_calls_per_run": 200, "max_calls_per_hour": 150,
        "max_calls_per_day": 600, "usage_soft_pct": 75, "usage_soft_sleep_s": 300,
        "usage_hard_pct": 90, "usage_hard_cooldown_min": 60, "throttle_cooldown_min": 60,
        "transient_retries": 3, "transient_delays_s": [2, 8, 30], "max_pages_per_query": 50,
        "breaker_failures": 5, "breaker_cooldown_min": 15,
    },
    "token": {"store": "env", "warn_days": [14, 7, 3], "status_cache_hours": 24},
}

KILL_HINTS = {
    "token": "the access token is invalid or expired. Run: .venv/bin/python scripts/token_cli.py status, "
             "then rotate it in your own Terminal (README 'Long-lived token')",
    "account": "Meta checkpointed or did not confirm the account. A human logs in at facebook.com and "
               "resolves it; the agent never does",
    "policy": "Meta temporarily blocked the account for a policy reason. Stop everything until a human reviews",
    "permission": "permission denied or Ad Library access not approved (or an app token was used)",
    "volume": "Meta flagged inconsistent request volume (613/1996). Review the ledger before any new run",
}


class GuardStop(RuntimeError):
    """The guard refused a call or a run. `kind`: kill, cooldown, cap, lock."""

    def __init__(self, message: str, kind: str):
        super().__init__(scrub_tokens(message))
        self.kind = kind


def load_config(path: Path = GUARD_CONFIG_PATH) -> dict:
    doc = yaml.safe_load(path.read_text()) if path.exists() else {}
    return {sec: {**vals, **((doc or {}).get(sec) or {})} for sec, vals in DEFAULTS.items()}


def classify_error(code, subcode) -> str | None:
    """Kill-switch reason for a Graph error, or None when the error does not trip it."""
    codes = {c for c in (code, subcode) if isinstance(c, int)}
    if 190 in codes:
        return "token"
    if codes & {459, 464}:
        return "account"
    if 368 in codes:
        return "policy"
    if code == 613 and subcode == 1996:
        return "volume"
    if codes & {10, 2332002, 2332004} or any(200 <= c <= 299 for c in codes):
        return "permission"
    return None


def _lower(headers) -> dict:
    return {str(k).lower(): v for k, v in (headers or {}).items()}


def usage_from_headers(headers) -> tuple[dict[str, float], float]:
    """Peak percentage per usage header, and the longest estimated_time_to_regain_access (minutes).

    X-App-Usage: {"call_count": pct, "total_cputime": pct, "total_time": pct}
    X-Business-Use-Case-Usage: {"<id>": [{"call_count": .., "total_time": .., "total_cputime": ..,
                                          "estimated_time_to_regain_access": minutes, ...}]}
    X-Ad-Account-Usage: {"acc_id_util_pct": pct, "reset_time_duration": s, ...}
    """
    h = _lower(headers)
    peaks: dict[str, float] = {}
    eta = 0.0

    def parse(name):
        raw = h.get(name)
        if not raw:
            return None
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None

    def pct_max(d: dict, keys) -> float | None:
        vals = []
        for k in keys:
            try:
                vals.append(float(d[k]))
            except (KeyError, TypeError, ValueError):
                pass
        return max(vals) if vals else None

    app = parse("x-app-usage")
    if isinstance(app, dict) and (p := pct_max(app, ("call_count", "total_cputime", "total_time"))) is not None:
        peaks["x-app-usage"] = p
    buc = parse("x-business-use-case-usage")
    if isinstance(buc, dict):
        for entries in buc.values():
            for e in entries if isinstance(entries, list) else [entries]:
                if not isinstance(e, dict):
                    continue
                p = pct_max(e, ("call_count", "total_cputime", "total_time"))
                if p is not None:
                    peaks["x-business-use-case-usage"] = max(p, peaks.get("x-business-use-case-usage", 0))
                try:
                    eta = max(eta, float(e.get("estimated_time_to_regain_access") or 0))
                except (TypeError, ValueError):
                    pass
    acct = parse("x-ad-account-usage")
    if isinstance(acct, dict) and (p := pct_max(acct, ("acc_id_util_pct",))) is not None:
        peaks["x-ad-account-usage"] = p
    return peaks, eta


class Guard:
    def __init__(self, data_dir: Path = DATA_DIR, config: dict | None = None, *, live: bool = True,
                 clock=time.time, sleep=time.sleep, log=None, env=None, jitter=random.uniform,
                 respect_kill: bool = True):
        """respect_kill=False is only for `token_cli.py rotate`, run by a human at a terminal:
        replacing a dead or leaked token is how a token kill is resolved. Caps and cooldowns still apply."""
        self.cfg = config or load_config()
        self.respect_kill = respect_kill
        self.api = self.cfg["api"]
        self.live = live
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "state" / "guard"
        self.runs_dir = self.data_dir / "runs"
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.log = log or (lambda msg: print(scrub_tokens(msg), file=sys.stderr))
        self.env = os.environ if env is None else env
        self.run_calls = 0
        self.ledger: dict | None = None

    # --- files ------------------------------------------------------------

    @property
    def kill_path(self) -> Path:
        return self.state_dir / "KILL"

    def _read_json(self, name: str, default):
        path = self.state_dir / name
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return default

    def _write_json(self, name: str, obj) -> None:
        write_private(self.state_dir / name, json.dumps(obj, indent=2) + "\n")

    def _event(self, event: str, **fields) -> None:
        line = json.dumps({"at": iso_z(utc_now()), "event": event, **fields})
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_dir / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(scrub_tokens(line) + "\n")
        if self.ledger is not None:
            self.ledger["events"].append({"event": event, **fields})

    # --- kill switch --------------------------------------------------------

    def kill_reason(self) -> str | None:
        if self.env.get("FBCM_KILL") == "1":
            return "FBCM_KILL=1 is set in the environment"
        if self.kill_path.exists():
            return self.kill_path.read_text().strip() or "kill switch file present"
        return None

    def kill(self, reason: str, *, kind: str = "manual", code=None, subcode=None) -> None:
        where = f", code {code}" + (f"/{subcode}" if subcode else "") if code else ""
        text = f"{kind}: {scrub_tokens(reason)} (at {iso_z(utc_now())}{where})"
        write_private(self.kill_path, text + "\n")
        self._event("kill", kind=kind, reason=scrub_tokens(reason), code=code, subcode=subcode)
        if self.ledger is not None:
            self.ledger["kill_tripped"] = text
        self.log(f"KILL SWITCH ON - {text}")

    def unkill(self) -> None:
        self.kill_path.unlink(missing_ok=True)
        self._event("unkill")

    # --- cooldown -----------------------------------------------------------

    def cooldown(self) -> dict | None:
        cd = self._read_json("cooldown.json", None)
        return cd if cd and cd.get("until", 0) > self.clock() else None

    def set_cooldown(self, minutes: float, reason: str, code=None) -> None:
        until = self.clock() + minutes * 60
        old = self._read_json("cooldown.json", None) or {}
        if old.get("until", 0) > until:
            return
        cd = {"until": until, "until_iso": iso_z(datetime.fromtimestamp(until, timezone.utc)),
              "reason": scrub_tokens(reason), "code": code}
        self._write_json("cooldown.json", cd)
        self._event("cooldown", minutes=minutes, reason=cd["reason"], code=code)
        if self.ledger is not None:
            self.ledger["cooldowns"].append(cd)
        self.log(f"cooldown until {cd['until_iso']}: {cd['reason']}")

    def clear_cooldown(self) -> None:
        (self.state_dir / "cooldown.json").unlink(missing_ok=True)
        self._event("cooldown-cleared")

    # --- preflight and lock -------------------------------------------------

    def preflight(self) -> None:
        if self.respect_kill and (reason := self.kill_reason()):
            raise GuardStop(f"kill switch is on: {reason}. Only a human can clear it "
                            "(python scripts/guard.py unkill in a terminal).", "kill")
        if self.live and (cd := self.cooldown()):
            raise GuardStop(f"cooling down until {cd['until_iso']}: {cd['reason']}", "cooldown")

    @contextmanager
    def lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_dir / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GuardStop("another fetch run holds the lock (data/state/guard/run.lock); "
                                "wait for it to finish", "lock") from None
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield self
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    # --- usage accounting ---------------------------------------------------

    def _usage(self) -> dict:
        u = self._read_json("usage.json", {})
        now = self.clock()
        u["calls"] = [t for t in u.get("calls", []) if t > now - DAY]
        u.setdefault("last_call", 0)
        u.setdefault("consecutive_failures", 0)
        return u

    def counts(self) -> dict:
        u, now = self._usage(), self.clock()
        return {"hour": sum(1 for t in u["calls"] if t > now - HOUR), "day": len(u["calls"]),
                "run": self.run_calls, "consecutive_failures": u["consecutive_failures"]}

    def before_call(self) -> None:
        """Refuse or delay the next API call. Raises GuardStop."""
        self.preflight()
        if self.run_calls >= self.api["max_calls_per_run"]:
            raise GuardStop(f"run cap reached ({self.api['max_calls_per_run']} calls per run)", "cap")
        if not self.live:
            self.run_calls += 1
            self._count_ledger_call()
            return
        u = self._usage()
        now = self.clock()
        hour = sum(1 for t in u["calls"] if t > now - HOUR)
        if hour >= self.api["max_calls_per_hour"]:
            raise GuardStop(f"hourly cap reached ({hour}/{self.api['max_calls_per_hour']} calls in the last hour)",
                            "cap")
        if len(u["calls"]) >= self.api["max_calls_per_day"]:
            raise GuardStop(f"daily cap reached ({len(u['calls'])}/{self.api['max_calls_per_day']} calls "
                            "in the last 24 hours)", "cap")
        interval = float(self.api["min_interval_s"])
        wait = u["last_call"] + interval + self.jitter(0, interval / 2) - now
        if wait > 0:
            self.sleep(wait)
        now = self.clock()
        u["calls"].append(now)
        u["last_call"] = now
        self._write_json("usage.json", u)
        self.run_calls += 1
        self._count_ledger_call()

    def _count_ledger_call(self) -> None:
        if self.ledger is not None:
            self.ledger["calls"] += 1

    def _failures(self, delta: int | None) -> int:
        if not self.live:
            return 0
        u = self._usage()
        u["consecutive_failures"] = 0 if delta is None else u["consecutive_failures"] + delta
        self._write_json("usage.json", u)
        return u["consecutive_failures"]

    def after_success(self, headers) -> None:
        self._failures(None)
        self.check_usage(headers)

    def check_usage(self, headers) -> None:
        peaks, eta = usage_from_headers(headers)
        if self.ledger is not None:
            for k, v in peaks.items():
                self.ledger["usage_peak"][k] = max(v, self.ledger["usage_peak"].get(k, 0))
        if not peaks:
            return
        name, peak = max(peaks.items(), key=lambda kv: kv[1])
        if peak >= self.api["usage_hard_pct"]:
            self.set_cooldown(max(self.api["usage_hard_cooldown_min"], eta),
                              f"{name} at {peak:.0f}% (hard limit {self.api['usage_hard_pct']}%)")
        elif peak >= self.api["usage_soft_pct"]:
            self._event("usage-soft", header=name, pct=peak)
            self.log(f"{name} at {peak:.0f}%, pausing {self.api['usage_soft_sleep_s']}s")
            self.sleep(self.api["usage_soft_sleep_s"])

    def after_failure(self, *, code=None, subcode=None, status=None, headers=None, message: str = "",
                      soft_kinds: tuple[str, ...] = ()) -> None:
        """Record a failed call; trip the kill switch, cool down or open the breaker as policy says.

        soft_kinds: kill reasons that only fail this call (debug_token may deny introspection
        with a permission error without the account being at risk).
        """
        if self.ledger is not None:
            self.ledger["errors"].append({"code": code, "subcode": subcode, "http_status": status})
        peaks, eta = usage_from_headers(headers)
        if self.ledger is not None:
            for k, v in peaks.items():
                self.ledger["usage_peak"][k] = max(v, self.ledger["usage_peak"].get(k, 0))
        if (kind := classify_error(code, subcode)) and kind not in soft_kinds:
            self.kill(f"Graph error {code}" + (f"/{subcode}" if subcode else "") + f": {KILL_HINTS[kind]}",
                      kind=kind, code=code, subcode=subcode)
            return
        if code in THROTTLE_CODES:
            self.set_cooldown(max(self.api["throttle_cooldown_min"], eta),
                              f"throttled by Meta (code {code}); not retrying", code=code)
            return
        if self.live:
            n = self._failures(1)
            if n >= self.api["breaker_failures"]:
                self.set_cooldown(self.api["breaker_cooldown_min"],
                                  f"circuit breaker open after {n} consecutive failed calls")
                self._failures(None)

    # --- run ledger ---------------------------------------------------------

    def start_run(self, run_id: str, **meta) -> dict:
        self.run_calls = 0
        self.ledger = {"run_id": run_id, "mode": "live" if self.live else "fixture",
                       "started_at": iso_z(utc_now()), "finished_at": None, "status": "running",
                       "calls": 0, "pages": 0, "windows_done": [], "windows_missing": [], "errors": [],
                       "usage_peak": {}, "cooldowns": [], "kill_tripped": None, "events": [], **meta}
        self.write_ledger()
        return self.ledger

    def write_ledger(self) -> Path | None:
        if self.ledger is None:
            return None
        path = self.runs_dir / f"{self.ledger['run_id']}.json"
        write_private(path, json.dumps(self.ledger, indent=2, ensure_ascii=False) + "\n")
        return path

    def close_run(self, status: str, **extra) -> Path | None:
        if self.ledger is None:
            return None
        self.ledger.update(status=status, finished_at=iso_z(utc_now()), **extra)
        path = self.write_ledger()
        self.ledger = None
        return path

    # --- reporting ----------------------------------------------------------

    def status(self) -> dict:
        runs = sorted(self.runs_dir.glob("*.json")) if self.runs_dir.exists() else []
        last = json.loads(runs[-1].read_text()) if runs else None
        c = self.counts()
        return {
            "kill_switch": self.kill_reason(),
            "cooldown": self.cooldown(),
            "calls_last_hour": f"{c['hour']}/{self.api['max_calls_per_hour']}",
            "calls_last_24h": f"{c['day']}/{self.api['max_calls_per_day']}",
            "consecutive_failures": c["consecutive_failures"],
            "last_run": {k: last.get(k) for k in ("run_id", "mode", "status", "calls", "finished_at",
                                                  "windows_missing")} if last else None,
        }


def _confirm_interactive(word: str, stdin=None, out=None) -> bool:
    stdin, out = stdin or sys.stdin, out or sys.stderr
    if not stdin.isatty():
        print(f"refused: '{word}' needs a human at an interactive terminal (no pipes, agents or schedules).",
              file=out)
        return False
    print(f"Type '{word}' to confirm: ", end="", file=out, flush=True)
    return stdin.readline().strip() == word


def main(argv=None, *, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="Meta API guard: status, preflight, kill switch")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("preflight")
    k = sub.add_parser("kill")
    k.add_argument("reason")
    sub.add_parser("unkill")
    sub.add_parser("clear-cooldown")
    args = ap.parse_args(argv)
    g = Guard(args.data_dir)
    if args.cmd == "status":
        print(json.dumps(g.status(), indent=2))
        return 0
    if args.cmd == "preflight":
        try:
            g.preflight()
        except GuardStop as exc:
            print(f"blocked: {exc}", file=sys.stderr)
            return 1
        print("clear")
        return 0
    if args.cmd == "kill":
        g.kill(args.reason)
        return 0
    if args.cmd == "unkill":
        if g.env.get("FBCM_KILL") == "1":
            print("FBCM_KILL=1 is set in the environment; unset it yourself.", file=sys.stderr)
            return 2
        if not g.kill_path.exists():
            print("kill switch is not on")
            return 0
        print(f"kill switch: {g.kill_reason()}", file=sys.stderr)
        if not _confirm_interactive("unkill", stdin):
            return 2
        g.unkill()
        print("kill switch cleared")
        return 0
    if args.cmd == "clear-cooldown":
        if not g.cooldown():
            print("no active cooldown")
            return 0
        if not _confirm_interactive("clear-cooldown", stdin):
            return 2
        g.clear_cooldown()
        print("cooldown cleared")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
