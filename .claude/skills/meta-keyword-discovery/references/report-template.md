# Discovery report template

Write `reports/<slug>/<date>/discovery/discovery.md` in plain English, in this order. Keep the prose short; the
tables carry the detail.

```markdown
# Discovery: new <category> competitors from keyword search

Date, project, round number. One line: "No deep dive has started; you pick the brands."

## What I did
- Keywords: a table of term | search type | distinct copy clusters | brands using it, plus why each was chosen.
- Search: countries, window (month or week), pages per window, the calls used vs the budget, and any stop.
- Classification: how many Pages are kept as brand or channel and how many dropped, by class.

## Candidates (6-8)
| Candidate | Class (brand / channel: carries X) | Page ID | Ads in sample (live) | Keywords | Why it is relevant |

## Recommendation
Your 3-4 for a deep dive. They must not duplicate each other or the known brands, and should cover different
angles. Give one line each. Name a swap-in if there is a close call.

## Channel versus brand
One or two lines on what the table shows (who speaks for the brand: its own Page, retailers, creators or funnels).

## Caveats
- A sample, not the market (newest-first, capped per window); never share, spend or reach.
- Exact phrases miss synonyms.
- UK/EU commercial ads only.
- Classification reads headlines and captions only.
- "Unverified" is the weakest bucket: skim it.
- Name any candidate whose Page ID has not been confirmed.

## Tables
<paste tables.md from discovery_pages.py --tables-md, unedited: Candidates, Channel versus brand, Dropped and why>
```

Then ask the user with `AskUserQuestion`: a multi-select of the candidates, with your recommended ones first and
marked "(Recommended)". Offer "None yet, iterate with new keywords" as an option. **Do not fetch anything for the
candidates until the user answers.**
