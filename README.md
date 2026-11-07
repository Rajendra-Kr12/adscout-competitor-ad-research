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
- **Any country can be queried** (`--country` takes any ISO-2 code, repeatable; a project takes any
  countries per brand). For a country outside the EU/UK the report says plainly that the API only
  covers commercial ads delivered to the EU/UK, so an empty result is coverage, not an error.
- **Outside the EU/UK, Meta's Ads MCP (`ads_library_search`) is the only official route**, and it gives
  discovery only: headline, start date, Page and permalink for live ads, at most 50 newest per call,
  no body text, CTA, media, reach or stop date (tested 2026-09-30). It is **not connected yet**: it
  needs your own Meta login and an active ad account. Projects record it as each such brand's
  coverage plan; full text and visuals there will come from the screenshot checklist (stage 3).
- Competitor **spend, impressions, CPM, CPA, ROAS and engagement are not available** from any
  official source. UK/EU-delivered ads carry reach (UK reach from `total_reach_by_location`, else
  the sum of the GB age x gender breakdown, labelled as such), age x gender breakdowns and targeting.
  EU reach exists only for EU-delivered ads.
- The analysis is **text only**: primary text, headlines, link descriptions and captions. The CTA
  button label, images, video, on-image text and the full landing URL are **not in the API**.
  Ads with no text are labelled `creative-only`.
- "Days running" is a proxy for what works, not performance data.

Background: `docs/research/fbcm-research-r1.md` (sections 2, 4-7). Decisions: `docs/decisions.md`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # then fill in; .env is git-ignored and must stay 0600
.venv/bin/python -m pytest -q
git config core.hooksPath .githooks   # once per clone: enables the pre-commit token/data check
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

The Explorer's token expires in about an hour. Exchange it for a long-lived one **in your own Terminal
tab, never in a Claude chat**:

```bash
.venv/bin/python scripts/token_cli.py rotate                 # store in the active store (.env by default)
.venv/bin/python scripts/token_cli.py rotate --to keychain   # or the macOS Keychain (see "Token store")
```

It asks for the short-lived Explorer token and the App Secret without echoing them, calls
`oauth/access_token?grant_type=fb_exchange_token` through the guard, stores the result, and prints only
its fingerprint and expiry. The App Secret is never stored. `.env` also holds `META_APP_ID` and
`META_API_VERSION=v26.0`. Repeat before the token expires (about every 60 days). An expired token
gives error **190** and trips the kill switch; rotating works even while it is on.

```bash
.venv/bin/python scripts/token_cli.py status      # fingerprint, validity, expiry, days left (never the token)
.venv/bin/python scripts/token_cli.py scan [PATH]  # files holding the token or a token-shaped string (paths only)
```

`status` calls `debug_token` at most once a day (`--refresh` forces it), caches metadata only in
`data/state/guard/token.json`, and warns 14, 7 and 3 days before expiry. Live fetches run the same
check first.

### Token store

`scripts/token_store.py` is the only code that reads the token. The active store is `.env` (the
default, `token.store` in `config/guard.yaml`), which must be mode 0600 or it is refused. The macOS
Keychain backend (service `fbcm.meta`, account `user_token`) is built but not active: choosing it is an
open captain decision (C2). `rotate --to keychain` switches to it and leaves the `.env` line for you to
remove. `FBCM_TOKEN_STORE=env|keychain` or a `META_ACCESS_TOKEN` environment variable override the
choice for one command. Never copy the token to a second machine or a cloud routine: Meta warns that
reusing one long-lived token across clients can trigger its spam systems.

### Error meanings

