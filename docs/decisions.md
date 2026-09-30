# Decisions

Newest first. Each entry records the decision, why it was made, and the status.

## 2026-09-30 - Phase 1 M1 "safe + visible" (plan: firstmate `fbcm-p1-plan`, captain "go")

| # | Decision | Why | Status |
|---|---|---|---|
| 13 | **P1-01: the report shows every API sub-element per ad** (full deduped primary text, headline, description, caption, platforms, languages, dates, UK/EU reach, age x gender, targeting, payer) as "Ad details" cards, plus `reports/<target>/<date>-ads.csv` | The captain had not seen the sub-elements; Phase 0 stored them but printed only an 80-char first line | Built + tested |
| 14 | Schema v0.2 is additive: `derived.text_status` (`has_text` / `creative-only`), `derived.variants_unique`, `derived.uk_reach` (`value` + `basis`), `derived.reach_by_country`. Older files are upgraded in memory (`text_metrics.upgrade_ad`) and rewritten on the next normalize | Deterministic values belong in `derived`, which is recomputed from `text` and `eu_metrics`, so no refetch is needed | Built |
| 15 | UK reach = `total_reach_by_location["UK"]`; if absent, the sum of the GB age x gender breakdown, labelled "breakdown sum" | UK-only ads carry no `eu_total_reach`, so Phase 0 showed no reach for UK advertisers | Built |
| 16 | Countries come from the run header, config or `--country`, never code. A query limited to non-EU/UK countries that returns 0 ads gets a plain coverage statement in the fetch output and the report | Captain: "Not just uk.. i should be able to ask any country" | Built |
| 17 | CTA button, images, video, on-image text and full landing URL are not in `ads_archive`; they are left to the Phase 1 visuals ladder (plan §3.3, decision C5) | ArchivedAd v26.0 reference | Noted |
| 18 | **P1-02: every Meta call goes through the deterministic `scripts/guard.py`**, with the policy in `config/guard.yaml` (plan §6.2 defaults, plus a breaker of 5 consecutive failures leading to a 15-minute cooldown). State lives in `data/state/guard/`, and ledgers in `data/runs/` | Phase 0 retried a code-4 error 5 times in about 68 s (6 calls), against Meta's "stop making API calls" | Built + tested |
| 19 | Throttle codes are never retried; only 1/2, HTTP 5xx and network errors are (2/8/30 s). Kill-switch codes: 190, 368, 459, 464, 10, 200-299, 2332002, 2332004, 613/1996. Only a human at a TTY can `unkill` | Plan §6.1 / §6.2 | Built + tested |
| 20 | Fixture runs also go through the guard (kill switch, lock, run cap, ledger), but are not counted against the hourly/daily Meta caps or cooldowns | Offline runs are not Meta calls, but must still stop when the kill switch is on | Built |
| 21 | The token is stripped from `ad_snapshot_url` before any raw write (the parameter is removed, not masked, so `grep -r access_token data/` is empty). Raw and ledger files are 0600 | Phase 0 raw files were 0644 and held the token | Built + tested |
| 22 | `normalize.py` refuses a raw run without its closing `summary` line unless `--allow-partial` is passed. Stopped runs end with a `stopped` line that lists the missing windows | Phase 0 normalized aborted runs silently | Built + tested |
| 23 | Keyword queries send `search_type`, defaulting to `KEYWORD_EXACT_PHRASE`. The fixture now follows the API: `KEYWORD_UNORDERED` (the API default) matches each word separately | Phase 0 sent no `search_type`, and the fixture pretended an exact match, so tests missed the Explorer behaviour | Built + tested |
| 24 | `.gitignore` ignores `data/*` except `.gitkeep`, plus `logs/` | `data/state`, `data/runs` and `logs` were committable | Built |
| 25 | The guard's event log is `data/state/guard/events.jsonl` (token-scrubbed), not `logs/guard.log` | Keeps all guard state under one ignored folder, and lets tests use a temporary data dir. `logs/` is reserved for launchd output (P1-12) | Built |
| 26 | **P1-03: token lifecycle.** `token_store.py` has two backends, `.env` (0600 enforced) and macOS Keychain (`security -i`, so the token never goes on argv). **`.env` stays active**; the current token was not migrated. `token_cli.py rotate --to keychain` exists but has not been run | C2 (Keychain vs `.env`) is still an open captain decision | Built + tested |
| 27 | `token_cli.py status` uses `debug_token` (input and access token = the user token), cached 24 h in `data/state/guard/token.json` (metadata only, no `user_id`), with warnings at 14/7/3 days. An invalid token trips the kill switch. A permission error from `debug_token` itself does not | Plan §6.1. A permission denial there means the endpoint can't introspect, not that the account is at risk | Built + tested |
| 28 | `rotate` refuses to run without an interactive terminal, and bypasses only the kill switch (caps and cooldowns still apply), because rotating is how a token kill is resolved | Secrets must not pass through Claude; a dead token must not block its own replacement | Built + tested |

