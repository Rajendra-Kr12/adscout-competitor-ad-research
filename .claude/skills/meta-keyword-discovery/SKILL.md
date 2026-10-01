---
name: meta-keyword-discovery
description: Find NEW competitor brands from Meta Ad Library keyword search, as an optional step after a multi-brand project. It ranks the phrases the tracked brands lead their ads with (by distinct message), searches them through the API guard (lean fields, hard call budget, monthly or weekly windows, stop on first warning), classifies every advertiser Page (brand owner and brand-led channel kept; retail promo, persona or fake-blog funnels, clinics and off-category dropped with a logged reason), proposes 6-8 candidates with a recommended 3-4, then STOPS for the user's pick. Use it whenever the user asks "who else advertises on these keywords", "find brands like these", "discover new competitors", "what other brands pop up for these words", "keyword discovery" or "/meta-keyword-discovery <project-slug>", even without naming the skill. Not for researching brands already chosen (use /research-brands) or one known brand (/analyze-competitor).
argument-hint: <project-slug> [--window month|week] [--budget N] [--fixture]
---

# /meta-keyword-discovery

**Use it** after `/research-brands` has run a project: it needs that project's normalized ads. The goal is a
shortlist of brands the user is not tracking yet, for a later deep dive.

**Do not use it** to research brands the user already named (`/research-brands`), for one known Page
(`/analyze-competitor`), outside the EU/UK (ads_archive has no commercial ads there), or for market share,
spend or reach.

Run it in the **main thread**: step 4 asks the user to choose. The project `AGENTS.md` overrides anything here.

## Read this first: rules and cautions

1. **Official Ad Library API only.** Never scrape facebook.com or the Ad Library website, and never open them with
   a browser tool. Cite ads only as `https://www.facebook.com/ads/library/?id=<library_id>`.
2. **Every call goes through the guard** (`scripts/discover_keywords.py`). Stay inside the call budget (default 25);
   the script refuses a plan that could exceed it. It stops at the first usage warning, error or guard stop.
   Relay the stop; never retry it and never raise caps.
3. **Results are newest first and capped.** One window over a quarter samples only its last days (round 1: 4 of 5
   keywords covered only 28-30 Sep). Always split by `--window month` (or `week` for 1-2 keywords).
4. **Exact phrases miss synonyms** ("dark circles" does not find "under-eye"). Pick terms as the market writes them;
   use `:unordered` for a word that needs context ("menopause skin:unordered").
5. **Lean fields only, no retries.** Heavy pages trigger Meta's code 1 "reduce the amount of data"; retrying the
   same request never helps. Discovery asks for Page, dates, headline and caption only (~300 ads per page).
6. **Never drop retailers or distributors wholesale.** A retailer, distributor or creator whose ads speak for one
   established brand is a **channel**: keep it and tag the brand. Brands selling via Amazon are brand owners.
   Drop only multi-brand price-and-promo retail.
7. **Persona and fake-blog funnels are the main noise:** person-named, "expert", "magazine" or "over 40" Pages
   pushing small DTC brands. They are dropped, with the brand they push logged.
8. **"Unverified" is the weakest judgement and usually the biggest bucket.** Tell the user to skim the
   **Dropped and why** table; every dropped Page is listed so they can overrule it.
9. **Classification reads headlines and link captions only** (no ad text in discovery). Say so in the report.
10. **Keyword results are a sample, not the market.** Never present counts as share, spend or reach.
11. **Always STOP for the user's brand pick** before any deep-dive fetch.
12. **A brand seen only via a creator or retailer** (round 1: Himalaya) needs its own Page looked up by name
    (`discover_pages.py --name "<Brand>"`, 1 call) before anyone concludes it does not advertise.

## Workflow

1. **Terms** (no calls): `scripts/keyword_terms.py --project <slug>`. Pick 5 specific, non-overlapping terms.
   Commands and flags: [references/commands.md](references/commands.md).
2. **Search** (through the guard): `scripts/discover_keywords.py ... --window month --dry-run`. Show the call
   ceiling, then run it without `--dry-run`.
3. **Classify** (no calls): `scripts/discovery_pages.py --name <slug> --project <slug> --tables-md ...`. The criteria
   and when to overrule them are in [references/classification.md](references/classification.md).
4. **Propose, then STOP:** write the report from [references/report-template.md](references/report-template.md),
   then ask the user to pick with `AskUserQuestion`.
5. **Hand off** the chosen Page IDs: `project.py new <slug>-discovery --brand "Name:GB@<id>"`, then
   `/research-brands <slug>-discovery`.

For calibration, see the worked example from the first run (keywords, the newest-first lesson, v1 vs v2
classification, Himalaya via a creator): [references/worked-example-round1.md](references/worked-example-round1.md).
Settings and criteria live in `config/discovery.yaml`; background is in README "Keyword discovery".
