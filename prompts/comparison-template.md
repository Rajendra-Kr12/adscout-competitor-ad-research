# Comparison notes template (report-writer, comparison-v1)

`scripts/run_project.py` writes `comparison.md` for each project run: the side-by-side table, the LLM
messaging mix and each brand's coverage plan. You write **only** the block between
`<!-- report-writer:start -->` and `<!-- report-writer:end -->`. Rebuilding keeps that block.

Read first: `comparison.md`, the run's `index.md`, every brand's `report.md` (their Analyst notes, if
written) and `prompts/rubric.md`. Evidence: `data/ads/<brand>/*.json` for the brands in the run.

Use these sections, in this order. Keep the block under about 600 words.

```markdown
### Headline comparison
- 3-5 bullets, each a cross-brand claim with its evidence (a table figure or [library_id](permalink) per brand).

### Messaging and propositions
- How the brands position themselves differently; one example ad per brand.

### Offers and promo rhythm
- Who discounts, how deep, and when (month), from the tables and LLM offers.

### Format, length and channels
- Copy length, creative-only share, platforms, compared.

### Where each brand is open to attack
- 1-2 gaps per brand, each tied to an observation. Label inferences.

### Coverage and confidence
- Which brands have full detail (EU/UK, `ads_archive`) and which have none or discovery only (outside the
  EU/UK). LLM coverage per brand. Anything that limits the comparison.
```

## Rules

- Everything in `prompts/report-template.md` "Rules" applies: permalinks only, no new numbers, one short
  quote per ad at most, no spend/CPM/CPA/ROAS, SYNTHETIC DATA first when the run is synthetic.
- **Brands or countries outside the EU/UK have no fetched ads in this stage.** Do not describe their copy,
  CTA, reach, targeting, run length or visuals; say the data is not available and why (coverage plan).
- Compare like with like: if one brand has 40 ads and another 3, say so before comparing shares.
