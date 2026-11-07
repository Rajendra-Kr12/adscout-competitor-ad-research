#!/usr/bin/env python3
"""Unsplash photo suggestions for the handoff's "Reference links (human picks)" block.

Searches the official Unsplash API (https://unsplash.com/documentation) for a query taken from the project's
product category and writes 3-5 candidate reference photos into handoff/<slug>.md, clearly marked as
suggestions a human confirms. Stock photography for mood, light and colour, not competitor creative.

API terms followed (https://unsplash.com/api-terms, https://help.unsplash.com/en/articles/2511245):
- photos are hotlinked from the URLs the API returned; nothing is downloaded, mirrored or stored (data/unsplash/
  keeps ids, links and credits only, 180 days);
- photographer and Unsplash are credited with UTM-tagged links on every suggestion;
- `choose` calls the download-tracking endpoint when a human picks a photo, and only then;
- results are never used to train or fine-tune any model;
- demo mode allows 50 requests/hour, so a local limiter (config/guard.yaml `unsplash:`, state in
  data/state/unsplash/, ledger.jsonl) caps calls per run, hour and day, spaces them, and cools down on a rate
  limit or a rejected key. It never retries.

The key is read only by unsplash_store.py and sent in the Authorization header, never in a URL. Without a key
(or if the live call fails) the block is filled from a clearly labelled SAMPLE result, so a demo never breaks.
This is not a Meta call and does not go through guard.py; it has its own limiter and no kill-switch sharing.

Usage:
  python scripts/unsplash_refs.py suggest <slug> [--query "natural skincare"] [--count 4] [--sample] [--dry-run]
  python scripts/unsplash_refs.py choose  <slug> <S1|photo-id>
  python scripts/unsplash_refs.py status
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

import project as projects
import unsplash_store
from common import DATA_DIR, GUARD_CONFIG_PATH, ROOT, assert_no_token, iso_z, scrub_tokens, utc_now, write_private
from unsplash_store import UnsplashKeyError

API_BASE = "https://api.unsplash.com"
API_HOST = "api.unsplash.com"
PHOTO_HOSTS = frozenset({"images.unsplash.com", "plus.unsplash.com", "unsplash.com"})
UTM = "utm_source=fbcm_agent&utm_medium=referral"
HANDOFF_DIR = ROOT / "handoff"
SAMPLE_PATH = ROOT / "tests" / "fixtures" / "unsplash_search_sample.json"
SECTION = "## Reference links (human picks)"
START, END = "<!-- unsplash-suggestions:start", "<!-- unsplash-suggestions:end -->"
PLACEHOLDER = re.compile(r"^- \[ \] \d+\. \(link\)")
MIN_COUNT, MAX_COUNT = 3, 5
LEADING_PLACES = {"uk", "gb", "eu", "us", "usa", "european", "british", "indian", "india", "europe", "global"}

DEFAULT_POLICY = {
    "min_interval_s": 2.0,
    "max_calls_per_run": 3,
    "max_calls_per_hour": 40,       # Unsplash demo mode allows 50/hour; stay under it
    "max_calls_per_day": 200,
    "rate_cooldown_min": 60,        # 429, 403 "rate limit", or X-Ratelimit-Remaining at the floor
    "key_cooldown_min": 1440,       # 401 or other 403: the key is wrong or revoked, stop for a day
    "low_remaining": 3,
    "timeout_s": 30,
}


class UnsplashError(RuntimeError):
    pass


class LimiterStop(RuntimeError):
    """The local limiter refused a call (cooldown or cap)."""


def load_policy(path: Path = GUARD_CONFIG_PATH) -> dict:
    doc = yaml.safe_load(path.read_text()) if path.exists() else {}
    return {**DEFAULT_POLICY, **((doc or {}).get("unsplash") or {})}


# --- limiter and ledger -----------------------------------------------------------------------------

class Limiter:
    """Caps, spacing, cooldown and a per-call ledger for Unsplash. State: data/state/unsplash/."""

    def __init__(self, data_dir: Path = DATA_DIR, policy: dict | None = None, *, clock=time.time,
                 sleep=time.sleep, jitter=random.uniform):
        self.policy = {**DEFAULT_POLICY, **(policy or {})}
        self.state_dir = Path(data_dir) / "state" / "unsplash"
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.run_calls = 0

    def _read(self, name: str, default):
        try:
            return json.loads((self.state_dir / name).read_text())
        except (FileNotFoundError, ValueError):
            return default

    def _usage(self) -> dict:
        u = self._read("usage.json", {})
        now = self.clock()
        u["calls"] = [t for t in u.get("calls", []) if t > now - 86400]
        u.setdefault("last_call", 0)
        return u

    def counts(self) -> dict:
        u, now = self._usage(), self.clock()
        return {"hour": sum(1 for t in u["calls"] if t > now - 3600), "day": len(u["calls"]), "run": self.run_calls}

    def cooldown(self) -> dict | None:
        cd = self._read("cooldown.json", None)
        return cd if cd and cd.get("until", 0) > self.clock() else None

    def before_call(self) -> None:
        if cd := self.cooldown():
            raise LimiterStop(f"Unsplash calls are paused until {cd['until_iso']}: {cd['reason']}")
        p = self.policy
        if self.run_calls >= p["max_calls_per_run"]:
            raise LimiterStop(f"run cap reached ({p['max_calls_per_run']} Unsplash calls per run)")
        u, now = self._usage(), self.clock()
        hour = sum(1 for t in u["calls"] if t > now - 3600)
        if hour >= p["max_calls_per_hour"]:
            raise LimiterStop(f"hourly cap reached ({hour}/{p['max_calls_per_hour']}; Unsplash demo mode allows 50)")
        if len(u["calls"]) >= p["max_calls_per_day"]:
            raise LimiterStop(f"daily cap reached ({len(u['calls'])}/{p['max_calls_per_day']})")
        wait = u["last_call"] + p["min_interval_s"] + self.jitter(0, p["min_interval_s"] / 2) - now
        if wait > 0:
            self.sleep(wait)
        now = self.clock()
        u["calls"].append(now)
        u["last_call"] = now
        write_private(self.state_dir / "usage.json", json.dumps(u) + "\n")
        self.run_calls += 1

    def set_cooldown(self, minutes: float, reason: str) -> None:
        until = self.clock() + minutes * 60
        old = self._read("cooldown.json", None) or {}
        if old.get("until", 0) > until:
            return
        cd = {"until": until, "until_iso": iso_z(datetime.fromtimestamp(until, timezone.utc)), "reason": reason}
        write_private(self.state_dir / "cooldown.json", json.dumps(cd) + "\n")

    def record(self, kind: str, label: str, status, headers, note: str = "") -> None:
        """Append one ledger line and apply cooldown policy. `label` is a query or photo id, never the key."""
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        remaining = h.get("x-ratelimit-remaining")
        line = {"at": iso_z(utc_now()), "kind": kind, "label": scrub_tokens(label)[:120], "status": status,
                "ratelimit_remaining": remaining, "note": scrub_tokens(note)[:200]}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_dir / "ledger.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(line) + "\n")
        p = self.policy
        if status == 429 or (status == 403 and "rate limit" in note.lower()):
            self.set_cooldown(p["rate_cooldown_min"], "Unsplash rate limit reached; not retrying")
        elif status in (401, 403):
            self.set_cooldown(p["key_cooldown_min"], f"Unsplash rejected the key (HTTP {status}); check the key")
        else:
            try:
                if remaining is not None and int(remaining) <= p["low_remaining"]:
                    self.set_cooldown(p["rate_cooldown_min"], f"Unsplash allowance nearly used ({remaining} left)")
            except ValueError:
                pass

    def status(self) -> dict:
        c = self.counts()
        return {"cooldown": self.cooldown(), "calls_last_hour": f"{c['hour']}/{self.policy['max_calls_per_hour']}",
                "calls_last_24h": f"{c['day']}/{self.policy['max_calls_per_day']}"}


# --- client -----------------------------------------------------------------------------------------

class Client:
    def __init__(self, key: str, limiter: Limiter, session=None):
        self._key = key
        self.limiter = limiter
        self.session = session or requests.Session()

    def _get(self, url: str, params: dict | None, kind: str, label: str) -> dict:
        host = urlparse(url).hostname
        if urlparse(url).scheme != "https" or host != API_HOST:
            raise UnsplashError(f"refusing to call {host!r}: only https://{API_HOST} is allowed")
        try:
            self.limiter.before_call()
        except LimiterStop as exc:
            raise UnsplashError(str(exc)) from None
        try:
            resp = self.session.get(url, params=params, timeout=self.limiter.policy["timeout_s"],
                                    headers={"Authorization": f"Client-ID {self._key}", "Accept-Version": "v1"})
        except requests.RequestException as exc:
            self.limiter.record(kind, label, None, None, f"network error: {type(exc).__name__}")
            raise UnsplashError(f"network error ({type(exc).__name__})") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        errors = " ".join(str(e) for e in body.get("errors", [])) if isinstance(body, dict) else ""
        text = errors or (resp.text if resp.status_code == 403 else "")
        self.limiter.record(kind, label, resp.status_code, resp.headers, text)
        if resp.status_code != 200:
            raise UnsplashError(f"HTTP {resp.status_code}: {scrub_tokens(errors)[:160] or 'no detail'}")
        return body

    def search(self, query: str, per_page: int, orientation: str | None = None) -> list[dict]:
        params = {"query": query, "per_page": per_page, "content_filter": "high"}
        if orientation:
            params["orientation"] = orientation
        body = self._get(f"{API_BASE}/search/photos", params, "search", query)
        return [s for s in (to_suggestion(p) for p in body.get("results", []) if isinstance(p, dict)) if s]

    def track_download(self, download_location: str, photo_id: str) -> None:
        """Unsplash asks apps to hit the photo's download endpoint when a user picks it. The photo is not fetched."""
        self._get(download_location, None, "download-track", photo_id)


