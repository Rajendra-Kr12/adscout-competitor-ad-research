"""Meta Ads MCP route (`ads_library_search`) for countries outside the EU/UK: interface only.

NOT CONNECTED in this stage. Connecting it needs the captain's own Meta OAuth login and an active
ad account, so `McpLibrarySource.search` always raises `McpNotConnected` and nothing here sends a
request. The route is planned and costed (run_project.py --dry-run) so outputs can say plainly
what a non-EU/UK country will and will not give.

What the tool returns (live spike, 2026-09-30, firstmate `fbcm-p1-mcp-spike`; docs/decisions.md):
- commercial ads for ANY country, but only these 8 fields: id, page_id (int), page_name,
  ad_creative_link_title, ad_creation_time and ad_delivery_start_time (Unix seconds),
  ad_snapshot_url (the public permalink, no token) and currency (the advertiser's billing
  currency, not spend);
- at most 50 ads per call, newest first, no pagination; outside the EU/UK effectively live ads only;
- no body text, description, caption, CTA, media, reach, platforms, languages or stop date.
Its own description says "Do NOT use for bulk data extraction/scraping of the Ad Library", so a
connected version must stay at a few calls per brand per day, use page_ids (keywords match
creative text, not advertisers), and go through scripts/guard.py like every other Meta call.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common import EU_UK, months_between, permalink
from text_metrics import SCHEMA_VERSION, compute

TOOL = "ads_library_search"
MAX_PER_CALL = 50
MCP_FIELDS = ("id", "page_id", "page_name", "ad_creative_link_title", "ad_creation_time",
              "ad_delivery_start_time", "ad_snapshot_url", "currency")
AVAILABLE = "headline, creation and start dates, Page and permalink for live ads (at most 50 newest per call)"
NOT_AVAILABLE = ("primary text, link description, caption, CTA button, images or video, reach, "
                 "platforms, languages and stop dates")
NOT_CONNECTED = "it needs the captain's own Meta login and an active ad account"


class McpNotConnected(RuntimeError):
    pass


def mcp_countries(countries) -> list[str]:
    """Countries that only the MCP route can cover (ads_archive has no commercial ads there)."""
    return [c for c in countries if c not in EU_UK]


def plan_requests(brand: dict, countries: list[str]) -> list[dict]:
    """The ads_library_search calls a connected run would make for one brand: one per brand.

    Uses page_ids when known; otherwise the brand name as search_terms, which matches creative
    text rather than the advertiser, so results need the Page confirmed before use.
    """
    if not countries:
        return []
    req = {"tool": TOOL, "countries": list(countries), "ad_active_status": "ACTIVE", "limit": MAX_PER_CALL}
    if brand.get("page_ids"):
        req["page_ids"] = list(brand["page_ids"])
    else:
        req["search_terms"] = brand.get("search_terms") or brand["name"]
    return [req]


class McpLibrarySource:
    """Source interface for the MCP route. `connected` stays False until the captain logs in."""

    name = "mcp"
    connected = False

    def search(self, request: dict) -> dict:
        raise McpNotConnected(f"the Meta Ads MCP route is not connected in this stage: {NOT_CONNECTED}")


def _iso(ts) -> str | None:
    if ts in (None, ""):
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def normalize_mcp_ad(ad: dict, *, fetched_at: str, run_id: str, active_status: str) -> dict:
    """Map one ads_library_search result to schemas/ad.schema.json (coverage mcp_library_search).

    Only the headline is text; bodies stay empty and derived.text_status is "headline-only".
    ad_snapshot_url is ignored: the permalink is rebuilt from the id. active_at_fetch is True only
    when the call asked for ACTIVE ads; the tool returns no stop date, so otherwise it is unknown.
    """
    start, created = _iso(ad.get("ad_delivery_start_time")), _iso(ad.get("ad_creation_time"))
    fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    start_dt = datetime.fromisoformat(start.replace("+0000", "+00:00")) if start else None
    title = ad.get("ad_creative_link_title")
    text = {"bodies": [], "link_titles": [title] if title else [], "link_descriptions": [], "link_captions": []}
    return {
        "schema_version": SCHEMA_VERSION,
        "library_id": str(ad["id"]),
        "page": {"id": str(ad["page_id"]) if ad.get("page_id") is not None else None,
                 "name": ad.get("page_name")},
        "permalink": permalink(ad["id"]),
        "coverage_basis": "mcp_library_search",
        "dates": {
            "created": created, "delivery_start": start, "delivery_stop": None,
            "start_month": start[:7] if start else None,
            "active_months": months_between(start_dt.date(), fetched.date()) if start_dt else [],
            "days_running": (fetched.date() - start_dt.date()).days + 1 if start_dt else None,
            "active_at_fetch": True if active_status == "ACTIVE" else None,
        },
        "platforms": [], "languages": [], "media_type_hint": None,
        "text": text,
        "eu_metrics": None, "political_metrics": None,
        "derived": compute(text, None, basis="mcp_library_search"),
        "llm": None, "creative": None,
        "provenance": {"api_version": "meta-ads-mcp", "fetched_at": fetched_at, "run_id": run_id,
                       "extractor_model": None, "prompt_version": None},
    }
