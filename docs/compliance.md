# Compliance

The things this project always does, never does, and why. The full analysis and sources are in `docs/research/fbcm-research-r1.md` §5.

## Data sources

| Allowed | Not allowed |
|---|---|
| Meta Ad Library API `GET /ads_archive`, called with the captain's own user token | Scraping facebook.com or the Ad Library website (HTTP scrapers, Apify actors, "no token" Ad Library MCP servers) |
| Meta-hosted Ads MCP server `https://mcp.facebook.com/ads` (Step-0 spike, then own-account work in Phase 3) | Driving facebook.com with a browser agent (Claude in Chrome, computer use, Playwright, chrome-devtools) |
| A human looking at the Ad Library website | Third-party "Meta Ads MCP" servers that hold our token |
| Per-ad creative download **for analysis** (Phase 1, not built) | Bulk mirroring of a competitor's creatives |

Basis: Meta Terms §3.2 ("You may not access or collect data from our Products using automated means... without our prior permission"). The Ad Library API page allows downloading creative "for an individual ad... for analysis".

## Tokens and secrets

- `META_ACCESS_TOKEN` lives only in `.env` (git-ignored). `.env.example` holds empty placeholders.
- `ad_snapshot_url` embeds the access token, so it is kept **only** in `data/raw/`. It is never written to `data/ads/`, `data/llm/`, reports or chat output. `scripts/common.py:write_json` and `build_report.py` refuse to write any text containing `access_token`, and `tests/test_pipeline.py` asserts this.
- Error messages are token-scrubbed before they are printed.
- Reports cite ads only by the public permalink `https://www.facebook.com/ads/library/?id=<library_id>`.

## Retention (Platform Terms §3.d)

| Data | Location | Keep |
|---|---|---|
| Raw API pulls (contain tokens in snapshot URLs) | `data/raw/` | 180 days |
| LLM batch inputs and outputs | `data/llm/` | 180 days |
| Creative media (Phase 1) | `data/media/` | 90 days |
| Normalized ads | `data/ads/` | While the analysis is in use. Re-derivable from raw |
| Reports (analysis) | `reports/` | Kept |

Run `.venv/bin/python scripts/purge_expired.py` for a dry run, then add `--apply` to delete. Also delete data when Meta asks, or when the project stops.

## Use of results

- Publishing analysis, aggregates and short quotes is allowed. The Ad Library FAQ allows publishing research.
- Selling or licensing the ad data, or data derived from it, is **not** allowed (Platform Terms §3.a.iv).
- Sending ad data to Claude is allowed under a Service Provider arrangement (Platform Terms §5.a). Prefer API or commercial terms, and don't enable model-training settings (research D4).
- Competitor spend, CPM, CPA and ROAS don't exist in any official source. Never estimate or present them as data.
