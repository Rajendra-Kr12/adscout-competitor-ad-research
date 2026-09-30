"""P1-02: the guard in front of every Meta call, raw-file hygiene and partial runs."""
import json
import stat
import subprocess
import sys
import time

import pytest

import fetch_ads
import guard as guard_mod
import normalize
from common import strip_token_params
from conftest import ROOT, SYNTHETIC, UK_PAGE, run_fixture_fetch
from fixture_api import FixtureTransport
from guard import Guard, GuardStop, classify_error, load_config, usage_from_headers
from test_fetch import FakeResp, FakeSession, err

ARGS = ["--page-id", UK_PAGE, "--country", "GB", "--since", "2026-01", "--until", "2026-03", "--out-key", "g"]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_guard(data_dir, clock=None, **cfg):
    config = load_config()
    config["api"].update(cfg)
    sleeps = []
    clock = clock or Clock()

    def sleep(s):
        sleeps.append(s)
        clock.t += s

    g = Guard(data_dir, config, clock=clock, sleep=sleep, log=lambda m: None, env={}, jitter=lambda a, b: 0)
    return g, sleeps


def live_run(data_dir, responses, g=None, argv=ARGS):
    g = g or make_guard(data_dir)[0]
    session = FakeSession(responses)
    t = fetch_ads.GraphTransport(session, guard=g, sleep=lambda s: None, log=lambda m: None)
    rc = fetch_ads.main([*argv, "--data-dir", str(data_dir)], transport=t, guard=g)
    return rc, session, g


def ok_page(ads=()):
    return FakeResp(200, {"data": list(ads)})


# --- throttling and the kill switch ------------------------------------------

def test_code_4_is_one_call_then_stop_with_cooldown_and_next_run_refused(tmp_path, capsys):
    rc, session, g = live_run(tmp_path, [err(4)] * 5)
    assert rc == 2 and session.calls == 1
    cd = json.loads((tmp_path / "state" / "guard" / "cooldown.json").read_text())
    assert cd["code"] == 4 and cd["until"] >= g.clock() + 59 * 60
    ledger = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())
    assert ledger["status"] == "stopped" and ledger["calls"] == 1 and ledger["errors"][0]["code"] == 4
    assert len(ledger["windows_missing"]) == 3
    # the next run is refused before any call
    rc2, session2, _ = live_run(tmp_path, [ok_page()] * 5, g=make_guard(tmp_path, Clock(g.clock() + 60))[0])
    assert rc2 == 3 and session2.calls == 0
    assert "cooling down" in capsys.readouterr().err


def test_buc_estimated_time_extends_the_cooldown(tmp_path):
    buc = json.dumps({"123": [{"type": "ads_archive", "call_count": 100, "total_cputime": 5, "total_time": 5,
                               "estimated_time_to_regain_access": 240}]})
    resp = FakeResp(400, {"error": {"code": 80000, "message": "throttled"}}, {"X-Business-Use-Case-Usage": buc})
    rc, _, g = live_run(tmp_path, [resp])
    assert rc == 2 and g.cooldown()["until"] >= g.clock() + 239 * 60


@pytest.mark.parametrize("code,subcode,kind", [
    (613, 1996, "volume"), (368, None, "policy"), (190, 463, "token"), (459, None, "account"),
    (464, None, "account"), (10, 2332002, "permission"), (200, None, "permission"), (100, 2332004, "permission"),
])
def test_stop_codes_trip_the_kill_switch(tmp_path, code, subcode, kind):
    assert classify_error(code, subcode) == kind
    rc, session, g = live_run(tmp_path, [err(code, subcode)] * 3)
    assert rc == 2 and session.calls == 1
    assert (tmp_path / "state" / "guard" / "KILL").read_text().startswith(kind)
    rc2, session2, _ = live_run(tmp_path, [ok_page()])
    assert rc2 == 3 and session2.calls == 0


def test_plain_613_is_throttle_not_kill(tmp_path):
    rc, session, g = live_run(tmp_path, [err(613)] * 2)
    assert rc == 2 and session.calls == 1 and g.kill_reason() is None and g.cooldown()


