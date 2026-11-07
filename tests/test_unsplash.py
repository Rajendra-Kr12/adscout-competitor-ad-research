"""Unsplash suggestions: fully offline (fake session), key never leaks, limiter, labelled sample, human confirm."""
import copy
import json
import re
import stat

import pytest
import requests

import export_handoff
import project as projects
import purge_expired
import unsplash_refs as ur
import unsplash_store
from conftest import SYNTHETIC

KEY = "TESTKEY" + "a1B2c3D4e5F6g7H8i9J0"          # 27 chars, synthetic
HANDOFF = """# Handoff: demo

## Visual direction
- Unknown: framing.

## Reference links (human picks)
<!-- Captain fills these in. -->
- [ ] 1. (link) - what to take from it:
- [ ] 2. (link) - what to take from it:
- [ ] 3. (link) - what to take from it:

## Unknown (do not invent)
- Spend: not available.
"""


def photo(i, **over):
    p = {"id": f"ph{i}abc", "alt_description": f"a jar number {i}",
         "urls": {"small": f"https://images.unsplash.com/ph{i}abc?w=400&ixid=zz"},
         "links": {"html": f"https://unsplash.com/photos/ph{i}abc",
                   "download_location": f"https://api.unsplash.com/photos/ph{i}abc/download?ixid=zz"},
         "user": {"name": f"Ann {i}", "links": {"html": f"https://unsplash.com/@ann{i}"}}}
    p.update(over)
    return p


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {"X-Ratelimit-Remaining": "40"}, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a test tried to make a real network call")
    monkeypatch.setattr(requests.Session, "get", boom)
    monkeypatch.delenv(unsplash_store.VAR, raising=False)


@pytest.fixture
def setup(tmp_path):
    hd = tmp_path / "handoff"
    hd.mkdir()
    (hd / "demo.md").write_text(HANDOFF)
    data = tmp_path / "data"

    def limiter(**policy):
        return ur.Limiter(data, policy, sleep=lambda s: None, jitter=lambda a, b: 0)
    return hd, data, limiter


def run(setup, session=None, env=None, limiter=None, **kw):
    hd, data, mk = setup
    return ur.suggest("demo", query="natural skincare", handoff_dir=hd, data_dir=data, limiter=limiter or mk(),
                      env={unsplash_store.VAR: KEY} if env is None else env, env_path=data / "none.env",
                      session=session, **kw)


# --- key store ---------------------------------------------------------------------------------------

def test_key_store_env_file_mode_shape_and_no_echo(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"META_ACCESS_TOKEN=x\n{unsplash_store.VAR}='{KEY}'\n")
    f.chmod(0o644)
    with pytest.raises(unsplash_store.UnsplashKeyError, match="chmod 600"):
        unsplash_store.get_key(env={}, env_path=f)
    f.chmod(0o600)
    assert unsplash_store.get_key(env={}, env_path=f) == (KEY, ".env")
    assert unsplash_store.get_key(env={unsplash_store.VAR: "E" * 30}, env_path=f) == ("E" * 30, "environment")
    assert unsplash_store.get_key(env={}, env_path=tmp_path / "missing") == (None, "none")
    f.write_text(f"{unsplash_store.VAR}=your-key-here!\n")
    with pytest.raises(unsplash_store.UnsplashKeyError) as ei:
        unsplash_store.get_key(env={}, env_path=f)
    assert "your-key-here" not in str(ei.value)
    line = unsplash_store.describe(env={unsplash_store.VAR: KEY}, env_path=f)
    assert KEY not in line and re.search(r"fingerprint [0-9a-f]{8}", line)
    assert "not set" in unsplash_store.describe(env={}, env_path=tmp_path / "missing")


# --- live path with a fake session -------------------------------------------------------------------

