import json

import pytest

import fetch_ads
from conftest import IN_PAGE, SYNTHETIC, UK_PAGE, run_fixture_fetch
from fixture_api import FixtureTransport


class RecordedTransport:
    """Serves recorded-shape pages in order; records what it was asked."""

    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def get(self, url, params):
        self.calls.append((url, params))
        return self.pages.pop(0)


def test_iter_ads_follows_paging_next_until_absent(fixtures_dir):
    p1 = json.loads((fixtures_dir / "ads_archive_page1.json").read_text())
    p2 = json.loads((fixtures_dir / "ads_archive_page2.json").read_text())
    t = RecordedTransport([p1, p2])
    ads = list(fetch_ads.iter_ads(t, "https://graph.facebook.com/v26.0/ads_archive", {"limit": "2"}))
    assert [a["id"] for a in ads] == ["990000000000101", "990000000000102", "990000000000103"]
    assert t.calls[0] == ("https://graph.facebook.com/v26.0/ads_archive", {"limit": "2"})
    assert t.calls[1] == (p1["paging"]["next"], None)  # next URL is used verbatim, no params re-sent


def test_iter_ads_stops_on_repeated_next_url():
    page = {"data": [{"id": "1"}], "paging": {"next": "https://x/next"}}
    with pytest.raises(fetch_ads.MetaApiError, match="paging stopped"):
        list(fetch_ads.iter_ads(RecordedTransport([page, page, page]), "https://x", {}))


def test_fixture_transport_pages_through_every_ad():
    t = FixtureTransport(SYNTHETIC)
    params = fetch_ads.build_params(page_ids=[UK_PAGE], search_terms=None, countries=["GB"],
                                    date_min="2025-01-01", date_max="2026-12-31", media_type="ALL",
                                    limit=2, token="tok")
    ads = list(fetch_ads.iter_ads(t, "https://graph.facebook.com/v26.0/ads_archive", params))
    assert len(ads) == 12 and len({a["id"] for a in ads}) == 12
    assert len(t.calls) == 6


def test_build_params_shapes():
    p = fetch_ads.build_params(page_ids=["1", 2], search_terms=None, countries=["GB"], date_min="2026-01-01",
                               date_max="2026-01-31", media_type="VIDEO", limit=500, token="t")
    assert p["search_page_ids"] == '["1", "2"]'
    assert p["ad_reached_countries"] == '["GB"]'
    assert p["ad_active_status"] == "ALL" and p["media_type"] == "VIDEO"
    assert "ad_snapshot_url" in p["fields"].split(",")
    assert "media_type" not in fetch_ads.build_params(page_ids=None, search_terms="x", countries=["GB"],
                                                      date_min="a", date_max="b", media_type="ALL",
                                                      limit=1, token=None)


# --- retry / error handling ------------------------------------------------

class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def err(code, subcode=None, status=400, msg="boom"):
    e = {"message": msg, "type": "OAuthException", "code": code}
    if subcode:
        e["error_subcode"] = subcode
    return FakeResp(status, {"error": e})


def transport(responses):
    sleeps = []
    t = fetch_ads.GraphTransport(FakeSession(responses), sleep=sleeps.append, log=lambda m: None)
    return t, sleeps


@pytest.mark.parametrize("code", [4, 17, 32, 341, 613, 80000])
def test_throttle_codes_are_never_retried(code):
    # Meta: "When the limit has been reached, stop making API calls". Phase 0 retried 5 times.
    t, sleeps = transport([err(code), FakeResp(200, {"data": []})])
    with pytest.raises(fetch_ads.MetaApiError, match=f"code={code}"):
        t.get("https://x", {})
    assert t.session.calls == 1 and sleeps == []


def test_server_errors_are_retried():
    t, sleeps = transport([FakeResp(503, {}), FakeResp(200, {"data": [1]})])
    assert t.get("https://x", {}) == {"data": [1]} and len(sleeps) == 1


def test_transient_retries_give_up_after_three():
    t, sleeps = transport([err(2)] * 6)
    with pytest.raises(fetch_ads.MetaApiError, match="code=2"):
        t.get("https://x", {})
    assert sleeps == [2, 8, 30] and t.session.calls == 4


