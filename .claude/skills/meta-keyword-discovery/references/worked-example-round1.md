# Worked example: round 1 (uk-natural-skincare-2026, 2026-10-01)

The first run of this workflow, before it became a skill. Read it to calibrate your judgement.

## Inputs and calls

- **Known brands:** UpCircle, Evolve, Spa Ceylon UK, Dr Organic, Conscious Skincare, Odylique (UK and France),
  Ayumi, Lucy Bee.
- **Terms, by distinct copy cluster:**
  - dark circles (13 clusters, 2 brands)
  - aloe vera (13, 2)
  - fine lines (9, 2)
  - hyaluronic acid (6, 2)
  - menopause skin, unordered ("menopause" in 10 clusters across 3 brands)
  - Rejected: "without compromise" and "say goodbye" (generic), and "tired eyes" (overlaps dark circles).
- **Search:** GB, Jul-Sep 2026, **one window over the whole quarter**, 4 pages x 300. 20 calls, no warnings.

## What went wrong, and what the skill now does

- **Newest-first caps.** Every keyword hit its 1,200-ad cap. For 4 of 5 keywords every sampled ad started on
  28-30 September; only "dark circles" (fewer results) spanned the quarter. Green People showed 1 ad and looked
  tiny, an artefact of the sample. **Now:** monthly or weekly windows with a page cap per window.
- **Blanket drops.** v1 dropped every Page that linked to Amazon or a retailer, and lost COSRX Global (20 live ads,
  4 of 5 keywords), Medicube and Sungboon Editor, which are brands selling via Amazon UK. **Now:** a class per
  Page (`classification.md`), with brand-led channels kept and every dropped Page listed with a reason.
- **Sleep hook.** The round 1 script stopped on *any* guard sleep. The guard also sleeps to space calls; it only
  survived because each 300-ad page took longer than the spacing interval. **Now:** `DiscoveryGuard` stops only on
  a usage warning.

## The result (v2 classification of the same sample)

- 5,455 ads from 695 Pages. Kept 246 brand owners and 3 channels. Dropped 445: 293 unverified, 62 advertorial,
  40 clinic, 30 off-category, 18 reseller funnel and 2 multi-brand retail. 1 known Page (Spa Ceylon UK).
- **Himalaya** appeared only through a creator Page ("Julie Celina", 1 ad naming Himalaya: a channel). Its own
  Page did not show up for these keywords in the sampled days. The right next step is a 1-call Page lookup by
  name (`discover_pages.py --name Himalaya`), not concluding that Himalaya does not advertise.
- **WelleCo** reached people through its own Page and its founder's Page (Elle Macpherson, a channel).
- **Persona funnels:** Prime Prometics ran 44 ads on its own Page and 41 across 6 persona Pages; Skincu ran 9 own and
  27 through 3. These are the channel-versus-brand stories worth showing.
- **Candidates proposed:** The Ayurveda Experience, COSRX Global, MooGoo Skin Care UK, Green People Organic, WelleCo,
  Dr.Melaxin UK and Faith In Nature, plus Himalaya on a watch-list. Recommended: The Ayurveda Experience, MooGoo,
  Green People and WelleCo (or COSRX Global as a big-brand benchmark). Then the run stopped for the captain's pick.

Files from that run: `reports/uk-natural-skincare-2026/2026-10-01/discovery/` (discovery.md for v1,
discovery-v2.md, pages-classified-v2.json).
