---
name: export-handoff
description: Write a one-page research brief (handoff/<slug>.md) from the ads a project or brand already has, for a separate Instagram creative agent to build angles from instead of browsing the web. Brands, what they run, hooks and angles with Ad Library links, formats, coverage gaps, whitespace, each line tagged Observed / Pattern / Hypothesis / Unknown. Offline and instant, so it is safe for a live demo. Use for "/export-handoff <project-or-brand-slug>", "hand off to the creative agent", "give the Instagram agent the research". Not for fetching new ads (use /research-brands or /analyze-competitor first).
argument-hint: <project-slug | brand-key> [--fixture tests/fixtures/ads_archive_synthetic.json] [--dry-run]
---

# /export-handoff

Read the project `AGENTS.md` first; its rules override anything here.

**Read this first**

- This step makes no Meta call and never browses. It only reads stored data (`data/ads/<brand>/`, the project file, the latest run window). If there is no stored data, say so and point to `/research-brands` or `/analyze-competitor`; do not fetch from here.
- The output has no spend, CPM, CPA or ROAS, no tokens and no `ad_snapshot_url`. Evidence is `https://www.facebook.com/ads/library/?id=<library_id>` only. The script refuses to write anything else.
- A country with 0 ads outside the EU/UK is a coverage gap. The brief says so; do not soften it into "no ads".
- `handoff/*` is git-ignored: it can hold real competitor copy. Never commit it.

## Steps

1. Offline demo, or a project that was already run:
   - Demo: `.venv/bin/python scripts/export_handoff.py demo --fixture tests/fixtures/ads_archive_synthetic.json`
   - Real: `.venv/bin/python scripts/export_handoff.py <slug>` (add `--dry-run` first to see what it would read).
2. Read `handoff/<slug>.md` back to the user in chat (it is one page). Mention how many ads had the LLM extract step (angles/tone); if few, offer `/analyze-competitor` extraction first, because hooks then become real angles instead of first lines.
3. Do not add claims to the file. Observed/Pattern lines come from counts; Hypothesis lines are the only place for interpretation.

The format is documented in README "Handoff to the Instagram creative agent".