@pytest.mark.parametrize("code,subcode,needle", [
    (2332002, None, "facebook.com/ID"),
    (10, 2332002, "facebook.com/ID"),
    (2332004, None, "USER access token"),
    (190, None, "expired"),
])
def test_access_errors_fail_fast_with_guidance(code, subcode, needle):
    t, sleeps = transport([err(code, subcode)])
    with pytest.raises(fetch_ads.MetaApiError, match=needle):
        t.get("https://x", {})
    assert sleeps == []


def test_non_retryable_error_fails_fast():
    t, sleeps = transport([err(100, msg="Invalid parameter")])
    with pytest.raises(fetch_ads.MetaApiError, match="Invalid parameter"):
        t.get("https://x", {})
    assert sleeps == []


def test_invalid_parameter_error_keeps_metas_detail():
    resp = err(100, 2334030, msg="Invalid parameter")
    resp._body["error"].update(error_user_title="Bad date", error_user_msg="see https://g/?access_token=SECRET9")
    t, _ = transport([resp])
    with pytest.raises(fetch_ads.MetaApiError) as exc:
        t.get("https://x", {})
    assert "Invalid parameter - Bad date - see" in str(exc.value) and "SECRET9" not in str(exc.value)


def test_error_messages_are_token_scrubbed():
    t, _ = transport([err(100, msg="bad url https://g/?access_token=SECRET123&x=1")])
    with pytest.raises(fetch_ads.MetaApiError) as exc:
        t.get("https://x", {})
    assert "SECRET123" not in str(exc.value)


def test_high_app_usage_is_handled_by_the_guard(tmp_path):
    # Soft limit: pause, then continue. Hard limit: cool down so the next call is refused.
    from guard import Guard, GuardStop, load_config
    sleeps = []
    g = Guard(tmp_path, load_config(), sleep=sleeps.append, log=lambda m: None, env={}, jitter=lambda a, b: 0)
    soft = FakeResp(200, {"data": []}, {"x-app-usage": '{"call_count": 80, "total_time": 10}'})
    hard = FakeResp(200, {"data": [1]}, {"x-app-usage": '{"call_count": 97, "total_time": 10}'})
    t = fetch_ads.GraphTransport(FakeSession([soft, hard, soft]), guard=g, sleep=sleeps.append, log=lambda m: None)
    assert t.get("https://x", {}) == {"data": []}
    assert 300 in sleeps
    assert t.get("https://x", {}) == {"data": [1]}  # the page that crossed 90% is kept
    with pytest.raises(GuardStop, match="cooling down"):
        t.get("https://x", {})
    assert t.session.calls == 2


# --- CLI modes -------------------------------------------------------------

def test_dry_run_sends_nothing_and_redacts(capsys):
    assert fetch_ads.main(["--search-terms", "Kapiva", "--country", "IN", "--since", "2026-01", "--until", "2026-03",
                           "--dry-run"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    assert all("access_token=REDACTED" in line and "search_terms=Kapiva" in line for line in out)


def test_competitor_with_page_id_uses_search_page_ids(capsys):
    fetch_ads.main(["--competitor", "forest-essentials-uk", "--since", "2026-01", "--until", "2026-01", "--dry-run"])
    line = capsys.readouterr().out
    assert "search_page_ids=%5B%22100331625781604%22%5D" in line and "%22GB%22" in line


def test_fixture_run_writes_raw_jsonl(tmp_path):
    key, raw = run_fixture_fetch(tmp_path, UK_PAGE, "GB")
    lines = [json.loads(l) for l in raw.read_text().splitlines()]
    assert lines[0]["type"] == "run" and lines[0]["mode"] == "fixture"
    assert "FIXTURE-NOT-A-REAL-TOKEN" not in json.dumps(lines[0])  # token never in the header
    assert lines[-1]["type"] == "summary" and lines[-1]["unique_ads"] == 12
    assert len(lines[0]["windows"]) == 9


def test_india_only_ads_are_not_returned(tmp_path):
    _, raw = run_fixture_fetch(tmp_path, IN_PAGE, "IN")
    assert json.loads(raw.read_text().splitlines()[-1])["unique_ads"] == 0
