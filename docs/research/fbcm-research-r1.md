> **Erratum (2026-09-29, added when copied into this repo):** sections 3 (row 3) and 7.2 (step 4.2) say to create a **Business**-type app. In practice the Business-type app could not issue user tokens in Graph API Explorer, while a **Consumer**-type app ("FBCM Ad Library Test") can. Follow the corrected steps in `README.md`. The rest of the report is copied verbatim from `/Users/raj/firstmate/data/fbcm-research-r1/report.md`.

# Facebook CM Agent - feasibility research + PM plan

Scout task `fbcm-research-r1`. Researched 2026-09-29. Every URL below was accessed on 2026-09-29 unless noted.
Labels: **[official]** = Meta or Anthropic primary docs, terms, or blog. **[observed]** = what I saw myself with an unauthenticated call. **[third-party]** = blogs or vendors, used only as leads. **[inference]** = my reasoning, not stated by a source.
No credentials were available, so nothing was called with a token. Nothing was written to the captain's project folder.

---

## Executive summary

**Verdict: buildable, but not as an app *inside* facebook.com, and competitor data is thinner than the brief hopes.**

1. **You cannot host a non-game business tool inside Facebook in 2026.**
   - Canvas / Facebook Web Games shuts down **2026-09-30, which is tomorrow**. It was games-only anyway.
   - Instant Games are games-only and run network-restricted.
   - Custom Page tabs can no longer be created through the API.
   - Meta Business Extension is a setup popup that lives on a partner's website.

   The closest real options:
   - **(a) Meta's own hosted Ads MCP server** (`https://mcp.facebook.com/ads`). Open beta since 2026-04-29. Meta hosts the tools and Claude Code is an officially supported client. It includes an `ads_library_search` tool that Meta describes as "for competitive research and creative inspiration".
   - **(b) A Messenger bot on the captain's own Page.** The conversation happens inside Facebook/Messenger. The logic runs on your machine.
   - **(c) Meta AI Connectors.** Developer-preview waitlist announced 2026-09-24. It puts a service inside Meta AI chat, but not yet inside the Facebook app.
2. **Competitor ad content is available officially, with one big regional catch.**
   - The Ad Library API returns ad text, link titles/captions, delivery dates, platforms, a snapshot link, and the library ID.
   - For ordinary commercial ads, it only returns **ads delivered to the EU/UK in the past year**. Political/issue ads are covered worldwide for 7 years.
   - The only competitor numbers are for EU-delivered ads: reach, age/gender/country reach breakdown, and targeting. Spend and impression ranges exist **only for political ads**.
   - **Competitor CPM, CPA, CPL, ROAS, ROI, likes, and comments are not available from any official Meta source.**
3. **The biggest open question** is whether the Ads MCP `ads_library_search` tool also covers non-EU commercial ads, as the public Ad Library website does.
   - It is undocumented, and no public source answers it.
   - A 10-minute test with the captain's login settles it.
   - It decides whether India-only competitors can be analysed at all. The project's first commit is timestamped +0530, so Indian competitors are a likely target.
4. **Creative media: per-ad download is permitted, bulk download is not.** The API returns no image or video files. `ad_snapshot_url` renders the ad with uncompressed media. Meta says [official]: "While you cannot currently download a batch of archived ads, you can download ad creative such as images and text for an individual ad. If you do so, it must be for analysis and you must comply with the data storage terms." Claude accepts images but **not video**, so video ads need frames plus a transcript.
5. **Own-account performance (2e) is fully available and now easy.** The Marketing API Insights endpoint, the Ads MCP server, and the Ads CLI give reach, CPM, CPC, CTR, cost per result, ROAS, and video retention. For your own ad accounts, no App Review is needed.
6. **Access cost:** the Meta APIs are free.
   - The one slow step is **identity confirmation at facebook.com/ID**, which takes "a few days". Start it now.
   - No App Review and no business verification are needed while only the captain, or people with a role on the app, use it.
   - The only running cost is Claude usage: roughly **USD 2-4 per 200-ad competitor report** (estimate, section 4).
7. **Recommended MVP (Phase 0):** input a Page ID and a date range. The agent pulls every ad through `ads_archive`, groups them by month with permalinks and text, has Claude extract hook, offer, CTA, tone, and length, and writes a markdown report. **Build effort: about 1-2 days** once the token exists. Run everything locally in Claude Code. Add the "inside Facebook" front-end later, and only if it earns its cost.

**Decisions for the captain** (details in section 8):
- D1: hosting surface. Recommend local Claude Code plus the Meta Ads MCP now, Messenger later.
- D2: which two competitors to test, one advertising in the EU/UK and one India-only.
- D3: whether to use your own ad account for 2e. Recommend yes, read-only, in Phase 3.
- D4: Claude billing route.
- D5: personal research versus client/product scope.
- D6: creative-download policy.

**Captain action now:** start facebook.com/ID confirmation.

---

## What I did (method and evidence)

- Read Meta primary docs in a headless browser (`chrome-devtools-axi`, session `fbcm-r1`) and through Meta's `.md` doc endpoints (`curl <doc-url>.md`), plus WebFetch/WebSearch for discovery. Pages read:
  - Ad Library API landing page, FAQ, and release notes
  - `ads_archive` and `ArchivedAd` reference
  - Marketing API authorization, rate limits, insights, and changelog
  - Graph API changelog and rate limits
  - Ads AI Connectors (MCP server and CLI)
  - Web/Instant Games announcement
  - Page tabs reference
  - Messenger Platform (overview, webview, App Review)
  - Meta Business Extension
  - Create-an-app, other app types, and long-lived tokens
  - Page Public Content Access
  - Meta Terms of Service, Platform Terms, and Developer Policies
  - Transparency Center Ad Library tools
  - Meta Connect 2026 recap and Meta AI Connectors
  - Anthropic vision docs and the model/pricing reference
- Unauthenticated probes (no credentials were used):
  ```
  $ curl -sS -g "https://graph.facebook.com/v26.0/ads_archive?ad_reached_countries=['IN']&fields=id,ad_snapshot_url"
  {"error":{"message":"An unknown error has occurred.","type":"OAuthException","code":1,...}}   HTTP 500
  ```
  ```
  $ curl -i -X POST https://mcp.facebook.com/ads --data-raw '{"jsonrpc":"2.0","method":"tools/list","id":1}'
  HTTP/2 401
  www-authenticate: Bearer resource_metadata="https://mcp.facebook.com/.well-known/oauth-protected-resource/ads",
    scope="ads_management ads_read catalog_management business_management pages_show_list pages_manage_ads instagram_basic ads_mcp_management"
  $ curl https://mcp.facebook.com/.well-known/oauth-protected-resource/ads
  {"resource":"https://mcp.facebook.com/ads","authorization_servers":["https://www.facebook.com/ads"],"scopes_supported":[...same 8 scopes...],"bearer_methods_supported":["header"]}
  ```
  So the Meta MCP server is live, uses standard MCP OAuth discovery, and needs a Facebook login before it will list its tools. I could not list `ads_library_search`'s parameters without credentials.
