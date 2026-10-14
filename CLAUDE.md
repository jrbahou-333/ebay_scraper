# CLAUDE.md — ebay_scraper

Handoff notes for Claude Code (and humans). Read this first.

## What this is

A personal "sniper" that finds cheap / faulty **appliances and vacuums** to repair
and flip, within a ~30-minute drive of **Liverpool**, and alerts new ones to
**Telegram**. It queries the **eBay Browse API** and scrapes **Gumtree** search
pages, dedupes against a hosted **Postgres** (Neon), and is designed to run free
on **GitHub Actions** (cron).

Owner: Jack (`jrbahou@gmail.com`). Solo project, greenfield.

## Current status (2026-10-14): Gumtree as a second source

Merged to `main` 2026-10-14 (branch `gumtree-source`):
- **Gumtree is a second site for the same searches.** Each search in
  `config/searches.yaml` names its terms once (`query`, `max_price`, `premium`,
  `repair_only`) and then one block per site: `ebay: {category_ids, radius_km}`
  and `gumtree: {categories, radius_miles}`. Omit a block to skip that site.
  Old top-level `category_ids` searches still work as eBay-only.
- Gumtree is **local only** (30 miles, categories `coffee-machines` +
  `espresso-machines`, both confirmed by Jack), alerted on
  discovery, tagged "📍 Close by · Gumtree". No buyer fee and no condition
  field, so `repair_only` matches fault words in the title **or** the
  description snippet.
- **GitHub Actions IPs are NOT blocked** (`archive/gumtree_probe/`, 2 runs,
  3/3 searches each, 2026-10-13). But Gumtree has **Radware bot detection**: about a
  dozen requests in a few minutes from the home IP got HTTP 247 + a JS
  challenge, still in force well after. See the Gumtree gotchas below.
- First real run per site is a **silent baseline** (`state.baseline_done:<site>`),
  so adding Gumtree won't burst-alert what's already listed.
- `--source ebay|gumtree` runs one site (e.g. a Gumtree dry run needs no eBay creds).
- Live Gumtree dry run of the full pipeline passed **on GitHub's IPs**
  (`gumtree_probe.yml`, 2026-10-13): 4/4 category requests OK, Sage 3 found and
  Ninja 2 found, all dropped (working or over £50). Kept 0 is correct: nothing
  faulty under the caps within 30 miles at the time.
- **Not yet done:** the first real run. It will send a one-off "✅ Gumtree
  baseline stored: N listings" message (N likely 0) and add the `source` column.

## Earlier status (2026-10-09)

- **Focus is now faulty coffee machines only** (Dyson/KitchenAid commented out,
  grinders removed). Two searches, "Sage coffee machines (faulty)" and "Ninja
  coffee machines (faulty)", across cats 38252/65643/156775/159902:
  - **UK-wide** (`radius_km: null`), incl. collection-only listings (the seller
    may post if asked). Alerts say **"📍 Close by"** within `pickup_km` (60 km).
  - **`repair_only`**: "For parts or not working", plus "Used" with a fault word
    (sent to eBay as an OR-group, and re-checked client-side).
  - **£50 cap, £200 for premium titles** (barista pro/touch, oracle, dual boiler,
    Ninja luxe), **£150 for the Express Impress** (a working used one lists at
    ~£300–370). Caps are asking prices: see the buyer-fee gotcha below.
- **Alerts are minimal by request:** price + linked title + "Close by". No km,
  condition or search label. Jack opens the listing for the rest.
- **Buy It Now is alerted on discovery. Auctions never are**, by Jack's
  explicit choice: no "new auction" message. `src/auctions.py` runs hourly via
  cron-job.org and sends one "⏰ Auction ends in 1h 34m" alert when a stored
  auction ends within `auction_alert_minutes` (120), so it lands 60–120 min
  before the end. Timing comes from the `end_date` stored at scrape time. The
  eBay call only adds the live bid and skips auctions that ended or went over
  budget. If eBay errors, the alert still goes out with the stored price.
- **Distance unit bug fixed (2026-10-09):** eBay reports MILES. Every earlier
  "km" figure in this file (25→40→60 km radius) was really miles.
