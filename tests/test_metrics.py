from text_metrics import assign_near_duplicates, compute, jaccard, landing_domain


def text(bodies, titles=(), descs=(), caps=()):
    return {"bodies": list(bodies), "link_titles": list(titles), "link_descriptions": list(descs),
            "link_captions": list(caps)}


def test_length_and_variant_metrics():
    d = compute(text(["Hello world\n\nsecond line", "Hello world\n\nsecond line", "Other copy"]))
    assert d["body_chars"] == [24, 24, 10]
    assert d["body_words"] == [4, 4, 2]
    assert d["body_lines"] == [2, 2, 1]
    assert d["variant_count"] == 2
    assert d["first_line"] == "Hello world"


def test_offer_and_style_signals():
    d = compute(text(["Flat 25% off this weekend 🔥🔥 #sale #ayurveda", "From £45"], ["Save 10%"]))
    assert d["emoji_count"] == 2 and d["hashtag_count"] == 2
    assert d["has_price"] and d["has_percent_off"]
    plain = compute(text(["A calm brand story with 5,000 years of heritage."]))
    assert not plain["has_price"] and not plain["has_percent_off"] and plain["emoji_count"] == 0


def test_price_formats():
    for s in ["Rs. 999 only", "₹499", "only 30 GBP", "INR 1,299", "$19.99"]:
        assert compute(text([s]))["has_price"], s
    assert not compute(text(["SPF 30 gel for 14 days"]))["has_price"]


def test_landing_domain_normalizes_captions():
    assert landing_domain(["WWW.Brand.CO.UK"]) == "brand.co.uk"
    assert landing_domain(["https://shop.brand.com/path"]) == "shop.brand.com"
    assert landing_domain(["Shop now", "brand.com"]) == "brand.com"
    assert landing_domain([]) is None


def test_empty_ad():
    d = compute(text([]))
    assert d["variant_count"] == 0 and d["first_line"] is None and d["body_chars"] == []


def _ad(lid, start, body):
    return {"library_id": lid, "dates": {"delivery_start": start}, "text": {"bodies": [body]},
            "derived": {"near_duplicate_of": "stale"}}


def test_near_duplicates_point_to_earliest_copy():
    copy = "Is your skin tired of winter? Our saffron day cream restores glow in 14 days."
    ads = [_ad("3", "2026-03-03", copy + " Shop now."), _ad("1", "2026-02-01", copy),
           _ad("2", "2026-02-10", "Totally different spring sale copy about hair oils and amla.")]
    assign_near_duplicates(ads)
    by = {a["library_id"]: a["derived"]["near_duplicate_of"] for a in ads}
    assert by == {"1": None, "2": None, "3": "1"}


def test_jaccard_edges():
    assert jaccard(set(), {1}) == 0.0 and jaccard({1, 2}, {1, 2}) == 1.0
