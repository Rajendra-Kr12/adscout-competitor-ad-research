"""Keyword discovery: cluster-weighted terms, windowed lean search through the guard, Page aggregation."""
import json
from datetime import date

import pytest

import discover_keywords as dk
import discovery_pages as dp
import keyword_terms as kt
from common import load_discovery_config, week_windows
from conftest import SYNTHETIC
from fetch_ads import GraphTransport

STOP = set(load_discovery_config()["terms"]["stopwords"].split())


def ad(brand_dir, lid, first_line, *, root=None, lang="en", creative_only=False, titles=()):
    brand_dir.mkdir(parents=True, exist_ok=True)
    doc = {"library_id": lid, "languages": [lang], "text": {"link_titles": list(titles)},
           "derived": {"first_line": first_line, "near_duplicate_of": root,
                       "text_status": "creative-only" if creative_only else "has_text"}}
    (brand_dir / f"{lid}.json").write_text(json.dumps(doc))


# --- step 1: terms ---------------------------------------------------------------------

def test_terms_are_weighted_by_copy_cluster_not_ad_count(tmp_path):
    a, b = tmp_path / "ads" / "brand-a", tmp_path / "ads" / "brand-b"
    for i in range(10):  # one message re-run ten times counts once
        ad(a, f"a{i}", "Say goodbye to dark circles today", root="a0" if i else None)
    ad(a, "a99", "Aloe vera for calm skin")
    ad(a, "a98", "Aloe vera, a French ad", lang="fr")
    ad(a, "a97", "", creative_only=True)
    ad(b, "b1", "Dark circles gone in a week")
    ad(b, "b2", "Pure aloe vera gel", titles=["Aloe vera bestseller"])
    clusters = {k: kt.clusters_for(tmp_path, k, "en") for k in ("brand-a", "brand-b")}
    assert len(clusters["brand-a"]) == 2 and len(clusters["brand-b"]) == 2  # fr and creative-only skipped
    rows = {r["term"]: r for r in kt.rank_terms(clusters, STOP, min_clusters=1)}
    assert rows["dark circles"]["clusters"] == 2 and rows["dark circles"]["brands"] == 2
    assert rows["aloe vera"]["clusters"] == 2  # a headline repeat inside one ad counts once
    assert "skin" not in rows and "goodbye to" not in rows  # a stopword alone, or at either edge
    assert "goodbye to dark" in rows  # stopwords may sit in the middle
    first = kt.rank_terms(clusters, STOP, min_clusters=1)[0]
    assert first["words"] > 1 and first["brands"] == 2  # multi-word, multi-brand terms rank first


def test_keyword_terms_cli_excludes_brand_names(tmp_path, capsys):
    ad(tmp_path / "ads" / "glow-co", "g1", "Glow co serum for dark circles")
    ad(tmp_path / "ads" / "glow-co", "g2", "Glow co dark circles balm")
    assert kt.main(["--brand", "glow-co", "--data-dir", str(tmp_path), "--min-clusters", "2",
                    "--out", str(tmp_path / "terms.json")]) == 0
    terms = [r["term"] for r in json.loads((tmp_path / "terms.json").read_text())["terms"]]
    assert "dark circles" in terms and not any(t.startswith("glow") for t in terms)


# --- step 2: windows, plan, search ------------------------------------------------------

def test_week_windows_cover_the_months_and_clamp():
    w = week_windows("2026-09", "2026-09")
    assert w[0] == ("2026-09-01", "2026-09-07") and w[-1] == ("2026-09-29", "2026-09-30") and len(w) == 5
    # 7-day steps from the start of the range; the last window is clamped to Meta's day
    assert week_windows("2026-07", "2026-09", last_day=date(2026, 9, 10))[-1] == ("2026-09-09", "2026-09-10")


def test_keyword_parsing():
    assert dk.parse_keyword("dark circles") == ("dark circles", "KEYWORD_EXACT_PHRASE")
    assert dk.parse_keyword("menopause skin:unordered") == ("menopause skin", "KEYWORD_UNORDERED")


def test_a_plan_over_budget_sends_nothing(tmp_path):
    with pytest.raises(SystemExit, match="plan needs up to 28 call"):
        dk.main(["--name", "x", "--keyword", "a b", "--keyword", "c d", "--since", "2026-07", "--until", "2026-09",
                 "--window", "week", "--budget", "25", "--fixture", str(SYNTHETIC), "--data-dir", str(tmp_path)])
    assert not (tmp_path / "discovery").exists() and not (tmp_path / "runs").exists()