- The project repo (`facebook-cm-agent`, commit `736d2f6`) is empty apart from firstmate hooks in `.claude/settings.local.json`. There is no code to reconcile with.

---

# Part 1 - Researcher (feasibility)

## 1. What "an app hosted on Facebook" can mean today

| Surface | Status (2026-09-29) | Allowed for a non-game business tool? | What the user would experience | Verdict |
|---|---|---|---|---|
| **Canvas / Facebook Web Games** | **Sunsets 2026-09-30 (Pacific).** "Web Games will sunset by September 30, 2026" [official: games blog] | No. Games only, and the product is ending | n/a | Dead |
| **Instant Games** | Live. New games must use "Zero Permissions" [official] | No. Games only. Policies forbid collecting user data, and Meta "may require you to load your Game in a network-restricted environment that will limit the Game's access to specific APIs and block in-game features that require callbacks to your servers" [official: Dev Policies §4] | n/a | Not viable |
| **Page tabs (custom app tabs)** | `/page/tabs` is read-only: "Creating: You can't perform this operation on this endpoint." The old `/docs/pages/tabs/` guide now redirects to the generic Pages API overview [official, observed] | No, a tab can't be installed | n/a | Dead |
| **Meta Business Extension (MBE)** | Live. "A popup-based solution that allows users to easily set up the Meta Pixel, Catalog, and Shops" on **a partner's platform** [official] | Only for commerce platforms onboarding merchants | A popup on someone else's site | Not a host |
| **Business Suite / Business Manager "apps"** | No third-party app hosting. The only AI-related item is **Settings > Integrations > Ads MCP server**, which holds rules that limit what agents may do [official: help 1456422242197840] | No | n/a | Not a host, but the rules are useful (see §5) |
| **Messenger Platform (bot on your Page)** | Live and free. "The Messenger Platform is free to use." Standard Access "limits data to users with a Role on your app or Page", so no App Review is needed for captain-only use. A **webview** can open a web page inside Messenger [official] | Yes. Business messaging is the intended use | You DM your Page ("analyse Brand X, last 6 months") and get a summary plus a button that opens the full report inside Messenger | **Closest true in-Facebook UX.** Phase 4 option |
| **WhatsApp Business Platform** | Live | Yes | Same idea, on WhatsApp | Possible, but not "Facebook" |
| **Meta AI Connectors** | **Developer preview, waitlist**, announced at Connect 2026-09-24: "Put your service inside the Meta AI conversation... connect your service through an API or MCP server." The product page says it runs on "AI glasses and Meta AI app and web today" [official] | Yes, but it needs an approved application with company details and "a live REST API" | You ask Meta AI and it calls your tool | Watch. Phase 5, only if accepted |
| **Meta-hosted Ads MCP server** | **Open beta since 2026-04-29.** Own-developer-app support and agent rules added 2026-07-16 [official] | Yes. Built for advertisers' AI agents. Claude Code is a listed supported client | You chat in Claude Code. Meta executes tools such as `ads_library_search` and `ads_get_ad_entities` on its servers | **Use now.** This is the most "Meta-hosted" piece available |
| **Meta developer app + Facebook Login, UI hosted elsewhere** | Standard and fully supported | Yes. This is what every ads tool does | A normal local or web app | **The foundation** |

**Verdict and honest tradeoff.** No supported surface renders a third-party business tool's UI inside facebook.com any more.

- **Meta-hosted parts you can use:**
  - the official **Ads MCP server** for own-account work and Meta's own Ad Library search tool;
  - **Messenger**, if you want to talk to the agent inside Facebook.
- **What you give up by building the "brain" off-Facebook:** nothing in data access. Every official data source (Ad Library API, Marketing API, MCP) is reachable from a local agent.
- **What you gain:** local files, full control over prompts and outputs, and no public server until you want one. That matches the captain's "local-only for now".
- **What a Messenger front-end costs:**
  - a public HTTPS webhook, which breaks "local-only" unless you run a tunnel;
  - asynchronous replies, because the "webhook must return 200 OK within 20 seconds" while an analysis takes minutes;
  - staying inside Messenger's 24-hour reply window.

## 2. Data availability matrix (captain items 2a-2e)

How to read the Source column:
- **API** = directly from an official endpoint.
- **Own** = only for ad accounts the captain owns or manages.
- **Derived** = the agent computes it.
- **None** = not available through official means.

### 2a. Ads for each month, and their URLs