## 2026-09-29 - Phase 0 build

| # | Decision | Why | Status |
|---|---|---|---|
| 1 | **Run locally in Claude Code on the official Ad Library API (`ads_archive`).** Nothing is hosted inside facebook.com | No non-game tool can be hosted inside Facebook any more (Canvas ends 2026-09-30, and Page tabs can't be created). See research §1 | Decided |
| 2 | **Official Meta APIs only.** No scraping or automated browsing of facebook.com, the Ad Library website, or third-party scraper MCPs | Meta Terms §3.2 bans automated collection. See `docs/compliance.md` | Decided (captain) |
| 3 | **Phase 0 scope:** Page ID or keyword plus a month range -> every ad -> grouped by month with permalinks and text -> Claude extracts hook, offer, CTA, tone, length and quality -> markdown report | Research §6-7. Smallest useful loop | Built |
| 4 | Deterministic work is done by Python scripts (fetch, normalize, metrics, report numbers). Judgement is done by Claude Code sub-agents (`ad-extractor`, `report-writer`), with outputs validated against `schemas/ad.schema.json` | Repeatable numbers and a validated LLM layer; prompts are versioned (`extract-v1`) | Built |
| 5 | Sub-agents specify no hardcoded model IDs: `ad-extractor` uses the `sonnet` alias and `report-writer` uses `inherit` | Avoid pinning IDs that may not exist | Built |
| 6 | One query per calendar month (`ad_delivery_date_min/max`), `ad_active_status=ALL`, and every `paging.next` is followed. Ads are deduplicated by library ID at normalize time | Meets the research acceptance criteria. Month windows keep pages small | Built |
| 7 | Reports link only to the public permalink `https://www.facebook.com/ads/library/?id=<id>`. `ad_snapshot_url` stays in `data/raw/`. Writers refuse any output containing `access_token` | The snapshot URL embeds the access token | Built + tested |
| 8 | **Meta app type: Consumer** ("FBCM Ad Library Test"), not Business | The Business-type app could not issue user tokens in Graph API Explorer. This corrects research §3/§7.2 | Decided (observed by captain) |
| 9 | Test brands are kept in `config/competitors.yaml`: Kapiva (IN, expected empty through the API), Forest Essentials UK (GB, Page ID `100331625781604`, unverified), Pukka Herbs (GB, fallback, Page ID unknown) | The UK pick is not final. Kapiva probes the India coverage gap | Open: captain confirms the UK brand |
| 10 | Offline testability: a synthetic fixture API (`scripts/fixture_api.py`) plus `--fixture` / `--dry-run` modes, so the whole pipeline runs without Meta access | The captain's API access is pending identity review (about 48h) | Built |
| 11 | `data/ads/`, `data/llm/` and `reports/<target>/` are git-ignored, not just `data/raw/` and `data/media/` | Normalized ads are still Platform Data (competitor copy). Keeping them out of git keeps the retention policy enforceable | Built. Revisit if the captain wants reports versioned |
| 12 | Retention: raw pulls and LLM batches 180 days, media 90 days, reports kept. Enforced by `scripts/purge_expired.py` | Platform Terms §3.d. Research §5 | Built |

## Deferred (out of Phase 0)

- **Step-0 coverage spike with the Meta Ads MCP** (`https://mcp.facebook.com/ads`, tool `ads_library_search`): run it once access exists, to learn whether it returns non-EU (for example India-only) commercial ads. See README "Step 0". Not wired into the pipeline.
- Creative and video analysis (Phase 1): per-ad, on-demand media download, kept for 90 days.
- Similarity (Phase 2), own-account insights through the Ads MCP (Phase 3), and Messenger (Phase 4).

## Open captain decisions (research §8)

D3 (own ad account, read-only, in Phase 3), D4 (Claude billing route), D5 (personal vs client scope), D6 (creative policy: recommended per-ad on demand).
