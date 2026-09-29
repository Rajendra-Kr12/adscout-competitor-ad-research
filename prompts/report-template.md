# Analyst notes template (report-writer, template v1)

`scripts/build_report.py` writes every number, table and ledger. You write **only** the
block between `<!-- report-writer:start -->` and `<!-- report-writer:end -->`. Rebuilding
the report keeps that block.

Use these sections, in this order. Keep the whole block under about 700 words.

```markdown
### Headline takeaways
- 3-5 bullets. Each is a claim plus its evidence ([library_id](permalink) or a table figure).

### Messaging themes and propositions
- Group ads into 2-5 themes (for example "heritage", "seasonal glow", "gifting"). For each theme: what it promises, which months it ran, 1-3 example ads.

### Offer strategy over time
- Offer types and depth by month, whether discounts are rising, and which offers the long-running ads carry.

### Tone, length and consistency
- The dominant tone mix and the ads that break it (per `prompts/rubric.md`), median length against the brand's own range, and reuse of proven copy.

### Long-runners and what they share
- What the top long-running ads have in common. Label this an inference: run length is a proxy, not performance data.

### Gaps and ideas to test
- 3-5 concrete test ideas for us, each tied to an observed gap or pattern.

### Confidence and caveats
- Coverage basis (EU/UK-delivered only), LLM coverage (x/N ads), text-only analysis, and anything that looked off in the data.
```

## Rules

- Cite ads as `[library_id](https://www.facebook.com/ads/library/?id=library_id)`. Never use any other ad URL.
- Do not introduce numbers that are not in the report or cannot be counted from `data/ads/<target>/*.json`.
- Quote at most one short line per ad. Do not republish full ad copy.
- Say "no evidence" rather than guessing. Do not talk about competitor spend, CPM or ROAS: that data does not exist here.
- If the report says **SYNTHETIC DATA**, say so in the first bullet.
