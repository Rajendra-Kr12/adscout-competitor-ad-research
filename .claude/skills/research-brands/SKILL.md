---
name: research-brands
description: Master research agent for several brands at once (any countries). Resolves each brand's Facebook Page, asks the intake questions (markets, window, goal, folder layout, outputs, later-stage preferences) with a "use recommended defaults" fast path, runs the research-critic at the plan, data and report gates, runs the project through the API guard, extracts with ad-extractor, writes per-brand reports, a comparison and the workbook. Use for "/research-brands <brand, brand, ...>" or "/research-brands <project-slug>", or any request to research or compare 2+ brands' Meta ads.
argument-hint: <"Brand A, Brand B (India), Brand C" | project-slug> [--fixture]
---

# /research-brands (master research agent)

Read the project `AGENTS.md` first; its rules override anything here. This skill runs in the **main
thread** only, because sub-agents cannot ask the user questions. Use `.venv/bin/python` for every script.

Contracts: `prompts/intake.md` (the questions, options, defaults and the flag each answer maps to),
`prompts/critic.md` (the critic), `prompts/comparison-template.md`. Background: README "Multi-brand
projects".

## Your obligations

- Ask only through `AskUserQuestion`, in the rounds of `prompts/intake.md`. Never invent extra rounds.
- A critic **blocker** must be fixed or put to the user; you may **never** overrule a compliance blocker
  (scraping, a token or non-permalink link in an output, batch media, spend claims). A **major** finding
  must be fixed or explained in the report caveats.
- Never pick a Page for the user when discovery is ambiguous; never retry a guard stop; never run
  `guard.py unkill` or `clear-cooldown`; never connect or call the Meta Ads MCP (not in this stage).
- Say plainly, for every brand outside the EU/UK, that the official API has no commercial ads there and
  that only headlines, start dates and links will be possible once the Meta Ads MCP route is connected.

## 0. Preflight

- `.venv/bin/python scripts/guard.py status`. If the kill switch or a cooldown is on, stop and tell the
  user why (and when it ends).
- Slug: if the argument names an existing `config/projects/<slug>.yaml`, ask the single re-run question
  from `prompts/intake.md` "Re-runs"; on "Re-run with saved settings" go to step 3.
- Otherwise make a short slug from the user's words (e.g. `ayurveda-uk`) and today's date is the run date.
- Offline/demo (`--fixture`, or no token): add `--fixture tests/fixtures/ads_archive_synthetic.json` to
  discovery and run commands, and say every output is SYNTHETIC.

## 1. Intake

1. **Round 0** (no questions): build a brand spec per brand and run discovery for brands without a
   Page ID (1 call each), as `prompts/intake.md` describes. Tell the user how many calls that is first.
2. **Round A**: defaults or walk-through.
3. **Round 1**: confirm ambiguous Pages (candidates from discovery).
4. **Rounds 2-4** only on "Walk me through each choice".
5. Save everything in one write:
   `.venv/bin/python scripts/project.py new <slug> --brand "<spec>" ... [answer flags] [--force]`
   It validates the file (any ISO-2 country; `UK` -> `GB`) and derives each brand's coverage plan.

## 2. Gate A: plan

1. `.venv/bin/python scripts/critique.py check <slug> --stage plan` (prints the checks file path).
2. Launch the `research-critic` sub-agent with: stage `plan`, the project file path and the checks file
   path. It replies with one JSON object.
3. Write that JSON next to the checks file as `draft-critique-plan.json`, then
   `.venv/bin/python scripts/critique.py record <that file> <slug>`. If `record` refuses it (invalid, or a
   check blocker was dropped), send the critic the error once and record again.
4. **Round 5**: show `.venv/bin/python scripts/project.py plan <slug>` plus the verdict and top findings
   with their reasoning. Ask the critic's `ask_user` questions (at most 3, one call), apply the answers
   (`project.py new ... --force`, `set-page`, `set-countries`) and repeat this gate if anything changed.
   Then the final question (Run now / Edit answers / Save and run later / Cancel; only Edit or Cancel on
   `block`).

## 3. Run (deterministic)

```
.venv/bin/python scripts/run_project.py <slug> --dry-run     # show the exact requests and the call count
.venv/bin/python scripts/run_project.py <slug>               # through the guard
```

Exit 2 or 3 means a group stopped (API error or guard). Relay the message and the ledger path from
`index.md`'s "Fetch stopped" section; do not retry. Outputs are still written and say what is missing.

## 4. Gate B: data

Same as Gate A with `--stage data` (critic input: checks file, `run.json`, `index.md`). Fix or ask about
blockers before spending LLM effort.

## 5. Extract (sub-agents, parallel)

For each brand with ads: `.venv/bin/python scripts/llm_batches.py plan --target <brand-key> --size 20`.
Launch one `ad-extractor` per printed line, all in the same turn, then merge per brand with
`llm_batches.py merge --target <brand-key> <outputs>`. Re-run rejected batches once with the reasons.
Then rebuild the outputs without API calls: `.venv/bin/python scripts/run_project.py <slug> --no-fetch`.

## 6. Write

Launch `report-writer` for each brand report that has ads (path + brand key) and once in comparison mode
with the run's `comparison.md`, all in one turn.

## 7. Gate C: report

Same as Gate A with `--stage report` (critic input: checks file, `index.md`, `comparison.md`, every brand
report). Fix blockers by re-running `report-writer` on the affected file with the finding quoted; at most
two rounds, then put what remains to the user. Finish with `run_project.py <slug> --no-fetch` so the
workbook's Coverage sheet carries the final findings.

## 8. Hand back

- `grep -rl access_token reports data/ads` must print nothing.
- Reply with: the run folder (`reports/<slug>/latest`), `index.md`, `comparison.md`, the workbook, calls
  used, per-brand ad counts, each brand's coverage in one line (full detail / none: outside the EU/UK),
  the critic verdicts, and 3 headline takeaways.
