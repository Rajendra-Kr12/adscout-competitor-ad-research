# Facebook CM Agent

A local Claude Code agent that analyses a competitor's (or any Page's) Meta ads using the
**official Ad Library API** (`ads_archive`). Phase 0 flow: Page ID or keyword plus a month
range -> every ad -> grouped by month with permalinks and text -> Claude extracts hook,
offer, CTA, tone, length and quality -> a markdown report.

It never scrapes or automates facebook.com. See `AGENTS.md` and `docs/compliance.md`.

## What the data can and cannot tell you (read first)

- **Commercial ads are returned only if they were delivered to the EU or UK** in the past year.
  Political and issue ads are covered worldwide for 7 years. An India-only brand (for example
  Kapiva) is **expected to return 0 ads**, even though the Ad Library website shows them.
  Every report states its coverage basis.
- Competitor **spend, impressions, CPM, CPA, ROAS and engagement are not available** from any
  official source. EU/UK-delivered ads carry reach and age/gender/country breakdowns.
- Phase 0 is **text only**: ad copy, headlines, link descriptions and captions. Images, video
  and on-image text come in Phase 1.
- "Days running" is a proxy for what works, not performance data.

Background: `docs/research/fbcm-research-r1.md` (sections 2, 4-7). Decisions: `docs/decisions.md`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in; .env is git-ignored
.venv/bin/python -m pytest -q
```

### Meta access (one-time, captain)

1. **Confirm your identity** at https://www.facebook.com/ID (identity and location). Approval takes a few days. Until it lands, the API answers with error **2332002**.
2. Register at https://developers.facebook.com (Get started) and accept the Platform Policy.
3. Create the app at https://developers.facebook.com/apps/creation/ as a **Consumer-type app**, for example **"FBCM Ad Library Test"**.
   - Correction to the research report: the **Business-type app could not issue user tokens** in Graph API Explorer. The Consumer-type app can.
   - Keep the app in Development mode. Do not submit it for App Review. You are the app's admin, which satisfies the "role on the app" requirement.
4. In App Settings > Basic, note the **App ID** and **App Secret**.
5. Open https://developers.facebook.com/tools/explorer, select the app, then **Generate Access Token**.
   This must be a **user** token. An app token fails with **2332004**. If you see `1357045`, turn off your ad blocker.
6. Test it in the Explorer:
   `ads_archive?search_page_ids=["100331625781604"]&ad_reached_countries=["GB"]&ad_active_status=ALL&fields=id,page_name,ad_delivery_start_time`

### Long-lived token (about 60 days)

The Explorer's token expires in about an hour. Exchange it from your Mac, never in a browser:

```bash
META_APP_ID=<your app id>
read -s SHORT_TOKEN; read -s APP_SECRET   # paste each, press Enter (not echoed, not in history)
curl -sG "https://graph.facebook.com/v26.0/oauth/access_token" \
  --data-urlencode grant_type=fb_exchange_token \
  --data-urlencode client_id="$META_APP_ID" \
  --data-urlencode client_secret="$APP_SECRET" \
  --data-urlencode fb_exchange_token="$SHORT_TOKEN"
```

Put the returned `access_token` into `.env` as `META_ACCESS_TOKEN`, along with `META_APP_ID` and
`META_API_VERSION=v26.0`. Repeat this before the token expires. When it has expired, the API returns error **190**.

### Error meanings

| Code | Meaning | Fix |
|---|---|---|
| **2332002** | Identity or Ad Library API access is not approved yet | Finish facebook.com/ID and wait |
| **2332004** | An app access token was used | Generate a **user** token in Graph API Explorer |
| **190** | Token invalid or expired | New user token, then run the long-lived exchange again |
| 4, 17, 32, 613, 80000-80014 | Rate limited | The fetcher retries with exponential backoff and pauses at 95% `X-App-Usage` |
| 100 | Invalid parameter | Check the page ID, country codes and date format |
| 0 ads for an IN/US brand | Coverage, not an error | Only EU/UK-delivered commercial ads are in the API |

## Usage

In Claude Code: `/analyze-competitor forest-essentials-uk 2025-10..2026-09`
(`.claude/skills/analyze-competitor/SKILL.md`). The skill runs the same scripts as the manual steps below:

```bash
PY=.venv/bin/python
$PY scripts/fetch_ads.py --competitor forest-essentials-uk --since 2025-10 --until 2026-09 --dry-run  # plan only
$PY scripts/fetch_ads.py --competitor forest-essentials-uk --since 2025-10 --until 2026-09
$PY scripts/normalize.py --target forest-essentials-uk
$PY scripts/llm_batches.py plan --target forest-essentials-uk        # then run ad-extractor sub-agents
$PY scripts/llm_batches.py merge --target forest-essentials-uk data/llm/forest-essentials-uk/<run>/output-*.json
$PY scripts/build_report.py --target forest-essentials-uk --since 2025-10 --until 2026-09
```

Other targets: `--page-id <id> --country GB`, or `--search-terms "Pukka Herbs" --country GB` (keyword
results can include other Pages, so pass `--only-page-id` to `normalize.py`). Add
`--media-types IMAGE,VIDEO,MEME,NONE` to label media types. This costs one query per type.
Competitors live in `config/competitors.yaml`.

### Offline demo (no token needed)

```bash
$PY scripts/fetch_ads.py --page-id 900000000000001 --country GB --since 2026-01 --until 2026-09 \
    --out-key synthetic-uk --fixture tests/fixtures/ads_archive_synthetic.json
$PY scripts/normalize.py --target synthetic-uk
$PY scripts/llm_batches.py merge --target synthetic-uk tests/fixtures/llm_output_synthetic.json  # rejects nothing
$PY scripts/build_report.py --target synthetic-uk --since 2026-01 --until 2026-09
```

The fixture is **synthetic**: made-up pages, IDs, copy and reach, in the `ads_archive` response
shape. The India-only fixture Page (`--page-id 900000000000002 --country IN`) returns 0 ads, the same way the real API does.

## Step 0 coverage spike (run once access exists; not automated)

The open question is whether Meta's hosted Ads MCP tool `ads_library_search` returns
**non-EU commercial ads** (for example Kapiva in India), which `ads_archive` cannot.

```bash
claude mcp add --transport http meta-ads https://mcp.facebook.com/ads   # log in when prompted
```

In Claude Code, ask it to search the Ad Library for Kapiva (IN) and Forest Essentials UK (GB).
Record which fields, countries and media links come back, and add the result to `docs/decisions.md`.
If you later connect your own ad account, first block write actions under Business Suite >
Settings > Integrations > Ads MCP server.

## Layout

```
config/competitors.yaml      test brands (UK pick not final)
schemas/ad.schema.json       per-ad schema v0.1 (research §7.3)
scripts/                     fetch_ads, normalize, text_metrics, llm_batches, build_report,
                             purge_expired, fixture_api (offline), common
prompts/                     extraction, rubric, report template (versioned)
.claude/agents/              ad-extractor, report-writer
.claude/skills/analyze-competitor/
data/raw|ads|llm|media/      local only, git-ignored (retention: AGENTS.md)
reports/<target>/            <date>-competitor-report.md (git-ignored)
docs/                        research, decisions, compliance
tests/                       pytest + synthetic fixtures
```

## Retention

`$PY scripts/purge_expired.py` lists files past retention (raw and LLM batches 180 days, media 90 days);
add `--apply` to delete them.