def test_fixture_search_is_lean_private_and_ledgered(tmp_path, capsys):
    assert dk.main(["--name", "Demo Run", "--keyword", "ayurveda", "--since", "2026-01", "--until", "2026-03",
                    "--fixture", str(SYNTHETIC), "--data-dir", str(tmp_path), "--limit", "2"]) == 0
    (out,) = (tmp_path / "discovery" / "demo-run").glob("*.jsonl")
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    lean = set(load_discovery_config()["search"]["fields"]) | {"keyword", "window"}
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert recs and all(set(r) <= lean for r in recs)
    text = out.read_text().lower()
    assert "access_token" not in text and "ad_snapshot_url" not in text and "render_ad" not in text
    summary = json.loads(out.with_suffix(".summary.json").read_text())
    assert len(summary["queries"]) == 3 and summary["calls"] <= 3 and summary["stopped"] is None
    assert any(q["capped"] for q in summary["queries"])  # limit 2 with more matches: marked as a sample
    assert list((tmp_path / "runs").glob("*.json"))  # the guard ledger


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


def page(n, nxt=True, usage=None):
    body = {"data": [{"id": f"{n}{i}", "page_id": "1", "page_name": "P"} for i in range(2)]}
    if nxt:
        body["paging"] = {"next": f"https://graph.facebook.com/next/{n}"}
    return FakeResp(200, body, {"x-app-usage": json.dumps({"call_count": usage})} if usage is not None else {})


def live_main(tmp_path, responses, *extra):
    g = dk.discovery_guard(tmp_path, clock=lambda: 1_000_000.0, jitter=lambda a, b: 0)
    g.sleep = lambda s: None  # call spacing in tests
    t = GraphTransport(FakeSession(responses), guard=g, retries=0, sleep=lambda s: None, log=lambda m: None)
    rc = dk.main(["--name", "t", "--keyword", "aloe vera", "--keyword", "fine lines", "--since", "2026-07",
                  "--until", "2026-08", "--data-dir", str(tmp_path), *extra], transport=t, guard=g)
    (summary,) = (tmp_path / "discovery" / "t").glob("*.summary.json")
    return rc, json.loads(summary.read_text()), t.session.calls


def test_stops_at_the_first_usage_warning(tmp_path, capsys):
    rc, summary, calls = live_main(tmp_path, [page(1, usage=10), page(2, usage=80), page(3)])
    assert rc == 2 and calls == 2 and "UsageWarning" in summary["stopped"]
    ledger = json.loads(sorted((tmp_path / "runs").glob("*.json"))[-1].read_text())
    assert ledger["status"] == "stopped" and max(ledger["usage_peak"].values()) == 80
    assert dk.Guard(tmp_path).cooldown() is None  # a soft warning stops discovery; it is not a cooldown


def test_call_spacing_does_not_stop_the_run(tmp_path, capsys):
    # The guard sleeps between calls; only a usage warning may stop discovery.
    rc, summary, calls = live_main(tmp_path, [page(i, usage=5) for i in range(4)])
    assert rc == 0 and calls == 4 and summary["stopped"] is None and len(summary["queries"]) == 4


def test_code_1_is_not_retried_and_stops(tmp_path, capsys):
    err = FakeResp(500, {"error": {"code": 1, "message": "Please reduce the amount of data you're asking for"}})
    rc, summary, calls = live_main(tmp_path, [err, page(2)])
    assert rc == 2 and calls == 1 and "MetaApiError" in summary["stopped"]


def test_budget_stops_mid_plan(tmp_path, capsys):
    rc, summary, calls = live_main(tmp_path, [page(i) for i in range(4)], "--budget", "4", "--pages-per-window", "1")
    assert calls == 4 and len(summary["queries"]) == 4  # 2 keywords x 2 months x 1 page fits exactly


# --- step 3: Pages ------------------------------------------------------------------------

def rec(lid, pid, name, caption, title="", kw="aloe vera", window="2026-07-01..2026-07-31", stop=None):
    return {"id": lid, "page_id": pid, "page_name": name, "ad_creative_link_captions": [caption],
            "ad_creative_link_titles": [title] if title else [], "keyword": kw, "window": window,
            "ad_delivery_start_time": "2026-07-03T10:00:00+0000", "ad_delivery_stop_time": stop}