- Live dry-run at this point: 61 found → 43 dropped → 18 kept (4 auctions).
  `" part"` became `" part "` (whole word), because on coffee machines "for
  parts" means a whole broken unit and it was dropping a £180 Barista Pro.
  Added `repair service`, `diagnostic`, `creami` excludes.
- **Not yet done:** first real scrape and auctions run in Actions after this
  change. The first scrape will alert the ~14 BIN listings in one burst.

## Earlier status (2026-08-12)

- **LIVE.** All credentials exist (local `.env` + GitHub Actions secrets): eBay
  App ID + Cert ID, Neon `DATABASE_URL`, Telegram bot (@JB333_Ebay_bot, chat id
  in `.env`). Real runs validated end-to-end: baseline stored (quiet first run),
  idempotency confirmed, Telegram delivery confirmed with photos and (now)
  hyperlinked titles.
- **Config targets:** "Dyson V10+" (category 20614, OR-query for v10/v11/v12/
  v13/v14/v15/gen5, ≤£120) and "KitchenAid stand mixers" (category **133701**
  Stand Mixers, confirmed by live probe, ≤£200). `radius_km` widened 25→40 on
  2026-07-25 after a week of near-zero matches at 25km (~7 static Dyson
  listings, 0 KitchenAid) — 40km found 21 unique whole units incl. 4
  KitchenAid. Earlier vacuum + appliance searches preserved commented-out in
  `config/searches.yaml`, which now carries a how-to-edit cheat sheet.
- **exclude_keywords tuned against live data** (2026-07-25): a parts reseller
  was flooding the 30-40km band with individual components once the radius
  widened. Added `" part"` (leading space — catches "replacement part"/"motor
  part" without matching "apart"/"compartment"), `body only`, `motor body`,
  `bin slider`, `handle housing`. Cut 45 raw Dyson matches down to 17 genuine
  whole-unit listings (28 dropped). Re-check if alerts start showing spares.
- **Sage coffee machines + grinders added** (2026-08-12), ≤£150 each, driven by
  Jack wanting the Bambino Plus and any other Sage kit. Two searches: "Sage
  coffee machines" (categories **38252** Espresso & Cappuccino, **65643**
  Bean-to-Cup, **156775** Pod & Capsule, **159902** Other) and "Sage coffee
  grinders" (**32882**) — all five ids confirmed by live probe, all leaves (the
  parent 38250 would drag in child 99565 "Coffee, Tea, Espresso Parts"). Live
  probe at 40km: 32 raw → 23 dropped → **9 genuine whole units** (Duo-Temp Pro
  £119 and £104 faulty, Smart Grinder Pro £90, 6× Nespresso Creatista £53–£132).
  Bean-to-Cup was 22/22 spare parts from one reseller ~35km out, so ~30 coffee
  part keywords were added and verified to drop all 23 with **zero** false drops
  (`top cover`, `brew head`, `thermocoil`, `triac`, `drip tray`, `shower
  screen`, `portafilter`, `hopper bean`, `grinder assembly`, …). Deliberately
  NOT excluded: `milk frother`/`milk jug` (whole units say "with milk frother" /
  "no milk jug"), `steam wand`, `burr` (model names read "conical burr grinder").
- **Actions schedule enabled** in `.github/workflows/scrape.yml`: every 4h,
  06–22 UTC (≈07:00–23:00 BST), nothing overnight. 50+ cloud runs green.
  Offline unit tests (`tests/test_logic.py`, 8 passing) run on every push via
  `.github/workflows/test.yml`.
- **Not yet done:** further exclude-keyword tuning if new part-listing patterns
  show up now the net is wider; KitchenAid still thin (occasional alerts
  expected, not a bug). The Sage searches are validated by a full local
  `--dry-run` (82 found / 53 dropped / 29 unique across all four searches) but
  have not yet had a real DB+Telegram run — the first one will alert the ~9
  existing coffee listings in a single burst. Expected, not a bug: the baseline
  guard only suppresses alerts when the whole DB is empty, and it isn't.

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# credentials live in .env (gitignored). See .env.example for the variable names.

python -m src.main --dry-run     # search + filter + print; NO DB writes, NO Telegram
python -m src.ebay --probe [i]   # validate eBay contract for search index i (default 0)
python -m src.gumtree --probe [i] # raw Gumtree results for search i (unfiltered)
python -m src.main --dry-run --source gumtree   # one site only (ebay | gumtree)
python -m src.main               # full run: writes to DB, sends Telegram alerts
python -m src.auctions --dry-run # list auctions ending within the hour; no sends/marks
                                 #   (does apply idempotent schema migrations)