# --- results ----------------------------------------------------------------------------------------

def clean(text, limit: int = 80) -> str:
    """Untrusted API text to one safe plain line: no markdown, HTML, URLs or control characters."""
    s = re.sub(r"(?i)access_token", "", str(text or ""))
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[^\w\s.,'&:!?-]", " ", s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def _utm(url: str) -> str:
    return url + ("&" if "?" in url else "?") + UTM


def _host_ok(url, hosts) -> bool:
    u = urlparse(str(url or ""))
    return u.scheme == "https" and u.hostname in hosts


def to_suggestion(photo: dict) -> dict | None:
    """Keep only what the handoff needs: ids, hotlink, credits. Drops a photo whose links are not Unsplash's."""
    try:
        links, urls, user = photo["links"], photo["urls"], photo["user"]
        pid = str(photo["id"])
        out = {"id": pid, "description": clean(photo.get("alt_description") or photo.get("description") or "photo"),
               "photographer": clean(user.get("name") or "Unknown", 60),
               "photographer_url": _utm(user["links"]["html"]), "photo_url": _utm(links["html"]),
               "image_url": urls["small"], "download_location": links["download_location"],
               "chosen": False}
    except (KeyError, TypeError):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", pid):
        return None
    if not (_host_ok(out["image_url"], PHOTO_HOSTS) and _host_ok(out["photo_url"], PHOTO_HOSTS)
            and _host_ok(out["photographer_url"], PHOTO_HOSTS)
            and _host_ok(out["download_location"], {API_HOST})):
        return None
    return out


def load_sample(path: Path = SAMPLE_PATH) -> list[dict]:
    body = json.loads(Path(path).read_text())
    return [s for s in (to_suggestion(p) for p in body["results"]) if s]


def derive_query(slug: str, projects_dir: Path = projects.PROJECTS_DIR) -> str | None:
    """Product category from the project title: the text before ' - ', minus a leading country word.
    'UK natural skincare - 8 brands, ...' -> 'natural skincare'. None when there is no project or no category."""
    if not projects.project_path(slug, projects_dir).exists():
        return None
    head = str(projects.load_project(slug, projects_dir).get("title") or "").split(" - ")[0]
    words = re.findall(r"[A-Za-z][A-Za-z'&-]*", head)
    while words and words[0].lower() in LEADING_PLACES:
        words.pop(0)
    return " ".join(words) or None


# --- handoff block ----------------------------------------------------------------------------------

def render_block(photos: list[dict], query: str, mode: str, note: str = "") -> list[str]:
    sample = mode == "sample"
    head = (f"SAMPLE RESULT, not a live search{f' ({note})' if note else ''}: placeholder photos so the demo runs without "
            "a key. Do not use." if sample else
            f"Suggestions from Unsplash for \"{query}\". NOT confirmed: a human picks, then runs `unsplash_refs.py choose`. "
            "Stock photography for mood, light and colour, not competitor creative; never for model training.")
    lines = [f"{START} source={mode} -->", head]
    for i, p in enumerate(photos, 1):
        mark = "x" if p.get("chosen") else " "
        status = " CONFIRMED by a human (download tracked)." if p.get("chosen") else ""
        lines.append(f"- [{mark}] S{i} \"{p['description']}\" - [photo]({p['photo_url']}) by "
                     f"[{p['photographer']}]({p['photographer_url']}) on [Unsplash](https://unsplash.com/?{UTM}) | "
                     f"[image]({p['image_url']}){status} - what to take from it:")
    lines.append(END)
    return lines


def apply_block(text: str, block: list[str]) -> str:
    """Put the block in the Reference links section. Replaces a previous block; drops only the unfilled
    placeholders (the human's own filled lines stay); inserts at the end of the section."""
    lines = text.split("\n")
    if START in text:
        s = next(i for i, l in enumerate(lines) if l.startswith(START))
        e = next(i for i, l in enumerate(lines) if l.strip() == END)
        lines[s:e + 1] = block
        return "\n".join(lines)
    try:
        h = lines.index(SECTION)
    except ValueError:
        raise UnsplashError(f"{SECTION!r} not found in the handoff; run export_handoff.py first") from None
    end = next((i for i in range(h + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = [l for l in lines[h + 1:end] if not PLACEHOLDER.match(l)]
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(lines[:h + 1] + body + block + [""] + lines[end:])


# --- state ------------------------------------------------------------------------------------------

def state_path(data_dir: Path, slug: str) -> Path:
    return Path(data_dir) / "unsplash" / f"{re.sub(r'[^A-Za-z0-9_.-]+', '-', slug)}.json"


def save_state(data_dir: Path, slug: str, state: dict) -> None:
    write_private(state_path(data_dir, slug), json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def load_state(data_dir: Path, slug: str) -> dict:
    try:
        return json.loads(state_path(data_dir, slug).read_text())
    except (FileNotFoundError, ValueError):
        raise UnsplashError(f"no suggestions stored for {slug!r}; run `unsplash_refs.py suggest {slug}` first") from None


def write_handoff(path: Path, state: dict) -> None:
    text = apply_block(path.read_text(), render_block(state["photos"], state["query"], state["mode"], state.get("note", "")))
    assert_no_token(text, str(path))
    path.write_text(text)


# --- commands ---------------------------------------------------------------------------------------

def make_client(limiter: Limiter, env=None, env_path: Path | None = None, session=None) -> tuple[Client | None, str]:
    """(client, '') when a usable key exists, else (None, reason). The reason never contains the key."""
    try:
        key, _ = unsplash_store.get_key(env=env, env_path=env_path)
    except UnsplashKeyError as exc:
        return None, str(exc)
    if key is None:
        return None, f"{unsplash_store.VAR} is not set"
    return Client(key, limiter, session), ""


def suggest(slug: str, *, query: str | None = None, count: int = 4, sample: bool = False, dry_run: bool = False,
            require_live: bool = False, orientation: str | None = None, data_dir: Path = DATA_DIR,
            handoff_dir: Path = HANDOFF_DIR, projects_dir: Path = projects.PROJECTS_DIR, limiter: Limiter | None = None,
            env=None, env_path: Path | None = None, session=None, out=print) -> int:
    count = max(MIN_COUNT, min(MAX_COUNT, count))
    query = clean(query, 80) if query else derive_query(slug, projects_dir)
    if not query:
        out(f"error: no product category found for {slug!r}; pass --query \"<category>\"", file=sys.stderr)
        return 2
    target = Path(handoff_dir) / f"{slug}.md"
    if not target.exists():
        out(f"error: {target} does not exist; run export_handoff.py {slug} first", file=sys.stderr)
        return 2
    limiter = limiter or Limiter(data_dir, load_policy())
    client, why = (None, "--sample was requested") if sample else make_client(limiter, env, env_path, session)
    if require_live and client is None:
        out(f"error: --require-live was given but no live search is possible: {why}", file=sys.stderr)
        return 2
    if dry_run:
        plan = (f"would search Unsplash for \"{query}\" ({count} photos, 1 call) and write suggestions into {target}"
                if client else f"would write a labelled SAMPLE block into {target} ({why})")
        out(f"# dry run: {plan}. No call is made; nothing written.")
        return 0
    mode, note, photos = "sample", why, []
    if client:
        try:
            photos = client.search(query, count, orientation)
            mode, note = "live", ""
            if not photos:
                out(f"no usable Unsplash results for \"{query}\"; try --query with a broader word. Handoff unchanged.",
                    file=sys.stderr)
                return 1
        except UnsplashError as exc:
            if require_live:
                out(f"error: {exc}", file=sys.stderr)
                return 2
            why = f"live search unavailable: {clean(exc, 100)}"
            out(why, file=sys.stderr)
            mode, note = "sample", why
    if mode == "sample":
        photos = load_sample()
        out(f"using SAMPLE results ({note}).", file=sys.stderr)
    state = {"slug": slug, "query": query, "mode": mode, "note": note, "saved_at": iso_z(utc_now()),
             "photos": photos[:count]}
    save_state(data_dir, slug, state)
    write_handoff(target, state)
    out(str(target))
    return 0


def choose(slug: str, ref: str, *, data_dir: Path = DATA_DIR, handoff_dir: Path = HANDOFF_DIR, limiter: Limiter | None = None,
           env=None, env_path: Path | None = None, session=None, out=print) -> int:
    state = load_state(data_dir, slug)
    photos = state["photos"]
    m = re.fullmatch(r"[sS](\d+)", ref.strip())
    photo = next((p for i, p in enumerate(photos, 1) if (m and int(m.group(1)) == i) or p["id"] == ref.strip()), None)
    if photo is None:
        out(f"error: {ref!r} is not one of the stored suggestions (S1..S{len(photos)} or a photo id)", file=sys.stderr)
        return 2
    if not photo["chosen"]:
        if state["mode"] == "live":
            client, why = make_client(limiter or Limiter(data_dir, load_policy()), env, env_path, session)
            if client is None:
                out(f"error: cannot record the pick: {why}", file=sys.stderr)
                return 2
            try:
                client.track_download(photo["download_location"], photo["id"])
            except UnsplashError as exc:
                out(f"error: download tracking failed, pick not recorded: {exc}", file=sys.stderr)
                return 2
        photo["chosen"] = True
        photo["chosen_at"] = iso_z(utc_now())
        save_state(data_dir, slug, state)
        write_handoff(Path(handoff_dir) / f"{slug}.md", state)
    out(f"{photo['id']} confirmed" + (" (sample: no tracking call made)" if state["mode"] == "sample" else ""))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Unsplash reference-photo suggestions for the handoff")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--handoff-dir", type=Path, default=HANDOFF_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suggest", help="search Unsplash and write 3-5 suggestions into handoff/<slug>.md")
    s.add_argument("slug")
    s.add_argument("--query", help="override the category derived from the project title")
    s.add_argument("--count", type=int, default=4, help=f"{MIN_COUNT}-{MAX_COUNT} photos (default 4)")
    s.add_argument("--orientation", choices=["landscape", "portrait", "squarish"])
    s.add_argument("--sample", action="store_true", help="use the labelled SAMPLE result, no call")
    s.add_argument("--require-live", action="store_true", help="fail instead of falling back to the SAMPLE result")
    s.add_argument("--dry-run", action="store_true", help="show the plan, call nothing, write nothing")
    s.add_argument("--projects-dir", type=Path, default=projects.PROJECTS_DIR)
    c = sub.add_parser("choose", help="record a human pick: calls Unsplash download tracking, marks the line confirmed")
    c.add_argument("slug")
    c.add_argument("ref", help="S1, S2, ... or the photo id")
    sub.add_parser("status", help="key fingerprint, cooldown and call counts")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "suggest":
            return suggest(a.slug, query=a.query, count=a.count, sample=a.sample, dry_run=a.dry_run,
                           require_live=a.require_live, orientation=a.orientation, data_dir=a.data_dir,
                           handoff_dir=a.handoff_dir, projects_dir=a.projects_dir)
        if a.cmd == "choose":
            return choose(a.slug, a.ref, data_dir=a.data_dir, handoff_dir=a.handoff_dir)
        print(unsplash_store.describe())
        print(json.dumps(Limiter(a.data_dir, load_policy()).status(), indent=2))
        return 0
    except (UnsplashError, UnsplashKeyError, projects.ProjectError) as exc:
        print(f"error: {scrub_tokens(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
