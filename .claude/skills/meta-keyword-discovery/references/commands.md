# Commands and flags

Use `.venv/bin/python` for every script. All paths are relative to the repo root. `<date>` is the run date
(YYYY-MM-DD) and `<slug>` is the project slug.

## Step 1: terms (no API calls)

```
.venv/bin/python scripts/keyword_terms.py --project <slug> --language en --out reports/<slug>/<date>/discovery/terms.json
```

| Flag | Meaning |
|---|---|
| `--project` / `--brand KEY` | the brands to read (`data/ads/<brand>/`; `run_project.py` must have run) |
| `--language en` | keep ads tagged with this language (`''` for all); search markets should match |
| `--min-clusters 3` | drop terms seen in fewer distinct copy clusters |
| `--top 40` | rows to print |

Pick **5** terms: category-specific, not overlapping ("dark circles" or "tired eyes", not both), and preferably
used by 2 or more brands. Search a word that needs context as unordered words: `"menopause skin:unordered"`.

## Step 2: search (through the guard)

```
.venv/bin/python scripts/discover_keywords.py --name <slug> --keyword "dark circles" --keyword "menopause skin:unordered" \
    --country GB --since 2026-07 --until 2026-09 --window month --pages-per-window 1 --budget 25 --dry-run
.venv/bin/python scripts/discover_keywords.py ...same arguments without --dry-run...
```

| Flag | Default | Meaning |
|---|---|---|
| `--keyword T[:unordered]` | exact phrase | repeat for each term |
| `--country` | GB | EU/UK only (ads_archive has commercial ads nowhere else) |
| `--window month\|week` | month | split each keyword's range so newest-first caps sample every period |
| `--pages-per-window` | 1 | page cap per keyword x window (~300 ads per page) |
| `--budget` | 25 | hard cap on ads_archive calls; a plan that could exceed it is refused before any call |
| `--limit` | 300 | page size (lean fields keep it light) |
| `--dry-run` / `--fixture F` | | print the plan / run offline |

Budget maths: keywords x windows x pages. A quarter by month is 5 x 3 x 1 = 15 calls. By week it is 5 x 14 x 1 = 70,
so use weeks only for 1-2 keywords or one month. The daily token check (at most 1 a day) is extra.

Exit codes: 0 ok; 2 stopped (an error, the budget or a usage warning; the records so far are kept); 3 the guard
refused the run (kill switch, cooldown, cap, lock). On 2 or 3, relay the message and do not rerun without the user.

Outputs: `data/discovery/<name>/<run>.jsonl` (lean records, 0600, 180-day retention), `<run>.summary.json`
(per keyword x window: ads, capped), and a ledger in `data/runs/`.

## Step 3: classify (no API calls)

```
.venv/bin/python scripts/discovery_pages.py --name <slug> --project <slug> --top 30 \
    --out reports/<slug>/<date>/discovery/pages.json --tables-md reports/<slug>/<date>/discovery/tables.md
```

`--run F` reads one records file instead of every run under the name. `--config F` uses other criteria, and
`--exclude-page ID` adds a known Page. `tables.md` holds the Candidates, Channel versus brand and Dropped and why
tables; paste it unedited into the report.

## Step 5: hand-off

```
.venv/bin/python scripts/project.py new <slug>-discovery --brand "Name:GB@<page_id>" ... [intake answers]
```

Then run `/research-brands <slug>-discovery` from its plan gate. For a brand seen only via a creator or retailer,
find its own Page first: `.venv/bin/python scripts/discover_pages.py --name "<Brand>" --country GB` (1 call).
