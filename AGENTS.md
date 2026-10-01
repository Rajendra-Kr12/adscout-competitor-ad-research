# Facebook CM Agent - agent rules

A local Claude Code agent that analyses competitors' Meta ads through the official Ad Library API.
Setup, usage and coverage caveats: `README.md`. Decisions: `docs/decisions.md`. Research: `docs/research/fbcm-research-r1.md`.

## Hard rules

- **Official Meta APIs only** (`ads_archive`, and later the Meta-hosted Ads MCP). Never scrape facebook.com or the Ad Library website, and never drive them with a browser agent (Claude in Chrome, computer use, Playwright, chrome-devtools), even "just to check". A human can look; the agent can't. Don't install third-party Ad Library scrapers or "Meta Ads MCP" proxies. See `docs/compliance.md`.
- **Every Meta call goes through `scripts/guard.py`** (caps, cooldown, kill switch, lock, ledger; policy in `config/guard.yaml`). Never add a call path around it, never retry throttle codes, and never run `guard.py unkill` or `clear-cooldown`: a tripped guard is for the captain to clear.
- **`ad_snapshot_url` never leaves `data/raw/`**. The API embeds the access token in it; `fetch_ads.py` strips the token before writing raw files. Outputs (`data/ads/`, `data/llm/`, `reports/`, chat) cite ads only as `https://www.facebook.com/ads/library/?id=<library_id>`. No file outside the token store may contain `access_token`, raw files included; `fetch_ads.py`, `write_json` and `build_report.py` enforce this, so don't bypass them.
- **The token is read only by `scripts/token_store.py`** and must never be printed, logged or copied; show `token_store.fingerprint` instead. Only the captain runs `token_cli.py rotate`, in their own Terminal.
- **Never commit** `.env`, `data/` contents or `reports/<target>/`. They are git-ignored, and `.githooks/pre-commit` refuses them and token-shaped strings (enable once with `git config core.hooksPath .githooks`).
- `.claude/settings.json` and `.claude/hooks/guard_pretool.py` enforce these rules on tool calls. A block is the rule working: don't work around it or weaken either file without the captain's say-so. Edit/Write are not checked, so edit docs that mention `access_token=` with them, not shell heredocs.
- **Retention:** raw pulls, LLM batches and discovery records 180 days, media 90 days, reports kept. Use `scripts/purge_expired.py`.
- **Creative media (Phase 1, not built):** download only per ad, on demand, and only for analysis. No bulk mirroring.
- Never present competitor spend, CPM, CPA or ROAS. They are not available from official sources.

## Working here

- Python venv: `.venv/bin/python`. Tests: `.venv/bin/python -m pytest -q`. The tests are fully offline and use synthetic fixtures (`tests/fixtures/`, `scripts/fixture_api.py`).
- Without a token, use `--dry-run` (request plan) or `--fixture tests/fixtures/ads_archive_synthetic.json`.
- Deterministic numbers come from scripts. Judgement comes from the sub-agents in `.claude/agents/`, and their output is validated against `schemas/ad.schema.json` (v0.2; `SCHEMA_VERSION` lives in `scripts/text_metrics.py`, keep changes additive and let `upgrade_ad` migrate old files). If you change a prompt in `prompts/`, bump its version (`extract-v1`).
- Test brands and Page IDs live in `config/competitors.yaml`, not in code. Countries come from config, a project or `--country`; never hard-code GB. Outside the EU/UK, 0 commercial ads is coverage, not an error, and outputs must say so.
- Multi-brand work goes through projects (`config/projects/<slug>.yaml`; `scripts/project.py`, `discover_pages.py`, `run_project.py`; README "Multi-brand projects"). Any country per brand: EU/UK routes to `ads_archive`, everything else to the Meta Ads MCP route, which is a stub until the captain connects it (`scripts/mcp_source.py`) and gives headlines and dates only. Never present it as full detail. Page IDs are confirmed by the user, never guessed.
- `/meta-keyword-discovery` (optional, after a project) finds new competitors by keyword search through the guard (lean fields, budget, month/week windows), classifies every advertiser by the criteria in `config/discovery.yaml` (dropped Pages stay listed with a reason) and stops for the user's pick before any deep dive.
- `/research-brands` is the master: it runs in the main thread (only it can ask the user) and calls the read-only `research-critic` at the plan, data and report gates. Record critiques only through `scripts/critique.py record`; a blocker from `critique.py check` must be fixed, never dropped. If you change `prompts/intake.md` or `prompts/critic.md`, bump their versions.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