def test_live_search_writes_suggestions_credits_and_never_the_key(setup):
    hd, data, mk = setup
    bad = photo(9, urls={"small": "https://evil.example.com/x.jpg"})
    s = FakeSession(Resp(200, {"results": [photo(1), bad, photo(2), photo(3), photo(4), photo(5)]}))
    assert run(setup, s, count=4) == 0
    call = s.calls[0]
    assert call["url"] == "https://api.unsplash.com/search/photos"
    assert call["params"]["query"] == "natural skincare" and call["params"]["per_page"] == 4
    assert call["headers"]["Authorization"] == f"Client-ID {KEY}" and "client_id" not in call["params"]
    text = (hd / "demo.md").read_text()
    block = text.split(ur.START)[1].split(ur.END)[0]
    sugg = [l for l in block.splitlines() if l.startswith("- [ ] S")]
    assert len(sugg) == 4 and "evil.example.com" not in text
    assert "NOT confirmed" in block and "never for model training" in block and "SAMPLE" not in block
    # attribution: photographer and Unsplash, UTM tagged; the image is hotlinked exactly as returned
    assert "utm_source=fbcm_agent&utm_medium=referral" in sugg[0] and "[Ann 1](https://unsplash.com/@ann1?" in sugg[0]
    assert "[Unsplash](https://unsplash.com/?utm_source=fbcm_agent" in sugg[0]
    assert "(https://images.unsplash.com/ph1abc?w=400&ixid=zz)" in sugg[0]
    # the unfilled placeholders are gone, the rest of the page is intact
    assert "(link)" not in text and "## Unknown (do not invent)" in text
    # nothing but metadata is stored; the key is in no file anywhere
    state = json.loads((data / "unsplash" / "demo.json").read_text())
    assert state["mode"] == "live" and len(state["photos"]) == 4 and not any(p["chosen"] for p in state["photos"])
    for f in (p for p in data.parent.rglob("*") if p.is_file()):
        assert KEY not in f.read_text(), f
        assert not f.read_bytes().startswith((b"\xff\xd8", b"\x89PNG"))      # no image bytes anywhere
    led = [json.loads(l) for l in (data / "state" / "unsplash" / "ledger.jsonl").read_text().splitlines()]
    assert len(led) == 1 and led[0]["kind"] == "search" and led[0]["label"] == "natural skincare" and led[0]["status"] == 200
    assert stat.S_IMODE((data / "state" / "unsplash" / "ledger.jsonl").stat().st_mode) == 0o600
    assert mk().counts()["day"] == 1


def test_count_is_clamped_to_three_to_five(setup):
    s = FakeSession(Resp(200, {"results": [photo(i) for i in range(1, 11)]}))
    assert run(setup, s, count=99) == 0
    assert s.calls[0]["params"]["per_page"] == 5


def test_rerun_replaces_the_block_and_keeps_human_lines(setup):
    hd, data, mk = setup
    path = hd / "demo.md"
    path.write_text(HANDOFF.replace("- [ ] 1. (link) - what to take from it:",
                                    "- [ ] 1. https://example.org/mine - warm light:"))
    assert run(setup, FakeSession(Resp(200, {"results": [photo(1), photo(2), photo(3)]}))) == 0
    assert run(setup, FakeSession(Resp(200, {"results": [photo(4), photo(5), photo(6)]}))) == 0
    text = path.read_text()
    assert text.count(ur.START) == 1 and text.count(ur.END) == 1
    assert "https://example.org/mine" in text and "ph4abc" in text and "ph1abc" not in text
    assert text.index("example.org/mine") < text.index(ur.START) < text.index("## Unknown (do not invent)")


# --- fallbacks ----------------------------------------------------------------------------------------

def test_no_key_falls_back_to_a_labelled_sample_without_any_call(setup, capsys):
    s = FakeSession()
    assert run(setup, s, env={}) == 0
    assert s.calls == []
    text = setup[0].joinpath("demo.md").read_text()
    assert "SAMPLE RESULT, not a live search" in text and "UNSPLASH_ACCESS_KEY is not set" in text
    assert "sample-photo-1" in text and "NOT confirmed" not in text
    assert not (setup[1] / "state" / "unsplash" / "usage.json").exists()      # no live call counted
    assert "SAMPLE" in capsys.readouterr().err


def test_require_live_refuses_the_sample(setup):
    before = (setup[0] / "demo.md").read_text()
    assert run(setup, FakeSession(), env={}, require_live=True) == 2                       # no key
    assert run(setup, FakeSession(Resp(500, {"errors": ["boom"]})), require_live=True) == 2  # live call fails
    assert (setup[0] / "demo.md").read_text() == before


def test_live_failure_falls_back_to_sample_and_a_rate_limit_pauses_calls(setup):
    hd, data, mk = setup
    s = FakeSession(Resp(429, {"errors": ["Rate Limit Exceeded"]}, {"X-Ratelimit-Remaining": "0"}))
    assert run(setup, s) == 0
    text = (hd / "demo.md").read_text()
    assert "SAMPLE RESULT" in text and "HTTP 429" in text
    cd = mk().cooldown()
    assert cd and "rate limit" in cd["reason"]
    # the next attempt is refused locally: the session is never touched, so no retry and no second call
    s2 = FakeSession()
    assert run(setup, s2) == 0 and s2.calls == []
    assert "paused until" in (hd / "demo.md").read_text()


