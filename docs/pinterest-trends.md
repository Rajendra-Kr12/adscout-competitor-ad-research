# Pinterest Trends: prepared, not switched on

Status: **disabled**. `scripts/pinterest_trends.py` is a stub with no network code. Nothing in this project calls
Pinterest, stores Pinterest data, scrapes pinterest.com, drives it with a browser agent, or uses a third-party
Pinterest MCP. This page records what switching the official Trends API on later would take. Basis: the
2026-11-07 scout report (Pinterest developer pages, public OpenAPI spec, developer guidelines). Pinterest says
its limits are "subject to change without notice", so re-check every figure before relying on it.

Why Trends and not pin search: pin search over other people's pins (`GET /search/partner/pins`) is a closed beta,
and the Pinterest MCP server is a waitlist. Both are out. Trends are aggregated signals (what is rising), not a
photo library, which is the closest fit to the project's rules.

## Before anything is built (captain, in order)

1. **Read the Pinterest Developer and API Terms yourself** (https://developers.pinterest.com/terms/). The scout
   could not read them (the page renders client-side), so nobody has. Do this **before registering an app**.
2. Read the Developer Guidelines (https://policy.pinterest.com/en/developer-guidelines) and the Terms of Service
   update that takes effect 12 Nov 2026 (https://policy.pinterest.com/terms-of-service-preview).
3. Decide, in writing in `docs/decisions.md`, that the open questions below are acceptable. If any answer is
   "no", stop here.

## What switching it on needs

| Item | Detail | Status |
|---|---|---|
| App | A Pinterest developer app, registered by the captain under their own account. Pinterest grants **Trial** access after approval (rejections cite a weak privacy policy or a vague description) | Not started |
| Access tier | Trial reads are allowed. Standard needs a screen recording of the app completing an OAuth flow and "a few days" of review | Trial is enough to start |
| Token | OAuth 2.0 **user** token: a human logs in and authorises. No client-credentials access for the endpoints scouted. The token would go in its own store module like `unsplash_store.py` (`.env`, 0600, fingerprint only), never printed or logged | Does not exist |
| Scopes | Not confirmed. The scout report did not record which scope the Trends endpoints require. Read the `security` entry of each Trends operation in Pinterest's OpenAPI file (https://github.com/pinterest/api-description) and request the minimum, never write scopes | Open |
| Endpoints (scouted) | `GET /trends/keywords/{region}/top/{trend_type}`, `GET /trends/product_categories/trending`, `GET /trends/product_categories/details`, `GET /trends/topics/featured` | Unverified in use |
| Rate limits | Rate category `trends_read`. Trial: 1,000 requests per day across all calls. Standard: `trends_read` 60 per minute, 100 per second per user and app overall. "Subject to change without notice." A local limiter like `unsplash_refs.Limiter` (caps, spacing, cooldown, ledger) would sit in front, with caps well under these | Not built |
| New-app restriction | Apps registered on or after 14 Sep 2026 "may receive HTTP 403 `PINNER_DATA_ACCESS_DENIED`" for boards or Pins of non-business users. Trends are aggregates, but nobody has tested whether they are affected | Open |

## Open terms questions (answer before enabling)

1. **Storage.** The guidelines say "you may not store any information accessed through any Pinterest Materials
   including the API. Instead, call the API each time you need to access information." A handoff file or a
   report that quotes a trend is stored data. Is a one-line trend label in a local file "storing"? Until
   answered, Trends output must not be written to `handoff/`, `data/` or `reports/`.
2. **Competitor research.** Forbidden without written authorisation: "Attempting or claiming to provide platform
   insights, benchmarking, or competitor research features". This project is a competitor-ads agent. Does a
   trends lookup for a product category count? Ask Pinterest in writing if in doubt.
3. **Purpose.** "Don't use information from our API except to serve and evaluate the performance of ads on
   Pinterest." Using Trends to brief Instagram creative is not that. Does the Trends API carry its own,
   different permission? Unknown.
4. **AI use.** Pinterest Materials may not be used "to train, fine-tune, or otherwise improve or develop any
   artificial intelligence or machine learning models". An agent reading a trend at run time is not training,
   but the line is untested.
5. **Developer and API Terms.** Unread (see above). They may add or override any of the points here.
6. **ToS update on 12 Nov 2026.** Unread.
7. **Trial coverage.** Whether Trial apps can call the Trends endpoints at all, and for which regions
   (including GB and IN), is not documented in anything the scout read.

## Rules that stay when it is enabled

- Official API only. No scraping, no browser agent, no third-party Pinterest MCP or "API" proxies.
- Keep the credential in a store module; show a fingerprint only.
- A local limiter with caps and a ledger, no retries on 429, a cooldown after a limit.
- Hold results in memory for the run; store nothing until question 1 is answered.
- Tag any trend line in outputs **Observed** with its region and date, never as performance data, and never
  mix it with Meta ad evidence as if it were the same source.
- Add Pinterest to `docs/compliance.md` "Allowed" in the same change that enables it, and update `AGENTS.md`
  (its first hard rule says official Meta APIs only).

The stub's tests (`tests/test_pinterest_stub.py`) fail if the file gains a network import or stops raising.
