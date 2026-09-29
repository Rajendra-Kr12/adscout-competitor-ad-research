"""Offline stand-in for ads_archive, driven by a SYNTHETIC fixture file.

It mimics the parts of the real API the pipeline depends on: search_page_ids /
search_terms filtering, ad_reached_countries with EU/UK-only coverage for commercial
ads, delivery-date windows, media_type filtering, limit + paging.next, and
ad_snapshot_url values that embed the access token (so scrubbing can be tested).
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from common import EU_UK, parse_ts


class FixtureTransport:
    def __init__(self, path: Path):
        doc = json.loads(Path(path).read_text())
        self.ads = doc["ads"]
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None) -> dict:
        if params is None:  # a paging.next URL: every parameter is in the query string
            params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.calls.append(dict(params))
        token = params.get("access_token", "")
        matches = [a for a in self.ads if self._match(a, params)]
        offset = int(params.get("after", 0))
        limit = int(params.get("limit", 25))
        page = matches[offset:offset + limit]
        body = {"data": [self._render(a, token, params.get("fields")) for a in page]}
        if offset + limit < len(matches):
            nxt = dict(params, after=str(offset + limit))
            body["paging"] = {"cursors": {"after": str(offset + limit)},
                              "next": "https://graph.facebook.com/fixture/ads_archive?" + urlencode(nxt)}
        return body

    @staticmethod
    def _match(ad: dict, p: dict) -> bool:
        meta = ad.get("_fixture", {})
        if "search_page_ids" in p and str(ad.get("page_id")) not in json.loads(p["search_page_ids"]):
            return False
        if "search_terms" in p:
            hay = " ".join([ad.get("page_name", "")] + ad.get("ad_creative_bodies", [])).lower()
            if p["search_terms"].strip("'\"").lower() not in hay:
                return False
        wanted = set(json.loads(p.get("ad_reached_countries", "[]")))
        reached = set(meta.get("reached_countries", []))
        if "ALL" not in wanted and not wanted & reached:
            return False
        if not reached & EU_UK:  # commercial ads outside the EU/UK are not in the API
            return False
        if p.get("media_type") and p["media_type"] != meta.get("media_type"):
            return False
        start = parse_ts(ad.get("ad_delivery_start_time"))
        stop = parse_ts(ad.get("ad_delivery_stop_time"))
        dmax = parse_ts(p.get("ad_delivery_date_max"))
        dmin = parse_ts(p.get("ad_delivery_date_min"))
        if dmax and start and start.date() > dmax.date():
            return False
        if dmin and stop and stop.date() < dmin.date():
            return False
        return True

    @staticmethod
    def _render(ad: dict, token: str, fields: str | None) -> dict:
        out = {k: v for k, v in ad.items() if not k.startswith("_")}
        out["ad_snapshot_url"] = (f"https://www.facebook.com/ads/archive/render_ad/"
                                  f"?id={ad['id']}&access_token={token}")
        if fields:
            keep = set(fields.split(","))
            out = {k: v for k, v in out.items() if k in keep}
        return out
