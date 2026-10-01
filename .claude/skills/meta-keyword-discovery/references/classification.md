# Advertiser classification criteria

Read this at step 3, before you present any candidate or drop any Page. The authoritative patterns and
thresholds are in `config/discovery.yaml` (`pages`); `scripts/discovery_pages.py` applies them. Run a variant
with `--config <file>`; never edit the code to change a criterion.

## The classes (the first rule that applies wins)

| # | Class | Criterion (scripted) | Outcome |
|---|---|---|---|
| 1 | known | The Page ID is already in the project or `config/competitors.yaml` (or `--exclude-page`) | listed, not a candidate |
| 2 | drop:clinic | `clinic_patterns` in the name, a landing domain or a headline (clinic, aesthetic, injectables, pharma, B2B) | dropped: a different audience |
| 3 | drop:off-category | `non_category_patterns` (name or domain), or `non_uk_patterns` (a .us/.ca/.com.au site, or a "US" Page) | dropped |
| 4 | brand | The Page name contains a `known_brands` entry, wherever it links (Amazon included) | **kept** |
| 5 | drop:advertorial | `persona_patterns` in the name or domain (expert, review, blog, journal, interview, digest...) | dropped; the brand it pushes is logged |
| 6 | brand | Another Page of the brand it carries (for example "<Brand> Online") | **kept** |
| 7 | channel | Brand share ≥ `brand_share_min` (0.5) for an **established** brand (`known_brands`, the project's brands or competitors.yaml), or a named retailer doing so | **kept**, tagged with the brand it carries |
| 8 | drop:advertorial | Brand share ≥ 0.5 for a brand seen only in this sample (affiliate or persona funnel) | dropped; the brand is logged |
| 9 | drop:multi-brand-retail | A Page named as a retailer or marketplace (`marketplace_patterns`) with no single brand in its ads | dropped, counted |
| 10 | brand | Own-brand share ≥ 0.5 (most ads name the Page's own brand) | **kept** |
| 11 | brand | Links only to a marketplace and names no other brand (a brand selling there; unverified) | **kept** |
| 12 | brand | The landing domain matches the Page name | **kept** |
| 13 | drop:reseller-funnel | None of the above, and promo share ≥ `promo_share_min` (0.5) | dropped |
| 14 | drop:unverified | None of the above | dropped |

## Measures

- **Brand share:** of a Page's ads that have a headline or link caption, the share that name the same lexicon brand.
  A caption that is a brand's own shop domain counts as naming it.
- **Own-brand share:** the same, for the Page's own name (without noise words such as UK, official, shop, skincare).
- **Promo share:** the share of those ads matching `promo_patterns` (% off, sale, deal, free delivery, buy 1...).
- **Lexicon:** `known_brands`, plus the project's brands and competitors.yaml, plus names of brand-owner Pages in
  this sample at least `lexicon_min_chars` long. `lexicon_stopwords` (reviews, deals, health...) are never learned.

## Why these criteria (from the 2026-10-01 round 1 review)

- **Retailers and distributors are not dropped wholesale.** A big brand such as Himalaya sells through its own
  shops, stores, retailers and aggregators. When a retailer's ads speak for one brand, the messaging is the
  brand's. That is a *channel*, kept and tagged. Only multi-brand price-and-promo retail is dropped.
- **Brands selling via Amazon are brand owners.** COSRX Global, Medicube and Sungboon Editor link only to Amazon UK.
  v1 dropped them as "retailer"; v2 keeps them.
- **Persona and affiliate funnels are the main noise.** Person-named, "expert", "magazine" or "over 40" Pages push
  small DTC brands (for example Skincu: 9 ads on its own Page, 27 through 3 persona Pages). They are dropped, but
  the brand they push is logged, and the channel-versus-brand table keeps the pattern visible.
- **"Unverified" is the weakest judgement.** It means the landing domain is unlike the Page name and no brand or
  promo signal was found. It is usually the largest bucket (293 of 445 dropped in round 1), so tell the user to
  skim it in the Dropped and why table.

## Overruling

You may overrule a class with a stated reason (for example a real brand whose shop domain differs from its Page
name). Say which Page and why in the report. Never delete a dropped Page from the tables.