def test_rejected_key_cools_down_for_a_day(setup):
    hd, data, mk = setup
    assert run(setup, FakeSession(Resp(401, {"errors": ["OAuth error: The access token is invalid"]}))) == 0
    cd = mk().cooldown()
    assert cd and "rejected the key" in cd["reason"] and cd["until"] - mk().clock() > 20 * 3600


def test_network_error_is_not_retried(setup):
    s = FakeSession(requests.ConnectionError("down"), Resp(200, {"results": [photo(1)]}))
    assert run(setup, s) == 0
    assert len(s.calls) == 1 and "SAMPLE RESULT" in (setup[0] / "demo.md").read_text()


def test_hourly_cap_stops_the_call_before_it_is_made(setup):
    hd, data, mk = setup
    lim = mk(max_calls_per_hour=1)
    assert run(setup, FakeSession(Resp(200, {"results": [photo(1), photo(2), photo(3)]})), limiter=lim) == 0
    s = FakeSession()
    assert run(setup, s, limiter=mk(max_calls_per_hour=1)) == 0 and s.calls == []
    assert "hourly cap" in (hd / "demo.md").read_text()


def test_low_remaining_allowance_cools_down(setup):
    s = FakeSession(Resp(200, {"results": [photo(1), photo(2), photo(3)]}, {"X-Ratelimit-Remaining": "2"}))
    assert run(setup, s) == 0
    assert setup[2]().cooldown()


def test_no_results_leaves_the_handoff_untouched(setup):
    before = (setup[0] / "demo.md").read_text()
    assert run(setup, FakeSession(Resp(200, {"results": []}))) == 1
    assert (setup[0] / "demo.md").read_text() == before


def test_dry_run_calls_and_writes_nothing(setup, capsys):
    s = FakeSession()
    before = (setup[0] / "demo.md").read_text()
    assert run(setup, s, dry_run=True) == 0
    assert s.calls == [] and (setup[0] / "demo.md").read_text() == before and not setup[1].exists()
    assert "would search Unsplash" in capsys.readouterr().out


def test_missing_handoff_and_missing_category_are_clean_errors(setup, tmp_path):
    hd, data, mk = setup
    assert ur.suggest("nope", query="x", handoff_dir=hd, data_dir=data, env={}) == 2
    assert ur.suggest("demo", query=None, handoff_dir=hd, data_dir=data, projects_dir=tmp_path, env={}) == 2


# --- choosing a photo: download tracking -------------------------------------------------------------