| Code | Meaning | Fix |
|---|---|---|
| **2332002** | Identity or Ad Library API access is not approved yet | Finish facebook.com/ID and wait |
| **2332004** | An app access token was used | Generate a **user** token in Graph API Explorer |
| **190** | Token invalid or expired | New user token, then run the long-lived exchange again. Also trips the kill switch |
| 4, 17, 32, 341, 613, 80000-80014 | Rate limited | **Never retried.** The run stops, the guard sets a cooldown of at least 60 minutes, and later runs are refused until it ends |
| 613 / subcode 1996 | Inconsistent request volume | Kill switch trips (see "API guard") |
| 368, 459, 464 | Policy block, account checkpoint, unconfirmed user | Kill switch trips. A human resolves it on facebook.com; the agent never does |
| 100 | Invalid parameter | Check the page ID, country codes and date format. Dates after Meta's own "today" (Pacific time) are refused; scripts clamp to UTC minus 12 hours |
| 0 ads for an IN/US brand | Coverage, not an error | Only EU/UK-delivered commercial ads are in the API |

## API guard

Every Meta call goes through `scripts/guard.py`, with the policy in `config/guard.yaml` and state in
`data/state/guard/`. It enforces:

- **Caps** of 200 calls per run, 150 per hour and 600 per day (persisted across processes), with at least 1.5 s between calls.
- **Usage headers** (`X-App-Usage`, `X-Business-Use-Case-Usage`, `X-Ad-Account-Usage`): at 75% it pauses 5 minutes; at 90% it stops and cools down for 60 minutes.
- **No retry on throttling.** Only codes 1/2, HTTP 5xx and network errors are retried (3 times: 2, 8, 30 s). 5 consecutive failures open a 15-minute circuit breaker.
- **Kill switch** (`data/state/guard/KILL`, or `FBCM_KILL=1`): it trips on 190, 368, 459, 464, 10/200-299, 2332002/2332004 and 613/1996, and blocks every run until a human clears it.
- **One run at a time** (a lock file), and a **ledger** per run in `data/runs/<run_id>.json` (calls, pages, windows done/missing, errors, usage peaks, cooldowns).

```bash
$PY scripts/guard.py status              # kill switch, cooldown, calls used, last run
$PY scripts/guard.py kill "reason"       # stop everything
$PY scripts/guard.py unkill              # human only: needs a typed confirmation in a terminal
$PY scripts/guard.py clear-cooldown      # human only, same confirmation
```

A run that stops early leaves a raw file without its summary line. `normalize.py` refuses it unless
you pass `--allow-partial`, and the ledger lists the missing windows.

## Agent guardrails

- `.claude/settings.json` (committed) denies reading `.env` and `data/raw/`, WebFetch to facebook.com
  and fbcdn.net, Claude in Chrome, and `lavish-axi share`. It also registers the PreToolUse hook.
- `.claude/hooks/guard_pretool.py` checks every tool call. It blocks:
  - facebook.com and fbcdn.net access by WebFetch, browser MCPs or network commands (`graph.` and `developers.facebook.com` are allowed);
  - any Bash, WebFetch or MCP input containing `access_token=`, and Keychain token reads;
  - `.env` and direct `data/raw/` reads;
  - `lavish-axi share`;
  - `claude mcp add` of third-party Meta Ads MCP servers.

  It uses the standard library only (`python3`), so it works without the venv. Edit and Write are not
  checked, so docs that mention `access_token=` are edited with those tools rather than shell heredocs.