python -m src.auctions           # send ending-soon auction alerts
python -m tests.test_logic       # offline unit tests (also run in CI on push)
```

**On Jack's Windows machine** (the current dev box): Python is not on PATH — use
`.venv\Scripts\python.exe`, and set `$env:PYTHONUTF8 = "1"` first or the console
mangles £ and emoji.

**If `.venv\Scripts\python.exe` dies with `No Python at '...Python312\python.exe'`:**
the venv's base interpreter has gone missing (this happened on 2026-08-12 — only
the Microsoft Store `python.exe` stub was left on PATH). `site-packages` survives
this, so the fix is just to reinstall the same minor version and the existing venv
works again untouched:

```powershell
winget install --id Python.Python.3.12 --scope user --accept-package-agreements
```

`--scope user` matters: it restores the exact `%LOCALAPPDATA%\Programs\Python\Python312`
path `pyvenv.cfg` points at. A different minor version needs `py -m venv --clear
.venv` + a `pip install -r requirements.txt` instead. Note CI is never affected —
Actions installs its own Python.

**Auth flexibility (important for testing):** `make_client()` in `src/main.py`
prefers a static `EBAY_OAUTH_TOKEN` env var if set (a ~2-hour eBay *application
access token*, the `v^1.1#...` blob you can mint in the eBay developer portal),
otherwise it uses OAuth client-credentials from `EBAY_CLIENT_ID` +
`EBAY_CLIENT_SECRET`. The token override lets you validate before the Cert ID
exists; the scheduled job needs the id+secret because it mints a fresh token each
run. Example: `EBAY_OAUTH_TOKEN="$(cat token.txt)" python -m src.main --dry-run`.

## Architecture (one module per concern)

| File | Responsibility |
|---|---|
| `src/ebay.py` | The ONLY module that knows eBay's shape. `EbayClient`, `search()`, `Listing` dataclass, `--probe`. |
| `src/gumtree.py` | The ONLY module that knows Gumtree's page shape. `GumtreeClient`, `parse_results()`, `--probe`. Emits `Listing(source="gumtree", item_id="gt:<id>")`. |
| `src/db.py` | psycopg3: `ensure_schema`, `upsert`, `is_empty`, `fetch_unnotified`, `mark_notified`, `prune`, `get_state`/`set_state`. |
| `src/filters.py` | Keyword `exclude`/`highlight` logic over `Listing.title`; price tiers (buyer fee on eBay only); `repair_only` (Gumtree also checks the description). |
| `src/notifier.py` | Telegram `sendPhoto`/`sendMessage`, one message per listing. |
| `src/main.py` | Orchestrator: `scrape` (searches × sites) → `dedupe` → `upsert` → notify (Buy It Now only). `source_cfg` reads a search's per-site block; `search_ebay` / `search_gumtree`. `--dry-run`, `--source`, per-site dead-man switch and baseline guard. |
| `src/auctions.py` | Last-hour auction alerts: `fetch_ending_auctions` → `get_item` (live bid) → `send_ending`. |
| `src/config.py` | Loads `config/searches.yaml` + `.env`; `require_env`. |
| `config/searches.yaml` | All tuning: location, price caps, category-driven searches, keyword filters. Edited freely, reloaded each run. |
| `schema.sql` | `listings` (PK = eBay `item_id`) + `state` tables. Applied only when its sha256 differs from `state.schema_sha256`: no-op DDL still locks `listings` and deadlocked the concurrent scrape/auctions runs (2026-10-09/10). |
| `archive/fb_marketplace/` | Abandoned Facebook spike (see history). Not wired in. |
| `archive/gumtree_probe/` | Standalone Gumtree reachability probe (stdlib + requests). `.github/workflows/gumtree_probe.yml` now runs `--dry-run --source gumtree` on GitHub's IPs instead (on push of `src/gumtree.py`, or by hand; no secrets). Output is a `::notice::` annotation. |

## eBay Browse API — hard-won gotchas (DON'T re-learn these)

