# Compliance

The things this project always does, never does, and why. The full analysis and sources are in `docs/research/fbcm-research-r1.md` §5.

## Data sources

| Allowed | Not allowed |
|---|---|
| Meta Ad Library API `GET /ads_archive`, called with the captain's own user token | Scraping facebook.com or the Ad Library website (HTTP scrapers, Apify actors, "no token" Ad Library MCP servers) |
| Meta-hosted Ads MCP server `https://mcp.facebook.com/ads`, tool `ads_library_search` only (spike done; not connected yet), then own-account work in Phase 3 | Driving facebook.com with a browser agent (Claude in Chrome, computer use, Playwright, chrome-devtools) |
| A human looking at the Ad Library website | Third-party "Meta Ads MCP" servers that hold our token |
| Per-ad creative download **for analysis** (Phase 1, not built) | Bulk mirroring of a competitor's creatives |

Meta Ads MCP rules, for when the route is connected (`scripts/mcp_source.py` is a stub until then):

- Only `ads_library_search`. The login grants write scopes and the server exposes about 44 write tools,
  so block write actions in Business Suite > Settings > Integrations > Ads MCP server and deny every other
  `mcp__meta-ads__*` tool.
- Its own description says "Do NOT use for bulk data extraction/scraping of the Ad Library": a few calls
  per brand per day, by `page_ids`, through `scripts/guard.py`.
- Its `advertiser_request` field sends the user's request to Meta verbatim, so it carries brand names and
  countries only, never personal data.
- Outputs label its records `headline-only` and never present them as full ad detail.

Basis: Meta Terms §3.2 ("You may not access or collect data from our Products using automated means... without our prior permission"). The Ad Library API page allows downloading creative "for an individual ad... for analysis".

## Tokens and secrets

- The token lives in one store: `.env` (git-ignored, must be 0600, the default) or the macOS Keychain (`fbcm.meta` / `user_token`). Only `scripts/token_store.py` reads it; everything else sees its fingerprint (sha256[:8]). `.env.example` holds empty placeholders.
- The App Secret is typed into `token_cli.py rotate` and never stored. `client_secret`, `fb_exchange_token` and `input_token` are scrubbed from error messages like `access_token`.
- One Mac only. Do not reuse the long-lived token on another machine or in a cloud routine (Meta's long-lived token guide: "Do not use the same long-lived tokens on more than one web client").
- The API returns `ad_snapshot_url` with the access token embedded. `fetch_ads.py` strips the `access_token` parameter **before** writing `data/raw/` and refuses to write any line that still contains it. Raw files are created 0600. The snapshot URL itself is kept **only** in `data/raw/`. It is never written to `data/ads/`, `data/llm/`, reports or chat output. `scripts/common.py:write_json` and `build_report.py` refuse to write any text containing `access_token`, and `tests/test_pipeline.py` asserts this.
- Error messages are token-scrubbed before they are printed.
- Reports cite ads only by the public permalink `https://www.facebook.com/ads/library/?id=<library_id>`.
- Project files (`config/projects/*.yaml`) hold brand names, public Page IDs and preferences only, never ad
  data, so they may be committed. A project's `output_root` must be `reports/` or an absolute path outside the
  repo, because only `reports/` is git-ignored; the same privacy rules apply wherever it points.
- A project run's `workbook.xlsx` and `csv/` carry full ad text too: they are Platform Data like the reports,
  kept under the same rules, and never embed competitor images (`export_xlsx.py` also refuses any cell
  containing `access_token`). Share them only privately; the Google Drive copy is not built yet.
- `reports/<target>/<date>-ads.csv` carries each ad's full text, so it is Platform Data like the report: git-ignored, private, and written only through the same `access_token` check.

## Agent guardrails

The rules above are also enforced on Claude Code tool calls, not only written down:

- `.claude/settings.json` holds permission deny rules. It denies reading `.env` and `data/raw/`, WebFetch to facebook.com and fbcdn.net, Claude in Chrome, and `lavish-axi share`.
- `.claude/hooks/guard_pretool.py` is a PreToolUse hook. It catches what the deny rules can't:
  - network commands and browser MCPs aimed at facebook.com or fbcdn.net (`graph.` and `developers.facebook.com` are allowed);
  - `access_token=` in any Bash, WebFetch or MCP input, and Keychain token reads;
  - `.env` reads by other programs;
  - third-party Meta Ads MCP installs.
- `.githooks/pre-commit` refuses secrets, Platform Data and token-shaped strings at commit time. It prints file and line only, never the match.

## Token leak runbook

If the token may have leaked (pasted in chat, committed, shared, or found by `token_cli.py scan`):

1. `.venv/bin/python scripts/guard.py kill "suspected leak"`. Every run stops.
2. Revoke it: remove the FBCM app under Facebook Settings > Apps and websites (a human does this on facebook.com). Its user tokens stop working (190/458).
3. Mint a new Explorer token and run `.venv/bin/python scripts/token_cli.py rotate` in your own Terminal.
4. Find copies: `.venv/bin/python scripts/token_cli.py scan . ~/.claude/projects logs` lists files holding the token or a token-shaped string, by path only. Clean them.
5. If Platform Data was exposed, report it through the Platform Terms §6.b incident process.
6. `.venv/bin/python scripts/guard.py unkill` (interactive confirmation).

Losing the token (rather than leaking it) is low impact: mint a new one and rotate.

## Request volume (Graph API rate limiting; Platform Terms §7.e)

Meta: "When the limit has been reached, stop making API calls. Continuing to make calls will continue to increase your call count." `scripts/guard.py` enforces this for every call:

- Throttle codes are never retried. The run stops and a cooldown of at least 60 minutes (or Meta's `estimated_time_to_regain_access`) blocks new runs.
- The caps in `config/guard.yaml` (per run, per hour, per day) and call spacing apply across processes, and only one run can hold the lock.
- Codes that put the account or app at risk (190, 368, 459, 464, 10/200-299, 2332002/2332004, 613/1996) trip a kill switch that only a human can clear, at an interactive terminal.
- Every run leaves a ledger in `data/runs/`.

## Retention (Platform Terms §3.d)

| Data | Location | Keep |
|---|---|---|
| Raw API pulls (token stripped, 0600) | `data/raw/` | 180 days |
| LLM batch inputs and outputs | `data/llm/` | 180 days |
| Creative media (Phase 1) | `data/media/` | 90 days |
| Normalized ads | `data/ads/` | While the analysis is in use. Re-derivable from raw |
| Reports (analysis, including `-ads.csv`) | `reports/` | Kept |

Run `.venv/bin/python scripts/purge_expired.py` for a dry run, then add `--apply` to delete. Also delete data when Meta asks, or when the project stops.

## Use of results

- Publishing analysis, aggregates and short quotes is allowed. The Ad Library FAQ allows publishing research.
- Selling or licensing the ad data, or data derived from it, is **not** allowed (Platform Terms §3.a.iv).
- Sending ad data to Claude is allowed under a Service Provider arrangement (Platform Terms §5.a). Prefer API or commercial terms, and don't enable model-training settings (research D4).
- Competitor spend, CPM, CPA and ROAS don't exist in any official source. Never estimate or present them as data.