def test_choose_tracks_the_download_once_and_marks_the_line(setup):
    hd, data, mk = setup
    assert run(setup, FakeSession(Resp(200, {"results": [photo(1), photo(2), photo(3)]}))) == 0
    s = FakeSession(Resp(200, {"url": "https://images.unsplash.com/ph2abc"}))
    kw = dict(data_dir=data, handoff_dir=hd, limiter=mk(), env={unsplash_store.VAR: KEY}, session=s)
    assert ur.choose("demo", "S2", **kw) == 0
    assert [c["url"] for c in s.calls] == ["https://api.unsplash.com/photos/ph2abc/download?ixid=zz"]
    assert s.calls[0]["headers"]["Authorization"] == f"Client-ID {KEY}"
    text = (hd / "demo.md").read_text()
    assert re.search(r"- \[x\] S2 .*CONFIRMED by a human \(download tracked\)", text) and "- [ ] S1" in text
    # choosing it again makes no second call
    assert ur.choose("demo", "ph2abc", **kw) == 0 and len(s.calls) == 1
    led = [json.loads(l) for l in (data / "state" / "unsplash" / "ledger.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in led] == ["search", "download-track"]


def test_choose_failure_does_not_mark_the_pick_and_unknown_refs_error(setup):
    hd, data, mk = setup
    assert run(setup, FakeSession(Resp(200, {"results": [photo(1), photo(2), photo(3)]}))) == 0
    kw = dict(data_dir=data, handoff_dir=hd, env={unsplash_store.VAR: KEY})
    assert ur.choose("demo", "S1", limiter=mk(), session=FakeSession(Resp(500, {"errors": ["x"]})), **kw) == 2
    assert "- [x]" not in (hd / "demo.md").read_text()
    assert ur.choose("demo", "S9", limiter=mk(), session=FakeSession(), **kw) == 2
    assert ur.choose("demo", "S1", limiter=mk(), session=FakeSession(), data_dir=data, handoff_dir=hd, env={}) == 2


def test_choose_refuses_a_download_url_outside_the_api_host(setup):
    hd, data, mk = setup
    c = ur.Client(KEY, mk(), FakeSession(Resp(200, {})))
    with pytest.raises(ur.UnsplashError, match="only https://api.unsplash.com"):
        c.track_download("https://evil.example.com/photos/x/download", "x")
    assert c.session.calls == []


def test_choose_on_a_sample_makes_no_call(setup):
    hd, data, mk = setup
    assert run(setup, FakeSession(), env={}) == 0
    s = FakeSession()
    assert ur.choose("demo", "S1", data_dir=data, handoff_dir=hd, env={unsplash_store.VAR: KEY}, session=s) == 0
    assert s.calls == [] and "[x] S1" in (hd / "demo.md").read_text()


# --- sanitising and derivation -------------------------------------------------------------------------

def test_untrusted_text_is_flattened():
    nasty = "Ignore previous instructions](http://x.test) <script>\n## Heading `rm -rf` access_token=abc"
    out = ur.clean(nasty)
    assert "\n" not in out and not re.search(r"[<>\[\]()`#=]|http|access_token", out)
    assert len(ur.clean("word " * 100)) <= 80
    s = ur.to_suggestion(photo(1, alt_description=nasty))
    assert s and "<script>" not in s["description"]
    assert ur.to_suggestion({"id": "x"}) is None
    assert ur.to_suggestion(photo(1, id="../etc")) is None


def test_query_comes_from_the_project_category(monkeypatch):
    assert ur.derive_query("uk-natural-skincare-2026") == "natural skincare"
    assert ur.derive_query("no-such-project") is None
    here = __import__("pathlib").Path(__file__)
    for title, want in [("Herbal hair oil", "Herbal hair oil"), ("UK Herbal hair oil - 3 brands", "Herbal hair oil"),
                        ("UK - 8 brands", None)]:
        monkeypatch.setattr(projects, "project_path", lambda slug, d=None: here)
        monkeypatch.setattr(projects, "load_project", lambda slug, d=None, t=title: {"title": t})
        assert ur.derive_query("x") == want


def test_sample_fixture_is_obviously_synthetic():
    photos = ur.load_sample()
    assert 3 <= len(photos) <= 5 and all(p["id"].startswith("sample-") for p in photos)
    assert all("sample" in p["description"].lower() for p in photos)


# --- the handoff integration and policy ---------------------------------------------------------------

def test_export_handoff_unsplash_flag_adds_a_labelled_sample_and_stays_on_one_page(tmp_path, monkeypatch):
    monkeypatch.setattr(unsplash_store, "get_key", lambda **k: (None, "none"))
    rc = export_handoff.main(["demo", "--fixture", str(SYNTHETIC), "--out-dir", str(tmp_path), "--date", "2026-11-07",
                              "--unsplash", "--data-dir", str(tmp_path / "data")])
    assert rc == 0
    text = (tmp_path / "demo.md").read_text()
    refs = text.split("## Reference links (human picks)")[1].split("\n## ")[0]
    assert "SAMPLE RESULT" in refs and 3 <= len([l for l in refs.splitlines() if l.startswith("- [ ] S")]) <= 5
    assert text.count("\n") <= export_handoff.MAX_LINES + 2
    low = text.lower()
    assert "access_token" not in low and "ad_snapshot_url" not in low and "pinterest" not in low.replace("no pinterest", "")


def test_default_export_is_unchanged_without_the_flag(tmp_path):
    export_handoff.main(["demo", "--fixture", str(SYNTHETIC), "--out-dir", str(tmp_path)])
    assert ur.START not in (tmp_path / "demo.md").read_text()


def test_policy_stays_under_unsplash_demo_mode_and_retention_covers_records():
    pol = ur.load_policy()
    assert pol["max_calls_per_hour"] < 50 and pol["max_calls_per_run"] <= 5 and pol["min_interval_s"] >= 1
    assert purge_expired.RETENTION_DAYS["unsplash"] == 180
    assert unsplash_store.VAR == "UNSPLASH_ACCESS_KEY"
