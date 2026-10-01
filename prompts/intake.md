# Intake question bank (intake-v1)

Used by the `/research-brands` master (main thread only: sub-agents cannot ask the user). Source:
firstmate plan `fbcm-p1-plan` §1.2, updated with the 2026-09-30 Meta Ads MCP spike. The captain has not
yet approved these questions (C1), the folder layout (C3) or the outputs (C4), so **every answer is the
user's choice at run time**. The first option is the plan's recommended default and carries
"(Recommended)".

`AskUserQuestion` rules: at most 4 questions per call, 2-4 options each, a header of 12 characters or
less, and "Other" is added automatically (free text). Keep each option label short (about 5 words, plus
"(Recommended)" on the first) and put the rest of the wording below in the option's description. Ask in the rounds below and nothing else. Each
answer maps to one `scripts/project.py new` flag, so the whole intake is saved in one write.

## Round 0 - resolve brands (automatic, no questions)

1. Parse the user's list: names, Page URLs, Page IDs, and any country hints ("Kapiva (India)" means
   `Kapiva:IN`). Match names against `config/competitors.yaml` (key or name).
2. Brand spec per brand: `NAME_OR_KEY[:CC+CC][@PAGE_ID]`. Countries are ISO-2 (`UK` is read as `GB`).
3. Brands with no Page ID and at least one EU/UK market: run
   `.venv/bin/python scripts/discover_pages.py --name "<name>" --country <CC> [--country ...]`
   (1 call per brand, exact phrase). Use the brand's own countries, else GB. Never pick a Page yourself.
4. Brands whose countries are all outside the EU/UK are not discovered: the Meta Ads MCP route that
   could search them is not connected in this stage.

## Round A - "How should I set this up?" (1 question)

| Header | Question | Options |
|---|---|---|
| `Setup` | How should I set up this research? | **Use recommended defaults (Recommended)** - skips Rounds 2-4; you can edit any answer on the plan screen · **Walk me through each choice** |

## Round 1 - brand confirmation (dynamic; only brands discovery could not settle)

Ask when discovery found 0 or several Pages, or the top Page's name does not contain the brand name.
Up to 4 brands per call, one question each.

| Header | Question | Options |
|---|---|---|
| brand name, cut to 12 | Which Facebook Page is '<brand>'? | Up to 3 candidates as `<page name> - <page id> - <n> ads` (most ads first; `+` when the count is a lower bound) · **None of these, skip this brand** |

A pasted Page ID or URL arrives as "Other". Settle one obvious match (name contains every word of the
brand, and it has the most ads) without asking, but show it on the plan screen.

## Round 2 - scope (walk-through only)

| # | Header | Question | Options (first = default) | Flag |
|---|---|---|---|---|
| Q1 | `Markets` | Which delivery markets should I research? Any country works. Outside the EU/UK only headlines, start dates and links exist (Meta Ads MCP, not connected yet), and the official API returns 0 commercial ads there. | **UK only (GB) (Recommended)** · UK + big-5 EU (GB, DE, FR, ES, IT) · All EU + UK · (Other: any countries, e.g. "IN, US") | `--markets` (brand-specific countries stay in the brand spec) |
| Q2 | `Window` | How far back? | **Last 6 months (Recommended)** · Last 3 months · Last 12 months (API maximum for commercial ads) | `--months 6/3/12` |
| Q3 | `Goal` | What is the research for? | **Messaging + creative teardown (Recommended)** · Offer and promo calendar · Head-to-head comparison · All of these | `--goal messaging-creative / offers-calendar / head-to-head / all` |
| Q4 | `Folders` | How should the files be organised? | **Project folder > dated runs > one folder per brand (Recommended)** · Per-brand folders > dated runs · Flat, like single-brand reports | `--layout project/date/brand / brand/date / flat` |