def test_kill_switch_env_and_unkill_needs_a_terminal(tmp_path, capsys):
    g = Guard(tmp_path, load_config(), env={"FBCM_KILL": "1"}, log=lambda m: None)
    with pytest.raises(GuardStop, match="FBCM_KILL"):
        g.preflight()
    g2, _ = make_guard(tmp_path)
    g2.kill("suspected leak")
    with pytest.raises(GuardStop, match="suspected leak"):
        g2.preflight()

    class Pipe:
        def isatty(self):
            return False

        def readline(self):
            return "unkill\n"

    assert guard_mod.main(["--data-dir", str(tmp_path), "unkill"], stdin=Pipe()) == 2
    assert (tmp_path / "state" / "guard" / "KILL").exists()
    assert "interactive terminal" in capsys.readouterr().err


def test_fixture_runs_respect_the_kill_switch(tmp_path):
    make_guard(tmp_path)[0].kill("test")
    rc = fetch_ads.main([*ARGS, "--fixture", str(SYNTHETIC), "--data-dir", str(tmp_path)])
    assert rc == 3 and not (tmp_path / "raw").exists()


# --- caps, spacing, breaker ----------------------------------------------------

def test_hourly_cap_is_shared_across_processes_and_spacing_applies(tmp_path):
    clock = Clock()
    g1, sleeps = make_guard(tmp_path, clock, max_calls_per_hour=3, min_interval_s=2)
    for _ in range(3):
        g1.before_call()
    assert sleeps == [2, 2]  # spaced after the first call
    g2, _ = make_guard(tmp_path, clock, max_calls_per_hour=3)  # a new process, same state
    with pytest.raises(GuardStop, match="hourly cap"):
        g2.before_call()
    clock.t += 3601
    g2.before_call()


def test_daily_and_run_caps(tmp_path):
    clock = Clock()
    g, _ = make_guard(tmp_path, clock, max_calls_per_day=2, max_calls_per_hour=100)
    g.before_call()
    clock.t += 4000
    g.before_call()
    clock.t += 4000
    with pytest.raises(GuardStop, match="daily cap"):
        g.before_call()
    g2, _ = make_guard(tmp_path / "other", max_calls_per_run=1)
    g2.before_call()
    with pytest.raises(GuardStop, match="run cap"):
        g2.before_call()


def test_circuit_breaker_opens_after_consecutive_failures(tmp_path):
    g, _ = make_guard(tmp_path, breaker_failures=5)
    t = fetch_ads.GraphTransport(FakeSession([FakeResp(503, {})] * 10), guard=g, sleep=lambda s: None,
                                 log=lambda m: None)
    with pytest.raises(fetch_ads.MetaApiError):
        t.get("https://x", {})  # 4 attempts (1 + 3 transient retries)
    assert g.cooldown() is None
    with pytest.raises(GuardStop, match="circuit breaker"):
        t.get("https://x", {})  # 5th consecutive failure opens the circuit, so the retry is refused
    assert "circuit breaker" in g.cooldown()["reason"]
    assert t.session.calls == 5


def test_usage_header_parsing():
    peaks, eta = usage_from_headers({
        "X-App-Usage": '{"call_count": 12, "total_cputime": 40, "total_time": 3}',
        "x-business-use-case-usage": '{"1": [{"call_count": 5, "total_time": 91, "estimated_time_to_regain_access": 7}]}',
        "X-Ad-Account-Usage": '{"acc_id_util_pct": 9.5, "reset_time_duration": 0}',
    })
    assert peaks == {"x-app-usage": 40, "x-business-use-case-usage": 91, "x-ad-account-usage": 9.5}
    assert eta == 7
    assert usage_from_headers({"x-app-usage": "not json"}) == ({}, 0)


def test_page_cap_is_50():
    page = {"data": [{"id": "1"}]}

    class Endless:
        n = 0

        def get(self, url, params):
            Endless.n += 1
            return {**page, "paging": {"next": f"https://x/{Endless.n}"}}

    with pytest.raises(fetch_ads.MetaApiError, match="over 50 pages"):
        list(fetch_ads.iter_ads(Endless(), "https://x", {}))
    assert Endless.n == 50


# --- single run lock -------------------------------------------------------------

def test_two_concurrent_runs_one_refused(tmp_path, capsys):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, sys.argv[1]); from guard import Guard; "
         "g = Guard(sys.argv[2], env={});\nwith g.lock():\n print('locked', flush=True); time.sleep(20)",
         str(ROOT / "scripts"), str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        rc = fetch_ads.main([*ARGS, "--fixture", str(SYNTHETIC), "--data-dir", str(tmp_path)])
        assert rc == 3 and "holds the lock" in capsys.readouterr().err
    finally:
        holder.kill()
        holder.wait()
    assert fetch_ads.main([*ARGS, "--fixture", str(SYNTHETIC), "--data-dir", str(tmp_path)]) == 0