All discovered by live probing during the build. They shape the odd-looking
request code in `src/ebay.py`:

1. **The `pickup*` radius filter is silently IGNORED** on standard Buy API
   access. A deliberately invalid postcode returns identical results. So we do
   **not** rely on it for locality.
2. **Locality is done via a header + client-side filter instead:** send
   `X-EBAY-C-ENDUSERCTX: contextualLocation=country=GB,zip=<postcode>` (inner `=`
   and `,` percent-encoded) — this populates `distanceFromPickupLocation`. Then
   `sort=distance` (nearest first) and **stop paging when an item exceeds
   `radius_km`**. See `_context_header()` and `search()`.
3. **`distanceFromPickupLocation` is in MILES** (`unitOfMeasure: "mi"`) and
   rounded to 5. Nearby items floor at 5. `_distance_km` converts to km. It
   was read as km until 2026-10-09: London showed as "180 km" from Liverpool.
   Good enough as a local/not-local gate. Useless for fine ranking.
4. **Only ONE `category_ids` per request** (error 12030 otherwise). `search()`
   loops each category id and merges/dedupes by `item_id`.
5. **Search by CATEGORY, not keywords.** A bare keyword search is ~90% spare
   parts (e.g. "washing machine" → 82-94% category 99697 "Washing Machine &
   Dryer Parts"). Category-scoped search returns whole units. A `query` can
   refine *within* a category.
6. **Confirmed category IDs (EBAY_GB):** Washing Machines `71256`, Washer-Dryers
   `71257`, Tumble Dryers `71254`, Dishwashers `116023`, Fridge Freezers `20713`,
   Fridges `71262`, Freezers `71260`, Cookers `71250`, Gas Ranges `258592`, Ovens
   `71318`, **Vacuum Cleaners `20614`**.
7. **Auth:** OAuth2 client-credentials. `POST identity/v1/oauth2/token`, HTTP
   Basic `base64(ClientID:ClientSecret)`, `scope=.../oauth/api_scope`. Token ~2h.
8. Always send `X-EBAY-C-MARKETPLACE-ID: EBAY_GB`.
9. **Prices from private sellers include the Buyer Protection Fee** (£0.70 + 4%
   in the £20–£300 band). A £50 asking price comes back as £52.70.
   `filters.with_buyer_fee` adds this headroom to every cap, server and client.
10. **Auctions:** `buyingOptions` contains `"AUCTION"` (often alongside
    `"FIXED_PRICE"`). On those, `price` is the **BIN** price and the bid is in
    `currentBidPrice`. The end time is `itemEndDate`. `current_price()` handles
    this. `GET buy/browse/v1/item/{url-quoted v1|…|0 id}` returns a live bid and
    404s for unknown ids.
11. A `q` OR-group works with an AND term: `sage (faulty, broken, "not working")`.
    `q` is capped at 100 chars, so keep `fault_keywords` short.

## Gumtree — gotchas

1. **No API.** It parses the server-rendered `gumtree.com/search` page. The tile
   hooks are QA attributes (`data-q="search-result"`, `search-result-anchor`,
   `tile-title`, `tile-description`, `tile-location`, `tile-price`), steadier
   than the generated CSS classes. Item id = the number at the end of
   `/p/<category>/<slug>/<id>`.
2. **Radius and price are server-side:** `distance` (MILES from
   `search_location`) and `max_price` (whole £). No per-item distance, so every
   result counts as "Close by".
3. **Bot detection (Radware):** too many requests too fast → **HTTP 247** and a
   JS challenge page (`window.rbzns`). Never try to solve it. The client raises
   `GumtreeError`, stops making requests for the rest of the run, and eBay carries
   on. Keep volume tiny: about 1 request per search per run, 2s apart. **Don't loop
   probes against it while developing.** It locked the home IP out for 30+ min
   after ~12 quick requests.
4. **Out-of-radius padding:** sparse searches get a `data-q="nearby-results"`
   section ("Results from outside your search") with identical tiles from far
   away (Sheffield on a 30-mile Liverpool search). `parse_results` cuts the page
   there. Live: Sage within 30 mi had 2 real results + 4 padded ones.
5. A 200 without `data-q="srp"` is an error (layout change or block), never
   "no results". An empty search still renders `srp`. A page past the end 303s.
6. `q` has no OR syntax: keep `query` to a plain word (the brand). Fault words,
   premium tiers and excludes are all client-side.
7. Seller listings are personal data: test fixtures are synthetic, not saved pages.

## Config model (`config/searches.yaml`)

- `location.postcode` seeds the ENDUSERCTX header. `radius_km` is enforced
  client-side. `pickup_km` drives "📍 Close by".
- Per-search overrides: `ebay.radius_km` (null = UK-wide), `repair_only` (ignores
  `condition_ids`, uses `filters.fault_keywords`), and
  `premium`: a list of `{max_price, keywords}` tiers. The highest matching tier
  wins, and a single dict also works.
- `auction_alert_minutes`: the window for the single auction alert. It must be
  longer than the auction check interval: 120 with hourly checks means alerts
  land 60–120 min before the end.
- `condition_ids: [3000, 7000]` = Used + For parts or not working.
- Each search: `name`, `query`, `max_price` (GBP asking price, server-side cap),
  then per-site blocks: `ebay: {category_ids, radius_km}` (ids queried one by
  one) and `gumtree: {categories, radius_miles}` (each category is one request,
  so keep the list short). Either block may set its own
  `query`. A top-level `category_ids` (old shape) means eBay-only.
- `filters.exclude_keywords`: high-confidence *part* words that never appear in a
  whole-unit title. **Gotchas:** do NOT exclude `motor` (Dyson **Motorhead** is a
  whole vacuum), `fan` (fan oven), `pump` (heat-pump dryer), or "spares or
  repairs" (a whole faulty unit — that's what we want; it's a *highlight*).
- `filters.highlight_keywords`: fault words (faulty, broken, spares or repairs…)
  that flag the repair sweet spot with 🔧 in alerts.

## Deployment (GitHub Actions)

- Public repo `jrbahou-333/ebay_scraper` → unlimited free Actions minutes.
- **Both run workflows are triggered externally by cron-job.org** (since
  2026-10-09). They have no `schedule:`, only `workflow_dispatch`. GitHub's own
  scheduler was starting runs 1–4 hours late (the 22:00 scrape ran at 01:55),
  which is fatal for auction timing. Dispatch-API runs start within seconds.
  - `scrape.yml`: every 4h, 07:00–23:00 UK time.
  - `auctions.yml`: hourly, around the clock.
  - Each cron-job.org job does `POST https://api.github.com/repos/jrbahou-333/
    ebay_scraper/actions/workflows/<file>.yml/dispatches` with body
    `{"ref":"main"}` and headers `Authorization: Bearer <PAT>`,
    `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`.
    GitHub answers 204.
  - The PAT is a fine-grained token limited to this repo, with **Actions:
    Read and write** only. It lives only in cron-job.org. **If it expires, both
    jobs stop silently.** Its expiry date is set in GitHub, and cron-job.org's
    failure email is the alarm.
  - The 60-day inactivity auto-disable doesn't apply to dispatch-triggered runs.
- `.github/workflows/test.yml` runs the offline unit tests on every push.
- All five Actions **secrets** are set and verified: `EBAY_CLIENT_ID`,
  `EBAY_CLIENT_SECRET`, `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  (same values as the local `.env`). If a secret is ever rotated, update both.

## History / why eBay (not Facebook)

Originally targeted **Facebook Marketplace**. Logged-out scraping worked from a
home IP but is **hard-blocked from GitHub's datacenter IPs** (login/captcha wall,
GraphQL "Rate limit exceeded"), confirmed across two CI runs. Rather than run a
self-hosted runner, pivoted to eBay's official API (IP-reputation-agnostic). The
working FB fetcher is preserved in `archive/fb_marketplace/` as a reference for a
possible future "FB via self-hosted runner" second source — it is NOT wired in.

**Gumtree (2026-10-13)** became the second source instead: unlike FB, its
search pages load from GitHub's IPs (`archive/gumtree_probe/`). Jack's rule: if
GitHub ever gets blocked for good, drop Gumtree rather than adding a home runner
or proxy.

## Secrets hygiene

`.env` is gitignored and holds real credentials — **never commit it**. Only
`.env.example` (variable names, no values) is tracked. Never paste tokens/keys
into tracked files, commit messages, or this file.