- `.githooks/pre-commit` refuses staged `.env`, `data/`, `reports/<target>/` or `logs/` files, and any
  content that looks like a Meta token (`EAA...`). Enable it with `git config core.hooksPath .githooks`.

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
results can include other Pages, so pass `--only-page-id` to `normalize.py`). Keyword queries default to
`--search-type KEYWORD_EXACT_PHRASE`. With `KEYWORD_UNORDERED` (the API's own default) each word is
matched separately, in any order, which is how "Forest Essentials" pulls in unrelated ads. A competitor
can set `search_type` in `config/competitors.yaml`. Add
`--media-types IMAGE,VIDEO,MEME,NONE` to label media types. This costs one query per type.
Competitors live in `config/competitors.yaml`.

### Multi-brand projects

In Claude Code: `/research-brands "Forest Essentials UK, Pukka Herbs, Kapiva (India)"`
(`.claude/skills/research-brands/SKILL.md`). The master research agent runs in the main thread:

1. resolves each brand's Page (1 exact-phrase call per unknown brand; you confirm ambiguous Pages);
2. asks the intake questions in short rounds (`prompts/intake.md`): markets (any countries), window, goal,
   folder layout, outputs, and preferences for visuals and monitoring, which are saved for later stages.
   "Use recommended defaults" skips straight to the plan;
3. runs the read-only `research-critic` sub-agent (`prompts/critic.md`) at three gates: **plan** (coverage
   per country, Page identity, window, call budget, compliance), **data** (stopped runs, 0-ad brands, other
   Pages' ads) and **report** (every number and ad ID traced, no spend claims, permalinks only, nothing
   implying full detail outside the EU/UK). Each finding has evidence and reasoning; choices come back to you
   as questions. `scripts/critique.py check` computes the findings that must never depend on the LLM, and
   `critique.py record` refuses a critique that drops one of their blockers;
4. runs the project through the guard, then `ad-extractor`, then `report-writer` for each brand and for
   `comparison.md`.

The same steps by hand. For 2 or more brands, use a project (`config/projects/<slug>.yaml`, `schemas/project.schema.json`). It
saves the intake answers (brands, markets, window, goal, folder layout, outputs, and preferences for
later stages), so re-runs never ask again. Every answer has a recommended default.

```bash
$PY scripts/project.py new ayurveda-uk --brand forest-essentials-uk --brand "Pukka Herbs" --brand "Kapiva:IN"
$PY scripts/discover_pages.py --project ayurveda-uk          # 1 exact-phrase call per brand without a Page ID
$PY scripts/project.py set-page ayurveda-uk pukka-herbs <page_id> --source discovery   # after you confirm it
$PY scripts/project.py plan ayurveda-uk                      # brands, routes, coverage, call estimate vs caps
$PY scripts/run_project.py ayurveda-uk --dry-run             # the exact requests; nothing is sent
$PY scripts/run_project.py ayurveda-uk                       # fetch through the guard, then write outputs
$PY scripts/run_project.py ayurveda-uk --no-fetch            # rebuild outputs from data/ads (no API calls)
```

- **Brand spec:** `NAME_OR_KEY[:CC+CC][@PAGE_ID]`. Keys from `config/competitors.yaml` bring their
  Page ID. Countries are any ISO-2 codes (`UK` is read as Meta's `GB`); without them the project's
  `markets` apply.
- **Routing:** EU/UK countries go to `ads_archive` (full detail). Other countries go to the Meta Ads MCP
  route, which is a stub in this stage, so those brands get a report that states their coverage plan
  and 0 fetched ads. Brands without a Page ID are reported as unresolved, never guessed.
- **Calls:** brands that share a country set are batched, up to 10 Page IDs per `ads_archive` query, so a
  group costs one query per month window (+ paging). `--single-window` makes it one query per group.
- **Folders** (`layout`, chosen at intake; `output_root` is `reports` or an absolute path outside the repo):
  - `project/date/brand` (default): `reports/<p>/<date>/index.md`, `run.json`, `<brand>/report.md`,
    `<brand>/ads.csv`, plus `reports/<p>/latest -> <date>` and `reports/<p>/project.md`
  - `brand/date`: `reports/<p>/<brand>/<date>/...`, with run files in `reports/<p>/runs/<date>/`
  - `flat`: Phase 0 names, `reports/<brand>/<date>-competitor-report.md`, run files in `reports/<p>/`
- `index.md` has the cross-brand table, each brand's coverage plan, what was not fetched and why, and a
  critic block. Rebuilding keeps the report-writer notes and the critic block.
- **Exports** (`outputs` in the project; C4 stays the user's choice, xlsx + CSV by default): `workbook.xlsx`
  and `csv/` in the run folder, written by `scripts/export_xlsx.py` (re-run alone with
  `$PY scripts/export_xlsx.py <slug> --date <YYYY-MM-DD>`). Sheets: About, Ads (one row per ad with the full
  text of every variant, headline, description, caption, LLM fields, reach and targeting), Variants, Monthly,
  Offers, Reach (age x gender), Changes (empty until monitoring) and Coverage (per brand: route, coverage plan,
  why a brand has 0 ads, LLM coverage, critic findings). Links are public permalinks; no images are embedded;
  text starting with `=` stays text. The HTML dashboard and the Google Drive copy are not built yet.

### What the report contains

`reports/<target>/<date>-competitor-report.md` has the summary, per-month tables and ledger, then an
**Ad details** card per ad: the full primary text (identical variants shown once with a `×n` count),
headline, link description, caption, platforms, languages, dates, UK and EU reach, the age x gender
table, targeting and beneficiary/payer. `reports/<target>/<date>-ads.csv` has one row per ad with the
same fields plus the LLM columns (cells starting with `= + - @` are prefixed with `'` so spreadsheets
never run them as formulas).

### Keyword discovery (optional deep-dive step)

In Claude Code: `/meta-keyword-discovery <project-slug>` (`.claude/skills/meta-keyword-discovery/SKILL.md`: rules and
cautions first, then the workflow; detail in its `references/`: classification criteria, commands, report template,
worked example). It finds
competitors you are not tracking yet, from the words your tracked brands lead their ads with, then **stops for your
pick** before any deep dive. The same steps by hand:

```bash
$PY scripts/keyword_terms.py --project ayurveda-uk --language en            # no calls: terms by distinct copy cluster
$PY scripts/discover_keywords.py --name ayurveda-uk --keyword "dark circles" --keyword "menopause skin:unordered" \
    --country GB --since 2026-07 --until 2026-09 --window month --dry-run   # the plan and its call ceiling
$PY scripts/discover_keywords.py ...same arguments without --dry-run...     # through the guard
$PY scripts/discovery_pages.py --name ayurveda-uk --project ayurveda-uk --out pages.json --tables-md tables.md
```

- **Terms:** 1-3 word phrases from each ad's first line and headline. Each copy cluster counts once, so one brand's
  repeats cannot swamp the list. You choose the 5 search terms; the script only ranks them.
- **Search:** lean fields (Page, dates, headline, link caption; no ad text, no snapshot link), about 300 ads per page,
  and a hard `--budget` (default 25 calls; the plan is refused if it could exceed it). Nothing is retried, and the run
  stops at the first error, guard stop or usage warning. The API returns the newest ads first, so each keyword is
  split into **monthly or weekly windows** with a page cap per window. One window over a quarter samples only its
  last days. Records go to `data/discovery/<name>/` (0600, 180-day retention).
- **Classification:** every advertiser Page gets one class by explicit, overridable criteria in
  `config/discovery.yaml`:
  - **Kept:** brand owner, or a brand-led *channel* (a retailer, distributor or creator whose ads mostly name one
    established brand).
  - **Dropped:** multi-brand retail, advertorial or persona funnel, clinic or B2B, off-category, reseller funnel,
    unverified.
  - Dropped Pages are listed with their reason and ad count, so a person can overrule them. A channel-versus-brand
    table shows the same brand on its own Page versus other Pages.
- **Hand-off:** the chosen Page IDs go into a project (`project.py new ... --brand "Name:GB@<page_id>"`), and
  `/research-brands` runs the usual gated pipeline on them.
- Keyword results are a sample, not the market. Never read them as share, spend or reach.

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
Page `900000000000003` has a GB-only ad (UK reach, no EU reach), a `creative-only` ad, a DE/AT ad
(`--country DE`) and a US-only ad that is never returned (`--country US` shows the coverage message).

## Handoff to the Instagram creative agent

`/export-handoff <project-or-brand-slug>` (or `$PY scripts/export_handoff.py <slug>`) writes `handoff/<slug>.md`,
a one-page brief another agent can build creative angles from. It reads stored data only (no Meta call, no
browser), so it is demo-safe: `$PY scripts/export_handoff.py demo --fixture tests/fixtures/ads_archive_synthetic.json`
builds the synthetic dataset offline. `--dry-run` prints what it would read and writes nothing. `handoff/*` is
git-ignored (real competitor copy) and refused by the pre-commit hook.

Format (about 30-70 lines): title; a stamp line (date, source, window, brand and ad counts); the tag legend;
then `## Coverage` (one line per brand, a 0-ad market says plainly that it is a coverage gap),
`## What they run`, `## Hooks and angles (longest-running first)` (two hooks per brand with an Ad Library link,
then offer, tone and price patterns), `## Whitespace (Hypothesis, ...)`, `## Visual direction`, `## Reference links (human picks)` and `## Unknown (do not invent)`.
Every bullet starts with one tag: Observed (in the ads), Pattern (repeats, with counts), Hypothesis (our
reading) or Unknown (not available from official sources). Evidence is `https://www.facebook.com/ads/library/?id=<library_id>`
only. Hooks are the extracted `hook` when the extract step ran, otherwise the literal first line of the ad.

`## Visual direction` is built only from stored fields (media type when it was queried per type, creative-only ads,
Instagram placement) and says plainly that framing, colour, styling and text-on-image are Unknown: the Ad Library
API gives text and metadata, not image fields. Nothing is downloaded or scraped to fill the gap.
`## Reference links (human picks)` is an empty 3-line placeholder for the captain. By default the agent leaves it
alone and never pulls Pinterest or other pins; the only thing that may fill it is the opt-in Unsplash step below.

### Unsplash photo suggestions (optional)

Pinterest pin search is closed and Pinterest's rules bar storing pins and competitor research, so the visual
reference step uses the official Unsplash API instead (stock photography for mood, light and colour, not
competitor creative). It is opt-in and separate from the offline brief:

```bash
$PY scripts/export_handoff.py <slug> --unsplash                 # write the brief, then add suggestions
$PY scripts/unsplash_refs.py suggest <slug> [--query "natural skincare"] [--count 4] [--dry-run]
$PY scripts/unsplash_refs.py choose  <slug> S2                  # a human picked S2: record it
$PY scripts/unsplash_refs.py status                             # key fingerprint, cooldown, call counts
```

- **Query.** The product category from the project title (the text before " - ", minus a leading country word:
  "UK natural skincare - 8 brands" gives "natural skincare"), or `--query`.
- **Output.** 3-5 lines in the Reference links block, between `unsplash-suggestions` comments, each unchecked
  (`- [ ] S1 ...`) and headed "NOT confirmed: a human picks". Each has the photo link, the photographer and
  Unsplash credit (UTM-tagged links, as the API terms ask) and the hotlinked image URL exactly as returned.
  Re-running replaces the block; lines you filled in yourself stay.
- **Choosing.** `choose` calls Unsplash's download-tracking endpoint (the API terms require it when a photo is
  chosen; the image itself is not fetched) and marks the line `[x] ... CONFIRMED by a human`. Only a human runs it.
- **Key.** Put `UNSPLASH_ACCESS_KEY=...` in `.env` (git-ignored, mode 0600; also add the name to your
  `.env.example` as an empty placeholder) or export it. Create it yourself at https://unsplash.com/oauth/applications.
  Only `scripts/unsplash_store.py` reads it; it is sent in the `Authorization` header, never in a URL, and only its
  fingerprint is ever shown. Demo-mode keys allow 50 requests/hour.
- **No key, or the call fails** (rate limit, rejected key, network, local cap): the block is filled from a clearly
  labelled `SAMPLE RESULT` (`tests/fixtures/unsplash_search_sample.json`, fake ids and links) so a demo never breaks.
  `--require-live` makes it fail instead.
- **Limits.** `config/guard.yaml` `unsplash:` sets 3 calls per run, 40 per hour (under the 50 demo limit), 200 per
  day, 2 s spacing; a rate limit or low `X-Ratelimit-Remaining` cools down for 60 minutes, a rejected key for a
  day; nothing is retried. State and a call ledger live in `data/state/unsplash/`; this limiter is separate from
  the Meta guard and kill switch.
- **Terms kept.** Official API only; hotlinks only; nothing downloaded or mirrored; `data/unsplash/<slug>.json` holds
  ids, links and credits (180 days, no images); results are never used to train or fine-tune a model.

Pinterest Trends is prepared but **off**: `scripts/pinterest_trends.py` is a stub with no network code, and
`docs/pinterest-trends.md` lists what switching it on needs (app approval, OAuth token, scopes, limits, and the open
terms questions, starting with you reading Pinterest's Developer and API Terms before registering an app).

## Meta Ads MCP (tested, not connected)

The Step-0 spike ran on 2026-09-30 (`docs/decisions.md` #33-#35). `ads_library_search` returns commercial
ads for any country, with only 8 fields (id, Page, headline, creation/start time, permalink, currency) and
no media, so it is a discovery and monitoring source, not a replacement for `ads_archive`.
`scripts/mcp_source.py` is the interface for that route; nothing calls Meta through it yet. Connecting it
needs your own login (`claude mcp add --transport http meta-ads https://mcp.facebook.com/ads`). The login
grants write scopes, so first block write actions under Business Suite > Settings > Integrations > Ads MCP
server, and allow only the `ads_library_search` tool.

## Layout

```
config/competitors.yaml      test brands (UK pick not final)
config/projects/<slug>.yaml  multi-brand research projects (intake answers; no Platform Data)
schemas/project.schema.json  project file schema (project-v1)
schemas/critique.schema.json research-critic output (critic-v1)
schemas/ad.schema.json       per-ad schema v0.2 (research §7.3; additive over v0.1)
config/guard.yaml            API guard caps and thresholds
config/discovery.yaml        keyword discovery: stopwords, lean fields, budget, advertiser classification criteria
.claude/settings.json        permission deny rules + PreToolUse hook (.claude/hooks/guard_pretool.py)
.githooks/pre-commit         token / Platform Data commit check
scripts/                     fetch_ads, guard, normalize, text_metrics, llm_batches, build_report,
                             project, discover_pages, run_project, export_xlsx, export_handoff, critique, mcp_source (stub),
                             keyword_terms, discover_keywords, discovery_pages (keyword discovery),
                             unsplash_refs, unsplash_store (optional photo suggestions), pinterest_trends (disabled stub),
                             purge_expired, fixture_api (offline), common
prompts/                     extraction, rubric, report and comparison templates, intake, critic (versioned)
.claude/agents/              ad-extractor, report-writer, research-critic
.claude/skills/              analyze-competitor (one brand), research-brands (master, several brands),
                             meta-keyword-discovery (optional: find new competitors by keyword),
                             export-handoff (one-page brief for the Instagram creative agent)
data/raw|ads|llm|discovery|media/ local only, git-ignored (retention: AGENTS.md); raw files are 0600, token stripped
data/state/guard/, data/runs/ guard state and run ledgers (git-ignored; data/* is ignored as a whole)
data/state/unsplash/, data/unsplash/ Unsplash limiter state + call ledger; suggestion ids and credits (no images)
reports/<target>/            <date>-competitor-report.md and <date>-ads.csv (git-ignored)
handoff/<slug>.md            one-page creative-agent brief (git-ignored)
reports/<project>/           project.md, latest, dated run folders (layout per project; git-ignored)
docs/                        research, decisions, compliance
tests/                       pytest + synthetic fixtures
```

## Retention

`$PY scripts/purge_expired.py` lists files past retention (raw, LLM batches, discovery records and Unsplash
suggestion records 180 days, media 90 days);
add `--apply` to delete them.