# --- raw hygiene and partial runs ------------------------------------------------

def test_no_token_anywhere_under_data_and_private_modes(tmp_path):
    key, raw = run_fixture_fetch(tmp_path, UK_PAGE, "GB")
    normalize.normalize_run(raw, tmp_path)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert any(p.parent.name == "runs" for p in files)
    for p in files:
        text = p.read_text()
        assert "access_token" not in text.lower() and "FIXTURE-NOT-A-REAL-TOKEN" not in text, p
    assert stat.S_IMODE(raw.stat().st_mode) == 0o600
    assert stat.S_IMODE(next((tmp_path / "runs").glob("*.json")).stat().st_mode) == 0o600


def test_strip_token_params():
    assert strip_token_params("https://a/render_ad/?id=1&access_token=SECRET") == "https://a/render_ad/?id=1"
    assert strip_token_params("https://a/?access_token=S&id=1") == "https://a/?id=1"
    assert strip_token_params({"u": ["https://a/?id=1&access_token=S&x=2"]}) == {"u": ["https://a/?id=1&x=2"]}


def test_normalize_refuses_partial_runs(tmp_path, capsys):
    ad = json.loads(SYNTHETIC.read_text())["ads"][0]
    ad = {k: v for k, v in ad.items() if not k.startswith("_")}
    rc, _, _ = live_run(tmp_path, [FakeResp(200, {"data": [ad]}), err(17)])
    assert rc == 2
    raw = next((tmp_path / "raw" / "g").glob("*.jsonl"))
    assert json.loads(raw.read_text().splitlines()[-1])["type"] == "stopped"
    assert normalize.main([str(raw), "--data-dir", str(tmp_path)]) == 2
    assert "partial run" in capsys.readouterr().err
    assert not (tmp_path / "ads").exists()
    assert normalize.main([str(raw), "--data-dir", str(tmp_path), "--allow-partial"]) == 0
    assert (tmp_path / "ads" / "g" / f"{ad['id']}.json").exists()


# --- keyword search semantics --------------------------------------------------

def test_keyword_default_is_exact_phrase_and_fixture_matches_api_semantics(capsys):
    fetch_ads.main(["--search-terms", "Forest Essentials", "--country", "GB", "--since", "2026-01",
                    "--until", "2026-01", "--dry-run"])
    assert "search_type=KEYWORD_EXACT_PHRASE" in capsys.readouterr().out
    fetch_ads.main(["--search-terms", "x y", "--search-type", "KEYWORD_UNORDERED", "--country", "DE",
                    "--since", "2026-01", "--until", "2026-01", "--dry-run"])
    out = capsys.readouterr().out
    assert "search_type=KEYWORD_UNORDERED" in out and "%22DE%22" in out

    t = FixtureTransport(SYNTHETIC)

    def ids(terms, stype):
        p = fetch_ads.build_params(page_ids=None, search_terms=terms, countries=["GB"], date_min="2025-01-01",
                                   date_max="2026-12-31", media_type="ALL", limit=100, token="t", search_type=stype)
        return {a["id"] for a in t.get("https://x", p)["data"]}

    assert "990000000000101" in ids("night oil", "KEYWORD_EXACT_PHRASE")
    assert "990000000000101" not in ids("oil night", "KEYWORD_EXACT_PHRASE")
    assert "990000000000101" in ids("oil night", "KEYWORD_UNORDERED")  # words matched separately
    assert ids("oil saffron", "KEYWORD_UNORDERED") == {"990000000000101"}


def test_back_to_back_runs_get_distinct_files_and_errors_close_the_ledger(tmp_path):
    for _ in range(2):
        assert fetch_ads.main([*ARGS, "--fixture", str(SYNTHETIC), "--data-dir", str(tmp_path)]) == 0
    assert len(list((tmp_path / "raw" / "g").glob("*.jsonl"))) == 2

    class Boom:
        def get(self, url, params):
            raise KeyError("unexpected")

    g, _ = make_guard(tmp_path / "e")
    with pytest.raises(KeyError):
        fetch_ads.main([*ARGS, "--data-dir", str(tmp_path / "e")], transport=Boom(), guard=g)
    ledger = json.loads(next((tmp_path / "e" / "runs").glob("*.json")).read_text())
    assert ledger["status"] == "error" and "KeyError" in ledger["stop_reason"]
