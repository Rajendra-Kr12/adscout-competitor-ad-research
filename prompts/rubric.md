# Rubric: tone, quality, consistency, length (rubric v1)

The same rubric is used by `ad-extractor` (per ad) and `report-writer` (across ads). It
keeps judgements repeatable between runs. If you change it, bump `prompt_version` in
`prompts/extraction.md`.

## Quality scores (integers 1-5)

| Score | clarity: can a scroller get the point in one read? | specificity: concrete claims, numbers, products, proof | brand_consistency: matches the advertiser's dominant voice |
|---|---|---|---|
| 5 | One idea, stated in the first line, no jargon | Named product plus a measurable claim, price or proof ("glow in 14 days, from £45") | Indistinguishable from the brand's strongest ads |
| 4 | Clear, with minor clutter | Named product plus one concrete detail | Small deviations (for example one extra emoji) |
| 3 | Understandable after a second read | A product or benefit, but generic | Neutral; could be any brand in the category, or too little evidence |
| 2 | Several competing ideas, or a buried point | Vague benefit words only ("amazing results") | Clearly off-voice (for example a discount shout in a calm luxury brand) |
| 1 | Confusing or contradictory | No claim at all | Contradicts the brand's positioning |

## Tone labels

Pick 1-3, strongest first.

- `urgent` - time pressure, scarcity ("this weekend only")
- `confident` - assertive claims, results promised
- `warm` - friendly, inclusive, "we/you" care
- `playful` - humour, wordplay, casual emoji
- `educational` - explains how or why, ingredients, steps
- `aspirational` - lifestyle, self-image, "become"
- `luxurious` - premium cues, craftsmanship, gifting
- `clinical` - science, dermatology, data framing
- `authentic` - heritage, founders, "real", tradition
- `calm` - slow, soothing, ritual language
- `celebratory` - festivals, seasons, milestones
- `empathetic` - names a pain point with understanding
- `salesy` - discount-first, hard sell, exclamation-heavy

## Consistency (report level)

The report-writer judges consistency across the whole window:

- **Voice**: is the tone mix stable month to month? Which ads break it? Cite them.
- **Proposition**: does the core promise stay the same, or does it rotate by season or product?
- **Offer cadence**: how often discounts appear, and whether depth (% off) is creeping up.
- **Reuse**: near-duplicate copy (`derived.near_duplicate_of`) usually means a proven message was re-run.

## Length

Length is never judged by eye. Use the deterministic metrics (`derived.body_words`,
`body_chars`, `body_lines`, `variant_count`) and the report's "Length and format" table.
Describe them in terms of the brand's own median; don't apply an external "ideal length".
