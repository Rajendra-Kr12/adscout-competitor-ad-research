---
name: meta-keyword-discovery
description: Optional deep-dive step after a multi-brand project. Finds new competitors through Ad Library keyword search. It takes the most distinctive phrases the known brands lead their ads with (weighted by distinct copy cluster), searches them through the API guard with lean fields, monthly or weekly windows and a hard call budget, classifies every advertiser Page by explicit criteria (brand owner, brand-led channel kept; multi-brand retail, advertorial or fake-blog, clinic or B2B, off-category dropped with a logged reason), and proposes 6-8 candidates with a recommended 3-4. It then STOPS for the user's pick and hands the chosen Page IDs to the normal project pipeline. Use for "/meta-keyword-discovery <project-slug>", "find brands like these", or "who else advertises on these keywords".
argument-hint: <project-slug> [--window month|week] [--budget N] [--fixture]
---

# /meta-keyword-discovery (find new competitors by keyword)

Read the project `AGENTS.md` first; its rules override anything here. Run this skill in the **main thread**,
because step 4 asks the user to choose. Use `.venv/bin/python` for every script. Background: README
"Keyword discovery"; settings and exclusion lists live in `config/discovery.yaml`, not in code.

## Your obligations

- Every Meta call goes through `scripts/discover_keywords.py`, which uses the API guard. Never call the
  Ad Library another way, never open facebook.com, and never retry after a stop. On any `STOPPED` line or guard
  refusal, stop and relay it.
- Stay within the budget the user gave (default 25 ads_archive calls; the daily token check is extra). The
  script refuses a plan that could exceed it; reduce keywords or windows rather than raising the budget yourself.
- Keyword search returns a **sample**: the API serves the newest ads first and each window is capped. Say so
  wherever you present counts, and never present them as market share, spend or reach.
- Never pick the final brands. Propose; the user decides. Never present a Page ID as confirmed until the user
  confirms it.
- Discovery records have no ad text and no snapshot links. Keep it that way: cite ads only as
  `https://www.facebook.com/ads/library/?id=<library_id>`.

## 1. Keywords (no API calls)

The project must already have normalized ads (`run_project.py` has run):

```
.venv/bin/python scripts/keyword_terms.py --project <slug> --language en --out reports/<slug>/<date>/discovery/terms.json
```

Pick **5** search terms from the top of the list:
- skincare- or category-specific, not generic ("dark circles", "hyaluronic acid", not "without compromise");
- not overlapping each other ("dark circles" or "tired eyes", not both);
- preferably used by 2 or more brands.

A strong single word that only makes sense with context can be searched as unordered words
(`"menopause skin:unordered"`). Write each choice down with its cluster and brand counts.

## 2. Search (through the guard)

Dry-run first and show the user the call ceiling, then run it:

```
.venv/bin/python scripts/discover_keywords.py --name <slug> --keyword "dark circles" --keyword "aloe vera" ... \
    --country GB --since <YYYY-MM> --until <YYYY-MM> --window month --pages-per-window 1 --budget 25 --dry-run
.venv/bin/python scripts/discover_keywords.py ...same arguments without --dry-run...
```

- Use `--window month` with 1 page per window for a quarter (5 keywords x 3 months = 15 calls). Use `--window week`
  only for 1-2 keywords or a single month, because it multiplies the calls. One window over the whole range is
  biased: in the 2026-10-01 run, 4 of 5 keywords' samples all started in the last 3 days.
- Exit code 2 means the run stopped (an error, the budget, or a usage warning). The records so far are kept;
  relay the reason and do not rerun in the same hour without the user.

## 3. Classify advertisers (no API calls)

```
.venv/bin/python scripts/discovery_pages.py --name <slug> --project <slug> --top 30 \
    --out reports/<slug>/<date>/discovery/pages.json --tables-md reports/<slug>/<date>/discovery/tables.md
```

Every Page gets one class, by the first rule that applies. Thresholds and pattern lists are in
`config/discovery.yaml` `pages` (`--config` for a variant):

| Class | Criterion (scripted) | Outcome |
|---|---|---|
| known | Page ID already in the project or competitors.yaml | listed, not a candidate |
| drop:clinic | clinic, injectables or B2B patterns in the name, domain or a headline | dropped: a different audience |
| drop:off-category | non-skincare patterns, or a non-UK/EU domain or Page name | dropped |
| brand | Page name contains a `known_brands` entry (wherever it links, Amazon included) | **kept** |
| drop:advertorial | persona, expert, review, blog or interview patterns in the name or domain | dropped, pushed brand logged |
| brand | another Page of the brand it carries | **kept** |
| channel | brand share ≥ `brand_share_min` (0.5) for an **established** brand (known_brands, project, competitors.yaml) | **kept**, tagged with the brand |
| drop:advertorial | brand share ≥ 0.5 for a brand seen only in this sample (affiliate funnel) | dropped, brand logged |
| drop:multi-brand-retail | a Page named as a retailer or marketplace, no single brand | dropped, counted |
| brand | own-brand share ≥ 0.5, or the landing domain matches the Page name, or it links only to a marketplace and names no other brand | **kept** |
| drop:reseller-funnel | none of the above and promo share ≥ `promo_share_min` (0.5) | dropped |
| drop:unverified | none of the above | dropped |

"Brand share" is the share of a Page's ads (with a headline or caption) that name the same lexicon brand. A
caption that is the brand's own shop domain counts. The lexicon is `known_brands`, plus the project's and
competitors.yaml brands, plus brand-owner Page names from the sample (minus `lexicon_stopwords`).

Never silently drop a Page. The report must include the generated **Dropped and why** table (every dropped
Page, its reason, ads and carried brand) so the user can overrule it, and the **Channel versus brand
messaging** table (the same brand on its own Page vs other Pages). If you overrule a class, say which and why.
Then judge the kept Pages: prefer the same market (a `.uk` site or UK Page) and category, and treat
look-alike dropship shops with care even when the rules call them brand owners.

## 4. Propose, then STOP

Write `reports/<slug>/<date>/discovery/discovery.md` in plain English:
- the keywords and why each was chosen;
- the calls spent and any stop;
- a table of 6-8 candidates: name, class (brand, or channel with the brand it carries), Page ID, ads in sample,
  live, keywords matched, and one line on why each is relevant;
- your recommended 3-4 for a deep dive. They must not duplicate each other or the known brands, and should
  cover different angles;
- caveats: a sample, not the market; exact-phrase limits; UK/EU commercial ads only; unverified candidates;
  classification uses headlines and captions only;
- the generated tables from step 3 (candidates, channel versus brand, dropped and why), unedited.

Then ask the user to choose with `AskUserQuestion` (multi-select, recommended first). **Do not fetch anything
for the candidates before the user answers.**

## 5. Hand off the chosen brands

Add the chosen Pages to a project and run the normal pipeline:

```
.venv/bin/python scripts/project.py new <slug>-discovery --brand "Name:GB@<page_id>" ... [answers]   # or set-page on an existing project
```

Then follow `/research-brands <slug>-discovery` from its plan gate: the critic, `run_project.py`, extraction and
reports. That flow's budget and caveats apply from here on.