"All EU + UK" = `AT,BE,BG,HR,CY,CZ,DK,EE,FI,FR,DE,GR,HU,IE,IT,LV,LT,LU,MT,NL,PL,PT,RO,SK,SI,ES,SE,GB`.
If the user names a folder outside the repo in "Other" for Q4, also pass `--output-root <absolute path>`
(only `reports` or a path outside the repo is accepted).

## Round 3 - outputs and visuals (walk-through only)

| # | Header | Question | Options | Flag |
|---|---|---|---|---|
| Q5 | `Outputs` | Where do you want the results? The markdown report is always written. (multi-select) | **Excel workbook + CSV on this Mac (Recommended)** · Local HTML dashboard (not built yet - saved for later) · Copy to Google Drive (not built yet - saved for later) · Markdown only | `--outputs markdown,xlsx,csv[,html][,drive]` |
| Q6 | `Visuals` | Do you want ad images and videos analysed later? Nothing is downloaded in this version; I'll save your choice. | **Yes: I'll save screenshots from a checklist you prepare (Recommended)** · No, links only | `--visuals inbox / none` |

Meta's Ads MCP returns no media (2026-09-30 spike), so the plan's "Ads MCP media" option is gone. Scripted
snapshot fetching is never offered: it needs the captain's written opt-in first.

Only if Q6 is not "No" (one more call):

| # | Header | Question | Options | Flag |
|---|---|---|---|---|
| Q7 | `Which ads` | Which ads should get visuals? | **Active in the last 90 days, max 30 per brand (Recommended)** · Last 30 days, max 15 · Last 180 days, max 50 | `--visuals-days 90 --visuals-max 30` (30/15, 180/50) |
| Q8 | `Video` | Video ads? | **Keyframes only, no audio (Recommended)** · Keyframes + local transcript (installs whisper.cpp) · Skip videos | `--video keyframes / transcript / skip` |

## Round 4 - monitoring (walk-through only; saved for a later stage, nothing is scheduled now)

| # | Header | Question | Options | Flag |
|---|---|---|---|---|
| Q9 | `Monitor` | Keep watching these brands once monitoring exists? | **Daily check + weekly digest (Recommended)** · Weekly only · Daily digest · No, one-off research | `--monitor daily+weekly-digest / weekly / daily-digest / none` |

Only if Q9 is not "No" (one more call):

| # | Header | Question | Options | Flag |
|---|---|---|---|---|
| Q10 | `Alerts on` | What should trigger an alert? (multi-select) | **New and stopped ads (Recommended)** · New offers or deeper discounts · Copy or creative changes · New platforms or markets, and reach jumps | `--watch new_ads,stopped_ads[,offers][,copy_changes,creative_changes][,platforms_markets,reach_jumps]` (default `new_ads,stopped_ads,offers`) |
| Q11 | `Notify` | How should I tell you? | **Digest file + macOS notification (Recommended)** · Digest file only · Also draft an email to me in Gmail | `--alerts digest,macos / digest / digest,macos,gmail-draft` |
| Q12 | `Time` | What time? | **08:00 (Recommended)** · 12:00 · 18:00 | `--monitor-time` |

## Round 5 - plan and critic confirmation (always)

Show `.venv/bin/python scripts/project.py plan <slug>` (brands, Page IDs, countries, routes, coverage per
brand, call estimate against the guard caps, saved preferences, retention), then the critic's verdict and
top findings with their reasoning. Ask the critic's `ask_user` questions first (at most 3, in one call),
apply the answers, and re-run the plan gate if anything changed. Then:

| Header | Question | Options |
|---|---|---|
| `Go?` | Ready to run? | **Run now (Recommended)** · Edit answers · Save the plan and run later · Cancel |

If the critic verdict is `block`, offer only **Edit answers** and **Cancel**.

## Re-runs

`/research-brands <slug>` for an existing `config/projects/<slug>.yaml` asks one question instead:
`Re-run` - "Re-run <title> with its saved settings?" - **Re-run with saved settings (Recommended)** ·
Change settings (back to Round A).