| Need | Source | Details |
|---|---|---|
| All ads for a Page | API: `GET /v26.0/ads_archive` | `search_page_ids` takes up to 10 Page IDs. `ad_reached_countries` is required. Set `ad_active_status=ALL` explicitly, because the reference says the default is ACTIVE while the 2021 release notes say it became ALL. `limit` can be up to 5,000 per page ("each page of results can return a maximum of 5,000 ads"); follow `paging.next` |
| Month bucketing | API filters plus Derived | `ad_delivery_date_min` / `ad_delivery_date_max` (YYYY-mm-dd). Fields: `ad_creation_time`, `ad_delivery_start_time`, `ad_delivery_stop_time`. "As of August 2020, ad start dates are generated when an ad first delivers an impression" [official] |
| Unique ID | API | `id` = the Library ID ("Each ad uses a library ID as a unique identifier") |
| URL | API plus Derived | `ad_snapshot_url` renders the ad, but it **embeds your access token** (`.../render_ad/?id=...&access_token=...`), so never publish it. For reports, use the public Ad Library permalink `https://www.facebook.com/ads/library/?id=<library_id>` [inference: the Ad Library's share-link format, not an API field] |
| **Coverage** | API | "Ads about social issues, election or politics that were delivered anywhere in the world during the past seven years" **and** "Ads of any type that were delivered to the United Kingdom (UK) or European Union during the past year" [official]. **A commercial ad that never reached the EU/UK is not returned** |
| Coverage for non-EU commercial ads | Unknown: MCP `ads_library_search` | Meta describes it as "Search the public Meta Ad Library for competitive research and creative inspiration" [official]. Whether it returns what the website shows (all *active* ads in every country) or only the API's EU/UK scope is **undocumented**. **Test this first** (MVP step 0) |

Doc inconsistencies to test (all [official]):
- The API landing page says **UK or EU**.
- The `ad_reached_countries` note says "Ads that did not reach any location in the EU will only return if they are about social issues...", which does not mention UK-only ads.
- The Transparency Center says "EU and associated territories".

### 2b. Sub-elements and metrics of each ad

| Element | Source | Details |
|---|---|---|
| Primary text, per card or variant | API: `ad_creative_bodies` | "A list of the text which displays in each unique ad card". Gives card-level text for carousels and multi-version ads |
| Headline / link description / caption (CTA area) | API | `ad_creative_link_titles`, `ad_creative_link_descriptions`, `ad_creative_link_captions`. The caption usually shows the landing domain |
| Languages, placements | API | `languages`, `publisher_platforms` (FACEBOOK, INSTAGRAM, AUDIENCE_NETWORK, MESSENGER, WHATSAPP, OCULUS, THREADS, STREAMING_SERVICES) |
| Media type | API filter only | `media_type` = IMAGE, MEME, VIDEO, NONE, or ALL. It is a search filter, not a returned field, so you get the type by querying per type |
| Images, video, carousel visuals | Rendered via `ad_snapshot_url`, plus per-ad download (see below) | No image or video URL fields exist in `ArchivedAd` |
| Text on images and carousel cards | Derived | Claude vision reads text in images |
| Hook (first line or first 3 s), highlights in video, storyline | Derived | From the text, video keyframes, and an audio transcript |
| Offer (discount, trial, bundle, price) | Derived | Claude extracts it from the text and visuals |
| **Reach / impressions / demographics (EU/UK-delivered ads)** | API | EU only: `eu_total_reach`, `beneficiary_payers` (payer/beneficiary). UK and EU: `age_country_gender_reach_breakdown`, `target_ages`, `target_gender`, `target_locations`. EU, BR, and UK: `total_reach_by_location` (added 2025-07-01) |
| Spend / impressions ranges | API, **political/issue ads only** | `spend` (ranges from <100 to >1M), `impressions`, `demographic_distribution`, `delivery_by_region`, `estimated_audience_size`, `bylines`, `currency` |
| Likes, comments, shares on competitor ads | **None** in the Ad Library | Partial proxy: the **Page Public Content Access** feature ("allowed usage: analyze and/or display posts and engagement on Pages") reads public posts and engagement on competitor Pages. That covers boosted organic posts, not ad-only creatives. **It requires App Review** [official] |
| Performance proxies | Derived | Days running (long-running ads are usually winners, [inference]), number of near-duplicate variants, refresh cadence, platform spread |

**Can creative media be retrieved officially?** Yes, but only per ad and only for analysis.

- `ad_snapshot_url` "displays uncompressed images and videos from the ad". The same passage continues [official, Ad Library API page]: "While you cannot currently download a batch of archived ads, you can download ad creative such as images and text for an individual ad. If you do so, it must be for analysis and you must comply with the data storage terms in our Terms of Service."
- **Compliant pattern:** fetch creative on demand for the specific ads being analysed, keep it only as long as needed (Platform Terms §3.d), and never mirror a competitor's whole library in bulk.
- **Brittleness:** extracting media from the snapshot page means parsing Meta's HTML. That is a fragile integration and, done in bulk, drifts toward "automated collection" (§5). Treat it as a per-ad fetch with the captain's token, rate-limited and logged.

### 2c. Analysis (tone, quality, consistency, length)

This is all **Derived** from 2a/2b: Claude plus a few deterministic scripts.
- **Length** metrics are computed deterministically: characters, words, and lines per body; the number of variants.
- **Tone, quality, and consistency** are judged by Claude against a rubric. The rubric keeps the judgement repeatable.
- **Consistency** means consistency against a brand-voice profile learned from the competitor's own ads.

No Meta data gap here.

### 2d. Advanced similarity (ads, propositions, storylines, clips)

This is all **Derived**:
- **Text and proposition similarity:** embeddings plus clustering. Claude has no embeddings endpoint, so use a local embedding model or another embeddings API [inference].
- **Storyline similarity:** Claude-judged pairwise or cluster labels.
- **Image and creative reuse:** perceptual hashes.
- **Clip similarity:** keyframe hashes and transcript overlap.
- **Quantitative weighting:** only EU reach is available, and only for EU-delivered ads.

### 2e. Campaign, ad set, and ad performance

| Metric | Competitors | Own ad account |
|---|---|---|
| Reach, impressions, frequency | **None** (EU reach only, per 2b) | API: Insights `reach`, `impressions`, `frequency` ("This metric is estimated") |
| Spend, CPM, CPC, CTR, CPP | **None** | API: `spend`, `cpm`, `cpc`, `ctr`, `cpp`, `inline_link_click_ctr`, `outbound_clicks_ctr` |
| CPA, CPL | **None** | API: `cost_per_action_type`. CPL = the entry for the lead action type |
| ROAS | **None** | API: `purchase_roas`, `website_purchase_roas`, plus `action_values` / `conversion_values` |
| ROI | **None** | Derived: ROAS combined with margin or COGS the captain supplies |
| Video retention | **None** | API: `video_p25/p50/p75/p95/p100_watched_actions`, `video_avg_time_watched_actions`, `cost_per_thruplay`, and more |
| Creative-to-performance | **None** | API breakdowns `body_asset`, `title_asset`, `image_asset`, `video_asset`, `call_to_action_asset`, and more (dynamic/flexible creative), plus `publisher_platform` and `platform_position` |
| Benchmarks against similar advertisers | Only as an anonymous benchmark | MCP tools `ads_insights_industry_benchmark` ("compare ad set performance against benchmarks from similar advertisers"), `ads_insights_auction_ranking_benchmarks`, `ads_insights_performance_trend`, `ads_insights_anomaly_signal`, `ads_get_opportunity_score` [official]. **This is the only official "how do we compare" performance signal** |

Levels: account, campaign, ad set, and ad (`/{id}/insights`). Default attribution is `["7d_click","1d_view"]`. Async jobs are available for large pulls. Permission: `ads_read` [official].

## 3. Access requirements

| # | Requirement | Needed for | Time | Cost | Source |
|---|---|---|---|---|---|
| 1 | **Confirm identity and location at facebook.com/ID** ("the confirmation process required to run ads about social issues, elections or politics") | Ad Library API | "It can take a few days" | Free | [official] Ad Library API page |
| 2 | Meta for Developers account (agree to the Platform Policy) | Everything | Minutes | Free | [official] |
| 3 | A Meta app, of **Business type** ("consumer type apps can't generate access tokens") | Ad Library API; own-app MCP; Marketing API | Minutes | Free | [official] other-app-types |
| 4 | **App role:** "Users need to have a role or be assigned a role by the app owner to make queries" (since 2021-04-23) | Ad Library API | n/a | Free | [official] release notes |
| 5 | **User access token** of an ID-confirmed user. Graph API Explorer gives a short-lived token; exchange it with `oauth/access_token?grant_type=fb_exchange_token` for about 60 days | Ad Library API | Minutes | Free | [official] FAQ and long-lived-token doc |
| 6 | Permissions: **none beyond the default for `ads_archive`**. For own-account reporting: `ads_read`; add `ads_management` only for writes. MCP scopes: `ads_mcp_management ads_read ads_management catalog_management business_management pages_show_list instagram_basic` (plus `pages_manage_ads` in the server's advertised scopes) | Per feature | n/a | Free | [official] MCP get-started; [observed] 401 header |
| 7 | **App Review: not needed** while only people with a role on the app or business use it. "It is required if your app will be used by people without a role on your app or a role on the business that is connected to your app." For own ad accounts: "standard access to the `ads_read` and `ads_management` permissions are sufficient" | Later only (productizing, PPCA, public Messenger bot) | Weeks | Free | [official] create-an-app; MAPI authorization |
| 8 | **Business verification: not needed for the MVP.** Required for advanced access to data you don't own or manage | Later only | Days to weeks | Free | [official] |
| 9 | Marketing API Access Tier: **Limited** (default; "heavily rate-limited... for development only") or **Full** (needs 500+ calls in 15 days and under 15% errors). Renamed 2026-05-04; the screen recording is no longer required | Own-account scale | n/a | Free | [official] MAPI changelog |
| 10 | Rate limits | See below | | | [official] |

Rate limits [official]:
- **Ad Library API:** normal Graph "Platform Rate Limits", calls within one hour = `200 * Number of Users` (app-level). No separate Ad Library limit is documented.
- **Insights (per ad account per hour):**
  - Limited tier: `600 + 400 * active ads - 0.001 * user errors`
  - Full tier: `190000 + 400 * active ads - ...`
- **`ads_management`:** `300` (dev) or `100000` (full), `+ 40 * active ads`.
- **Non-use downgrade:** "Standard and Advanced Ads API access may be downgraded to Development access after 30 days of non-use" (Dev Policies §10).

**API versions** [official]:
- Graph/Marketing API **v26.0** (released 2026-07-29) is the latest. Pin to v26.0.
- v25.0 runs until 2028-07-29.
- **v24.0 is retired on 2026-10-06.**
- Stale docs, flag and ignore: the Ad Library API page's sample response still shows `v3.1` in its paging URL; the Insights reference version picker stops at v25.0.

**Costs:** Meta charges nothing for the Ad Library API, Marketing API, MCP server, or Messenger ("free to use"). Claude usage is covered in §4.

## 4. Agent architecture (proportional)

```
             Captain (terminal: Claude Code, in "Facebook CM Agent/")
                              │  /analyze-competitor <page_id> 2025-10..2026-09
                              ▼
   ┌──────────────────── Orchestrator (Claude Code session, Opus 5.5) ─────────────────────┐
   │ reads CLAUDE.md rules + prompts/ ; plans the run ; never scrapes                       │
   │                                                                                       │
   │ 1 fetch   → scripts/fetch_ads.py ──HTTPS──► graph.facebook.com/v26.0/ads_archive      │
   │             (deterministic, paged, month windows) → data/raw/*.jsonl                  │
   │ 1b (opt)  → MCP tool ads_library_search ──► mcp.facebook.com/ads  (Meta-hosted)       │
   │ 2 normalize + deterministic metrics → data/ads/<page>/<library_id>.json               │
   │ 3 fan-out: N× subagent "ad-extractor" (Sonnet 5.5), ~20 ads each, in parallel          │
   │            → hook/offer/CTA/tone/quality JSON per ad (schema-validated)               │
   │   (Phase 1: "creative-analyst" subagent: per-ad media → images/frames → vision)       │
   │ 4 aggregate by month + cross-ad analysis (Opus 5.5)                                   │
   │ 5 "report-writer" subagent → reports/<page>/<date>-competitor-report.md               │
   └───────────────────────────────────────────────────────────────────────────────────────┘
   Phase 3: same Claude Code session + Meta Ads MCP (own account, read-only rules) for 2e
   Phase 4: Messenger webhook (tunnel/cloud fn) → enqueue job → same pipeline → reply + webview link
```

- **Where it runs:** everything runs on the captain's Mac. The "Meta app" is just an identity, a token, and permissions. Meta hosts only the APIs and the MCP server. Nothing needs a public endpoint until the Phase 4 Messenger front-end.
- **Orchestrator choice:**
  - **Claude Code with project subagents** (`.claude/agents/*.md`) is the lowest-effort way to get parallel per-ad work. It is also where Meta's MCP already plugs in (`claude mcp add --transport http ... https://mcp.facebook.com/ads`).
  - For unattended or scheduled runs later (weekly monitoring), wrap the same prompts in a headless `claude -p` call or the **Claude Agent SDK**.
  - For bulk per-ad extraction that nobody waits on, the **Message Batches API** runs at **50% off**.
- **Pi agents:** Pi (`earendil-works/pi`, formerly `badlogic/pi-mono`) is a multi-provider agent toolkit.
  - Its core "deliberately ships without subagents". A subagent tool comes as an opt-in example extension (single, parallel, or chain modes), and it has an RPC mode [third-party: repo docs via search].
  - Use it only if the captain wants to mix non-Claude models, for example a video-native model for clip analysis. Any provider that receives Platform Data must meet the Service Provider clause (§5).
- **Models and pricing** [official, Anthropic model table cached 2026-09-25]:
  - `claude-opus-5-5`: USD 4 in / 20 out per million tokens (orchestration and synthesis).
  - `claude-sonnet-5-5`: USD 2 / 10 (per-ad extraction, with vision).
  - `claude-haiku-4-5`: USD 1 / 5 (cheap text tagging).
- **Media handling:**
  - Claude takes **images only** (JPEG, PNG, GIF, WebP; animations use the first frame). Limits: 10 MB each; up to 600 per request on 1M-context models; images must be 2000 px or less when a request has more than 20; high-res tier up to 2576 px / 4784 visual tokens; cost = `ceil(w/28) * ceil(h/28)` tokens.
  - **Video has to become keyframes plus a transcript.** Use `ffmpeg` scene-change frames, the first 3 s at 1-2 fps for the hook, and a local speech-to-text model [inference].
- **Cost estimate** [inference; Sonnet 5.5 extraction, Opus 5.5 synthesis]:
  - Per ad: about 3k input tokens (text plus a 1000 px image, about 1,296 tokens) and about 700 output, so about **USD 0.013/ad**, or about 0.0065 with Batches.
  - Video ads: +10 frames, about +13k tokens, about +USD 0.026/ad.
  - Synthesis over 200 ads: about 80k in / 10k out, about USD 0.5.
  - **Total: about USD 2-4 per 200-ad competitor report.** On a Claude Code subscription this comes out of plan usage instead.

## 5. ToS and policy risk

| Approach | Verdict | Why (source) | Compliant alternative |
|---|---|---|---|
| Scraping facebook.com/ads/library: HTTP scrapers, Apify "Ad Library scraper" actors, "no API token" Ad Library MCP servers (for example `RamsesAguirre777/facebook-ads-library-mcp`: "scrapes the public Facebook Ad Library (no API token)") | **Prohibited** | Meta Terms §3.2: "You may not access or collect data from our Products using automated means (without our prior permission)... regardless of whether such automated access or collection is undertaken while logged in" [official]. Risk: account or app enforcement (Platform Terms §7) | `ads_archive` API; MCP `ads_library_search` |
| Driving the Ad Library website with a browser agent (Claude in Chrome, computer use, Playwright) | **Prohibited** (same clause) | Automated means | Human browsing is fine; the agent uses the APIs |
| Bulk-parsing `ad_snapshot_url` pages to mirror all media | **Grey, avoid** | Meta permits download "for an individual ad... for analysis"; no batch download | Per-ad, on-demand fetch; a retention limit; a logged purpose |
| Sharing `ad_snapshot_url` links in reports | **Leaks your token** | The URL embeds `access_token`; Platform Data "includes all Meta Product access tokens" (Platform Terms, definitions) | Public permalink `facebook.com/ads/library/?id=<id>` |
| Publishing research (article, LinkedIn post, SEMrush/Ahrefs guest post, YouTube script) | **Allowed** for analysis | Ad Library FAQ: "Our Platform Policy allows researchers and journalists to use the Ad Library API to study advertising at scale and create and publish reports or articles." "Processed data or data that is analysed as part of your research can be published and shared publicly" [official] | Publish findings and aggregates. Don't republish competitors' creatives wholesale (their copyright, [inference]) |
| Selling datasets or licensing competitor-ad data | **Prohibited** | Platform Terms §3.a.iv: "Selling, licensing, or purchasing Platform Data". Platform Data includes data "derived from such data" [official] | Sell your own analysis or consulting, not the data. Get Meta's written guidance before productizing |
| Sending Platform Data to an LLM provider | **Allowed with conditions** | Platform Terms §5.a: a Service Provider must agree in writing to process "solely for you and at your direction... and for no other purpose, including for the Service Provider's own purposes" [official] | Use Claude under Anthropic's commercial/API terms; Anthropic's vision doc: "Anthropic does not use uploaded images to train models". Avoid consumer-plan settings that let chats train models [inference: verify the plan's terms] |
| Own-account data (2e), especially for clients | **Allowed with rules** | Dev Policies §10: use advertiser data "only to optimize or measure the performance of that end-advertiser's Meta campaign"; "Don't mix data obtained from us with advertising campaigns on different platforms (unless the terms for that product allow it explicitly)"; "Keep Meta's data... on behalf of one advertiser separately"; "Display Meta ad campaign reporting separately from other publishers" [official] | One data folder per ad account. Meta shown as its own section in cross-channel reports. Note: Meta's AI-connectors announcement pitches "cross-channel insights" through the connector, so cross-channel analysis done *through the MCP* looks sanctioned [inference] |
| Third-party "Meta Ads MCP" servers (Pipeboard and others) | **Avoid** | You hand them your ads token | Official `mcp.facebook.com/ads` |
| Agent writing to live ad accounts | **Risky** | Write tools exist (create, activate, budgets). Writes default to paused, but `ads_activate_entity` spends money | MCP rules in Business Suite > Settings > Integrations > Ads MCP server: block `all` write actions for the MVP. Meta: using the MCP server itself doesn't cause bans [official help] |

Retention [official, Platform Terms §3.d]: delete Platform Data "when retaining the Platform Data is no longer necessary for a legitimate business purpose", on Meta's request, or when you stop the product. Recommend a default of 180 days for raw pulls and 90 days for media, with reports kept (they are analysis) [inference].

---

# Part 2 - PM (prioritization)

## 6. Phased roadmap

Effort scale (agent-assisted dev): **S** is 1 day or less, **M** is 2-5 days, **L** is 1-3 weeks. Calendar waits (ID confirmation, App Review) are listed separately.

| Phase | Feature | Captain item | Value | Effort | Dependency | Risk |
|---|---|---|---|---|---|---|
| **0 MVP** | Step 0 coverage spike: run `ads_archive` and MCP `ads_library_search` on one EU/UK advertiser and one India-only advertiser | 2a | Decides feasibility for non-EU competitors | S | ID confirmation, app, token | May show India-only commercial ads are unreachable |
| 0 | Fetch every ad for a Page by month (paged, all statuses), raw JSONL | 2a | Core ledger | S | `ads_archive` | EU/UK-only coverage for commercial ads |
| 0 | Normalized per-ad JSON plus permalink plus deterministic metrics (length, days running, variants, platforms) | 2a, 2b, 2c | Foundation for everything | S | none | none |
| 0 | Claude text extraction per ad: hook, offer, CTA, proposition, tone, quality rubric | 2b, 2c | The first "insight" layer | S | Claude | Rubric drift; mitigate with schema and a small eval set |
| 0 | Markdown report: timeline, ledger, messaging themes, tone/length/consistency, longest-running ads, coverage caveats | 2c | The deliverable | S | none | none |
| **1** | Per-ad creative capture (images, carousel cards) with vision: on-image text, visual style, format | 2b | High (visuals drive ads) | M | Per-ad snapshot fetch | Snapshot HTML changes; grey-zone if bulk |
| 1 | Video breakdown: keyframes, 3-second hook, transcript, highlight moments, storyline | 2b | High for Reels-heavy brands | L | ffmpeg, speech-to-text | Cost and accuracy on long videos |
| 1 | Offer tracker (offer type or price over months) | 2b | Medium-high | S | Phase 0 extraction | none |
| **2** | Similarity: proposition clusters, storyline similarity, creative reuse (perceptual hash), clip similarity | 2d | High for "what's their playbook" | M-L | Phase 1 media | Embedding model choice |
| 2 | Multi-competitor comparison (up to 10 Pages per query) | 2c, 2d | High | M | none | none |
| 2 | EU reach and demographics analysis for EU-delivered ads | 2b, 2d | Medium; the only competitor numbers | S | EU delivery | Only for EU/UK advertisers |
| 2 | Branded-content (creator) ads | 2b | Medium | S | Branded Content Search API | Verify fields |
| **3** | Own-account performance through the Meta Ads MCP in Claude Code (read-only rules) | 2e | High | S | MCP OAuth; own ad account | Write tools if rules aren't set |
| 3 | Own creative to performance (asset breakdowns, video retention), plus ROI with the captain's margins | 2e | High | M | `ads_read` | Attribution caveats |
| 3 | Industry benchmarks and opportunity score (the only official "vs others" signal) | 2e | Medium-high | S | MCP | Beta tool |
| 3 | Persona and "deepest angle" mining plus brainstorm: competitor gaps crossed with own winners, giving new hooks, offers, and scripts | goals | High | M | Phases 0-2 (3 optional) | Hallucinated personas; ground every claim in cited ads |
| **4** | Multi-format outputs: research article (SEMrush/Ahrefs style), LinkedIn post, YouTube script, slide deck | goals | Medium-high | S each | Report JSON | Don't republish competitor creatives |
| 4 | Scheduled monitoring: weekly diff ("5 new ads, offer changed to 30% off") | 2a | High for ongoing use | M | launchd/cron + headless Claude | Token expiry about 60 days; re-auth needed |
| 4 | **Messenger front-end** (the "inside Facebook" experience) | hosting | Medium; mainly UX and wow factor | L | Page, `pages_messaging`, public webhook | Breaks local-only; 20 s webhook rule; 24 h window |
| 4 | Organic engagement on competitor Pages (likes, comments on posts) | 2b | Medium | M, plus App Review wait | Page Public Content Access + App Review | Rejection risk |
| **5** | Meta AI Connector (agent callable from Meta AI) | hosting | Speculative | L | Waitlist acceptance, live REST API | Preview product |
| 5 | Productize for clients (multi-tenant) | all | Business | L | App Review, business verification, privacy policy, per-advertiser data separation | Policy load |

## 7. MVP specification

### 7.1 Exact APIs and permissions

- **Endpoint:** `GET https://graph.facebook.com/v26.0/ads_archive`
  - Params:
    - `search_page_ids=[<page_id>]`
    - `ad_reached_countries=["ALL"]`: test this against an explicit EU list plus `GB` in the Step 0 spike, because of the doc inconsistency in §2a.
    - `ad_active_status=ALL`
    - `ad_delivery_date_min` / `ad_delivery_date_max` per month window
    - `limit=500`
    - `fields=id,page_id,page_name,ad_creation_time,ad_delivery_start_time,ad_delivery_stop_time,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,ad_creative_link_captions,ad_snapshot_url,languages,publisher_platforms,eu_total_reach,total_reach_by_location,age_country_gender_reach_breakdown,target_ages,target_gender,target_locations,beneficiary_payers`
  - The API returns an error if you pass a parameter that's invalid for the ad type, and "fields will only return for ads that have data available" [official].
  - Auth: long-lived user token of the ID-confirmed captain, who is an app Admin. No extra permission.
- **Optional (Step 0 spike only):** the Meta Ads MCP `ads_library_search`. Use Meta's no-app connector route, or own-app: `claude mcp add --transport http --client-id <META_APP_ID> meta-ads https://mcp.facebook.com/ads`.
- **Claude:** Claude Code as orchestrator. `claude-sonnet-5-5` subagents for extraction, `claude-opus-5-5` for synthesis.

### 7.2 Meta developer app setup checklist (captain)

1. **Today:** go to **facebook.com/ID** and complete identity and location confirmation. Wait for approval, which takes "a few days".
2. Go to **developers.facebook.com**, select **Get started**, register as a developer, and accept the Platform Policy.
3. *(Optional but recommended)* Create a **business portfolio** in Meta Business Suite. It can stay unverified. "If your app will access data that you don't own or manage, you must connect your app to a business portfolio" [official]. Connecting it costs nothing and avoids ambiguity.
4. Go to **developers.facebook.com/apps/creation/**:
   1. Enter the name `FBCM Ad Library (dev)` and your contact email.
   2. **Use case: scroll down, select "Other", then "Business"**. Business-type apps can generate tokens.
   3. Connect the portfolio from step 3, or pick "I don't want to connect a business portfolio yet".
   4. Create the app.

   Use cases can't be removed later. Keep this app for the Ad Library only, and create a second app in Phase 3 if you go the own-app MCP route.
5. In App Settings > Basic, note the **App ID** and **App Secret**. Keep the app in **Development mode**. **Do not submit for App Review.**
6. Open **developers.facebook.com/tools/explorer**. Select your app, then **Generate Access Token** (user token). Turn off any ad blocker: error `1357045` "is usually associated with an ad blocker" [official].
7. Run a test query: `ads_archive?search_page_ids=[<ID>]&ad_reached_countries=["ALL"]&ad_active_status=ALL&fields=id,page_name,ad_delivery_start_time,ad_creative_bodies`.
8. Exchange the token for a long-lived one (about 60 days). Run this server-side (from your Mac), never in a browser:
   `GET https://graph.facebook.com/v26.0/oauth/access_token?grant_type=fb_exchange_token&client_id=<APP_ID>&client_secret=<APP_SECRET>&fb_exchange_token=<SHORT_TOKEN>`
9. Put `META_APP_ID`, `META_ACCESS_TOKEN`, and `META_API_VERSION=v26.0` in the project's `.env`. It must be git-ignored.
10. **Step 0 spike:** add the Meta Ads MCP to Claude Code, log in, and ask it to search the Ad Library for (a) the EU/UK competitor and (b) the India-only competitor. Record what comes back: fields, countries, media links.
11. *(Phase 3, own account)* In Business Suite > Settings > Integrations > **Ads MCP server**, block write actions on your ad account before connecting any agent.

### 7.3 Per-ad data schema (v0.1)

```jsonc
{
  "schema_version": "0.1",
  "library_id": "123456789012345",                 // ads_archive.id
  "page": { "id": "1122334455", "name": "Brand X" },
  "permalink": "https://www.facebook.com/ads/library/?id=123456789012345",
  "coverage_basis": "eu_uk_delivery",              // eu_uk_delivery | political_issue | mcp_library_search
  "dates": {
    "created": "2026-03-02T10:11:00+0000",         // ad_creation_time
    "delivery_start": "2026-03-03T00:00:00+0000",  // first impression (post-Aug-2020 semantics)
    "delivery_stop": null,
    "start_month": "2026-03",
    "active_months": ["2026-03", "2026-04"],
    "days_running": 41,
    "active_at_fetch": true
  },
  "platforms": ["FACEBOOK", "INSTAGRAM"],
  "languages": ["en"],
  "media_type_hint": "VIDEO",                      // from which media_type-filtered query found it
  "text": {
    "bodies": ["Card 1 text...", "Card 2 text..."],
    "link_titles": ["..."], "link_descriptions": ["..."], "link_captions": ["brandx.com"]
  },
  "eu_metrics": {                                  // null if not EU/UK-delivered
    "eu_total_reach": 184000,
    "total_reach_by_location": { "EU": 184000, "UK": 22000 },
    "age_country_gender_reach_breakdown": [],
    "target_ages": ["25", "44"], "target_gender": "All", "target_locations": [],
    "beneficiary_payers": []
  },
  "political_metrics": null,                       // spend/impressions ranges if POLITICAL_AND_ISSUE_ADS
  "derived": {                                     // deterministic, scripts/text_metrics.py
    "body_chars": [212, 180], "body_words": [38, 31], "variant_count": 2,
    "emoji_count": 3, "has_price": true, "has_percent_off": true, "landing_domain": "brandx.com",
    "near_duplicate_of": null
  },
  "llm": {                                         // Claude, schema-validated
    "hook": "Stop overpaying for ...",
    "proposition": "Cheaper than competitors with same quality",
    "offer": { "type": "discount", "text": "30% off first order", "deadline": null },
    "cta": "Shop now",
    "tone": ["urgent", "confident"],
    "audience_signals": ["price-sensitive", "first-time buyers"],
    "format_guess": "carousel",
    "quality": { "clarity": 4, "specificity": 3, "brand_consistency": 4, "notes": "..." },
    "confidence": 0.8
  },
  "creative": null,                                // Phase 1: images[{path, ocr_text, visual_notes}], video{frames[], transcript, hook_3s, highlights[]}
  "provenance": {
    "api_version": "v26.0", "fetched_at": "2026-10-02T09:00:00Z",
    "extractor_model": "claude-sonnet-5-5", "prompt_version": "extract-v1"
  }
}
```

`ad_snapshot_url` stays only in `data/raw/`, never in `data/ads/` or reports, because it contains the token.

### 7.4 Suggested file layout (`/Users/raj/Documents/Work + Portfolio/2026 Y/Facebook CM Agent/`)

```
Facebook CM Agent/
├── CLAUDE.md                  # agent rules: no scraping, no snapshot URLs in outputs, retention, workflow
├── README.md                  # how to run; coverage caveats
├── .env.example               # META_APP_ID= META_ACCESS_TOKEN= META_API_VERSION=v26.0
├── .gitignore                 # .env, data/raw/, data/media/
├── .claude/
│   ├── agents/
│   │   ├── ad-extractor.md        # Sonnet 5.5; batch of ads -> llm{} JSON
│   │   ├── creative-analyst.md    # Phase 1
│   │   └── report-writer.md       # Opus 5.5; aggregates -> markdown
│   └── skills/
│       ├── fetch-ads/SKILL.md     # wraps scripts/fetch_ads.py
│       └── analyze-competitor/SKILL.md   # end-to-end /analyze-competitor
├── config/competitors.yaml     # name, page_id, markets, notes
├── prompts/
│   ├── extraction.md           # per-ad extraction instructions + examples
│   ├── rubric.md               # tone / quality / consistency rubric
│   └── report-template.md
├── schemas/ad.schema.json      # the 7.3 schema
├── scripts/
│   ├── fetch_ads.py            # ads_archive paging, month windows, retries, rate-limit headers
│   ├── normalize.py            # raw -> data/ads/*.json + permalinks
│   └── text_metrics.py         # deterministic length/variant/offer-regex metrics
├── data/
│   ├── raw/<page_id>/<run_date>.jsonl      # verbatim API (contains tokens in snapshot URLs; retention 180d)
│   ├── ads/<page_id>/<library_id>.json     # normalized + derived
│   └── media/<page_id>/<library_id>/       # Phase 1; retention 90d
├── reports/<page_id>/<date>-competitor-report.md
└── docs/
    ├── research/fbcm-research-r1.md        # this report
    ├── decisions.md
    └── compliance.md                       # sources, retention, what we never do
```

**MVP acceptance criteria:**
- A Page ID plus a month range produces a report in under 15 minutes for 300 or fewer ads.
- Every `paging.next` is followed.
- Every ad appears in the ledger with its permalink.
- Nothing in `reports/` contains `access_token`.
- The report states its coverage basis ("EU/UK-delivered ads only" and so on).
- Tone and length results reproduce within tolerance on a re-run: same schema, and same prompt version stamped.

## 8. Open decisions for the captain

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | **Hosting surface** | (a) Local Claude Code plus Meta-hosted Ads MCP; (b) add a Messenger bot on your Page; (c) apply to the Meta AI Connectors preview; (d) external web app with Facebook Login | **(a) now.** (b) as Phase 4 only if chatting inside Facebook matters. (c) optional waitlist application; it needs a live REST API and company details. "Inside Facebook" as the *foundation* isn't available |
| D2 | **Test competitors and region** | Name 1-2 competitor Pages | **One that advertises in the EU/UK** (guaranteed API data) **plus one India-only competitor** (tests the MCP coverage question). If India-only commercial ads turn out to be unreachable, the product's scope narrows to EU/UK advertisers, or to own-account plus manual review |
| D3 | **Use your own ad account for 2e** | Yes (read-only), yes (read and write), or no | **Yes, read-only, in Phase 3**, with Ads MCP rules blocking writes. It is the only source of real CPM, CPA, CPL, and ROAS, and of benchmarks |
| D4 | **Claude billing route** | Claude Code subscription vs Anthropic API key | **API key (commercial terms) once you process Platform Data regularly.** It is the clean fit for Platform Terms §5 and allows the Batches 50% discount. A subscription is fine for the Step 0 spike if model-training settings are off (verify the plan's terms) |
| D5 | **Scope: personal research vs client or product** | Personal/own brand; agency deliverables; SaaS | **Personal or own-brand for now.** Clients trigger Dev Policies §10 data separation. A product triggers App Review, business verification, a privacy policy, and the ban on selling Platform Data |
| D6 | **Creative media policy** | Text-only; per-ad on-demand download; bulk mirror | **Per-ad on-demand download with a 90-day retention.** Meta sanctions this. Bulk mirroring is off the table |

**Captain actions, not decisions:**
- Start facebook.com/ID now.
- Tell us whether you already have a Meta developer account, a business portfolio, or an ad account.

---

## Flags (things that looked off along the way)

- **Canvas / Web Games shuts down tomorrow (2026-09-30).** Any advice that mentions "Facebook Canvas apps" is obsolete.
- Meta doc inconsistencies:
  - `ad_active_status` default: ACTIVE per the reference, ALL per the 2021 release note.
  - UK-only ad coverage: landing page vs the `ad_reached_countries` note.
  - Stale version examples: `v3.1` in the Ad Library sample; the Insights version picker stops at v25.0; the long-lived-token page says "Latest Graph API Version: v25.0" while the changelog says v26.0.
- EU political ads: "The search parameter combination of ad_type with value POLITICAL_AND_ISSUE_ADS and ad_reached_countries for any countries in the European Union will no longer be available" (2025-10-06) [official]. It doesn't affect commercial ads.
- Third-party misinformation seen during research:
  - The Ads CLI install is **`pip install meta-ads`** (PyPI, Python 3.12+, system-user token) [official]. It is not `npm install -g @meta/ads-cli` as some blogs say. The CLI covers own-account resources only; it has no Ad Library command.
  - Blogs also claim the Ad Library API needs `ads_read` App Review. The official FAQ only requires a user token of an ID-confirmed user with an app role.
- Most "Meta Ad Library MCP" servers on Apify, Glama, and GitHub are scrapers. See §5.

## Recommendation (what should happen next)

1. The captain answers D1-D6, above all D2 (competitors), and starts facebook.com/ID.
2. Once the token exists, a follow-up crew task builds **Phase 0**:
   - the Step 0 coverage spike;
   - the fetch and normalize scripts, the ad-extractor subagent, and the report template;
   - the file layout in §7.4.

   This is ship-able work in the project repo, about 1-2 days.
3. Re-plan Phases 1-2 from the spike's coverage result.

---

## Sources (all accessed 2026-09-29)

**Meta - Ad Library**
- Ad Library API (landing, FAQ, field list): https://www.facebook.com/ads/library/api/
- Ad Library API release notes: https://www.facebook.com/ads/library/api/releasenotes
- `ads_archive` reference (v26.0): https://developers.facebook.com/docs/graph-api/reference/ads_archive/
- `ArchivedAd` reference: https://developers.facebook.com/docs/graph-api/reference/archived-ad/
- Transparency Center, Ad Library tools: https://transparency.meta.com/researchtools/ad-library-tools

**Meta - Ads AI Connectors (MCP and CLI)**
- Announcement (2026-04-29, updated 2026-07-16): https://www.facebook.com/business/news/meta-ads-ai-connectors
- Help, setup and rules: https://www.facebook.com/business/help/1456422242197840
- MCP overview: https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-overview
- MCP get started: .../ads-mcp-server/ads-mcp-server-get-started
- MCP tools, reporting: .../ads-mcp-server-tools-comprehensive-reporting
- MCP tools, ad creation (incl. `ads_library_search`): .../ads-mcp-server-tools-ad-creation-and-management
- MCP rules: .../ads-mcp-server-rules-best-practices
- Ads CLI: https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-cli/ads-cli-overview and .../ads-cli/setup/get-started

**Meta - Marketing API**
- Authorization and access tier: https://developers.facebook.com/documentation/ads-commerce/marketing-api/get-started/authorization
- Rate limits: https://developers.facebook.com/documentation/ads-commerce/marketing-api/overview/rate-limiting
- Ad Account Insights reference: https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/ad-account/insights
- Ad Creative reference: https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/ad-creative
- Changelog: https://developers.facebook.com/documentation/ads-commerce/marketing-api/marketing-api-changelog

**Meta - Graph API and apps**
- Graph API changelog: https://developers.facebook.com/docs/graph-api/changelog
- Graph API rate limits: https://developers.facebook.com/docs/graph-api/overview/rate-limiting
- Create an app: https://developers.facebook.com/documentation/development/create-an-app
- Other app types (Business vs Consumer): https://developers.facebook.com/documentation/development/create-an-app/other-app-types
- Long-lived tokens: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens/get-long-lived
- Page Public Content Access: https://developers.facebook.com/docs/features-reference/page-public-content-access

**Meta - hosting surfaces**
- Web and Instant Games changes (2025-07-31, updated 2025-11-18): https://developers.facebook.com/blog/post/2025/07/31/web-and-instant-games-changes/
- Page tabs edge: https://developers.facebook.com/docs/graph-api/reference/page/tabs/
- Meta Business Extension: https://developers.facebook.com/docs/meta-business-extension
- Messenger Platform: https://developers.facebook.com/documentation/business-messaging/messenger-platform (plus `/overview`, `/webview`, `/app-review`)
- Meta Connect 2026 recap (2026-09-24): https://developers.meta.com/blog/meta-connect-recap/
- Meta AI Connectors: https://dev.meta.ai/products/connectors

**Meta - terms and policies**
- Meta Terms of Service: https://www.facebook.com/terms.php
- Platform Terms (last updated 2026-02-03): https://developers.facebook.com/terms/
- Developer Policies (last updated 2026-02-03): https://developers.facebook.com/devpolicy/

**Anthropic**
- Vision: https://platform.claude.com/docs/en/build-with-claude/vision
- Model IDs and pricing: bundled `claude-api` skill model table, cached 2026-09-25

**Third-party (leads only)**
- Pi agent: https://github.com/earendil-works/pi
- Example scraper MCP (cited as what *not* to use): https://github.com/RamsesAguirre777/facebook-ads-library-mcp
- getpassionfruit.com, soku.ai, and ppc.land blog posts on the Ads MCP and CLI