RECORDS = [
    rec("1", "10", "Green Leaf Skincare", "greenleafskincare.co.uk", "Calm skin with aloe"),
    rec("1", "10", "Green Leaf Skincare", "greenleafskincare.co.uk", kw="fine lines"),  # same ad, 2nd keyword
    rec("2", "10", "Green Leaf Skincare", "greenleafskincare.co.uk", window="2026-08-01..2026-08-31", stop="2026-08-09"),
    rec("3", "20", "COSRX Global", "amazon.co.uk", "Snail mucin essence"),            # a brand selling via Amazon
    rec("4", "21", "Boots", "boots.com", "CeraVe hydrating cleanser, new in"),        # a retailer carrying one brand
    rec("5", "21", "Boots", "boots.com", "CeraVe moisturising cream"),
    rec("6", "22", "Cult Beauty", "cultbeauty.co.uk", "Up to 30% off skincare"),     # a multi-brand retailer
    rec("7", "30", "Michelle - Skincare Expert", "interview.lovi.care", "Why I stopped using retinol"),
    rec("8", "31", "Over 40 & Glowing", "greenleafskincare.co.uk", "Green Leaf Skincare changed my skin"),
    rec("9", "32", "Julie Celina", "julieceline.com", "My Himalaya routine"),         # a creator carrying a big brand
    rec("10", "40", "Bright Smile", "brightsmile.co.uk", "Polynucleotide booster offer"),  # injectables (headline)
    rec("11", "41", "Glowy Shop", "glowyshop.us", "Glow serum"),
    rec("12", "50", "Daisy", "novam-care.com", "50% off today only, free shipping"),
    rec("13", "51", "Rose", "rosa-care.com", "A gentle routine for you"),
    rec("14", "99", "Known Brand", "knownbrand.com"),
]


def classified(**thresholds):
    cfg = json.loads(json.dumps(load_discovery_config()["pages"]))
    cfg["thresholds"].update(thresholds)
    rows = dp.classify(dp.aggregate(RECORDS), {"99"}, cfg, ["Known Brand"])
    return {r["page_id"]: r for r in rows}, rows


def test_pages_are_aggregated_and_ranked():
    by, rows = classified()
    leaf = by["10"]
    assert leaf["ads"] == 2 and leaf["live_ads"] == 1 and leaf["keywords_matched"] == 2 and leaf["windows_matched"] == 2
    assert rows[0]["page_id"] == "10" and leaf["uk_site"]  # kept classes first, by keywords matched


@pytest.mark.parametrize("pid,cls,why", [
    ("10", "brand", "own brand"),                 # its ads (link captions) name its own brand
    ("20", "brand", "cosrx"),                      # named after a known brand: kept, even via Amazon
    ("21", "channel", "cerave"),                    # retailer whose ads name one established brand: kept as channel
    ("22", "drop:multi-brand-retail", "no single brand"),
    ("30", "drop:advertorial", "persona"),
    ("31", "drop:advertorial", "Green Leaf"),        # third-party Page pushing a brand seen only in the sample
    ("32", "channel", "himalaya"),
    ("40", "drop:clinic", "B2B"),
    ("41", "drop:off-category", "non-UK"),
    ("50", "drop:reseller-funnel", "promo"),
    ("51", "drop:unverified", "no single brand"),
    ("99", "known", "already tracked"),
])
def test_every_page_gets_a_class_and_a_logged_reason(pid, cls, why):
    by, _ = classified()
    assert by[pid]["class"] == cls and why.lower() in by[pid]["reason"].lower()


def test_thresholds_are_overridable():
    by, _ = classified(brand_share_min=1.01)  # nothing can name one brand in more than 100% of its ads
    assert by["21"]["class"] == "drop:multi-brand-retail" and by["32"]["class"] != "channel"


def test_channel_versus_brand_angle():
    _, rows = classified()
    angle = {a["brand"].lower(): a for a in dp.channel_vs_brand(rows)}
    assert angle["cerave"]["own_page"] is None and angle["cerave"]["channel_ads"] == 2
    assert angle["green leaf"]["own_page"] == "Green Leaf Skincare" and angle["green leaf"]["channel_pages"] == 1


def test_report_tables_list_every_dropped_page():
    _, rows = classified()
    result = {"rows": rows, "channel_vs_brand": dp.channel_vs_brand(rows)}
    md = dp.tables_md(result, top=25)
    dropped = [r for r in rows if r["class"].startswith("drop:")]
    assert "### Dropped and why" in md and all(f"| {r['page_id']} |" in md for r in dropped)
    assert "| channel | Boots | 21 |" in md and "### Channel versus brand messaging" in md
    assert "Already tracked (not candidates): Known Brand" in md
    assert "Julie Celina" in dp.tables_md(result, top=1)  # channels are listed even below the cut


def test_discovery_records_have_the_raw_retention():
    import purge_expired
    assert purge_expired.RETENTION_DAYS["discovery"] == purge_expired.RETENTION_DAYS["raw"] == 180
