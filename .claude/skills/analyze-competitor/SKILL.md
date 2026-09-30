---
name: analyze-competitor
description: End-to-end competitor ad analysis from Meta's official Ad Library API - fetch every ad for a Page or keyword over a month range, normalize, extract hook/offer/CTA/tone/quality with ad-extractor sub-agents, build the monthly markdown report and have report-writer add the analysis. Use for "/analyze-competitor <competitor-key|page-id> <YYYY-MM>..<YYYY-MM>" or any request to analyse a brand's Facebook/Instagram ads.
argument-hint: <competitor-key | page-id | "search terms"> <since YYYY-MM>..<until YYYY-MM> [--country GB] [--fixture]
---

# /analyze-competitor

Read the project `AGENTS.md` first; its rules override anything here. In short: official APIs
only, no browsing of facebook.com, and no snapshot URLs or tokens in outputs.

Use `.venv/bin/python` (see README "Setup"). `<target>` is the competitor key from
`config/competitors.yaml`, or whatever `--out-key` / Page ID the fetch used.

## 0. Preflight

- Resolve the target: a key in `config/competitors.yaml`, or a numeric Page ID (then `--country` is required), or keyword search terms.
- Live runs need `META_ACCESS_TOKEN` in `.env`. If it is missing, stop and point to the README, or offer the offline demo (`--fixture tests/fixtures/ads_archive_synthetic.json --page-id 900000000000001 --country GB --out-key synthetic-uk`).
- Run `.venv/bin/python scripts/guard.py status`. If the kill switch or a cooldown is on, stop and tell the captain why. Never run `guard.py unkill` or `clear-cooldown`; only the captain does that.
- Show the plan without sending anything: `.venv/bin/python scripts/fetch_ads.py <target args> --since A --until B --dry-run`. It also prints the calls already used against the guard caps.

## 1. Fetch (deterministic)

```
.venv/bin/python scripts/fetch_ads.py --competitor <key> --since <A> --until <B>
```

- Add `--media-types IMAGE,VIDEO,MEME,NONE` when the media mix matters. This costs 4x the queries.
- Exit code 2 or 3 means the run stopped (API error or guard). Relay the message and the ledger path, and do not retry. That covers `2332002` (identity not approved), `2332004` (app token), `190` (expired token), throttling (cooldown) and the kill switch. `normalize.py` refuses the partial raw file; use `--allow-partial` only if the captain wants the partial data.
- 0 ads for a non-EU/UK market is the expected coverage gap, not a bug. Still build the report, because it explains the gap.
- Keyword runs can return other Pages' ads. Pass `--only-page-id <id>` to normalize if needed, and record the discovered Page ID in `config/competitors.yaml` with `page_id_verified: true`.

## 2. Normalize

```
.venv/bin/python scripts/normalize.py --target <target>
```

## 3. Extract (sub-agents, parallel)

```
.venv/bin/python scripts/llm_batches.py plan --target <target> --size 20
```

Each output line reads `<input> -> <output>`. Launch one `ad-extractor` sub-agent per line, all in
the same turn so they run in parallel. Give each one only its two paths. Then merge:

```
.venv/bin/python scripts/llm_batches.py merge --target <target> data/llm/<target>/<run>/output-*.json
```

If the merge rejects ads, re-run their batches once, quoting the rejection reasons to the sub-agent. If they are rejected again, leave them without `llm` (the report shows coverage x/N) and mention it.

## 4. Report

```
.venv/bin/python scripts/build_report.py --target <target> --since <A> --until <B>
```

Then launch the `report-writer` sub-agent with the report path and target.

## 5. Verify and hand back

- `grep -rl access_token data/ads reports` must print nothing.
- Reply with the report path, the `-ads.csv` path, the ad count, the LLM coverage, the coverage basis, and 3 headline takeaways. If a non-EU/UK country returned 0 ads, say that this is the API's EU/UK coverage limit, not an error.
