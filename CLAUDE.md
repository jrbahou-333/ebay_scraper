# CLAUDE.md — ebay_scraper

Handoff notes for Claude Code (and humans). Read this first.

## What this is

A personal "sniper" that finds cheap / faulty **appliances and vacuums** to repair
and flip, within a ~30-minute drive of **Liverpool**, and alerts new ones to
**Telegram**. It queries the **eBay Browse API**, dedupes against a hosted
**Postgres** (Neon), and is designed to run free on **GitHub Actions** (cron).

Owner: Jack (`jrbahou@gmail.com`). Solo project, greenfield.

## Current status (2026-07-25)

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
- **Actions schedule enabled** in `.github/workflows/scrape.yml`: every 4h,
  06–22 UTC (≈07:00–23:00 BST), nothing overnight. 50+ cloud runs green.
  Offline unit tests (`tests/test_logic.py`, 8 passing) run on every push via
  `.github/workflows/test.yml`.
- **Not yet done:** further exclude-keyword tuning if new part-listing patterns
  show up now the net is wider; KitchenAid still thin (occasional alerts
  expected, not a bug).

## How to run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# credentials live in .env (gitignored). See .env.example for the variable names.

python -m src.main --dry-run     # search + filter + print; NO DB writes, NO Telegram
python -m src.ebay --probe [i]   # validate eBay contract for search index i (default 0)
python -m src.main               # full run: writes to DB, sends Telegram alerts
python -m tests.test_logic       # offline unit tests (also run in CI on push)
```

**On Jack's Windows machine** (the current dev box): Python is not on PATH — use
`.venv\Scripts\python.exe`, and set `$env:PYTHONUTF8 = "1"` first or the console
mangles £ and emoji.

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
| `src/db.py` | psycopg3: `ensure_schema`, `upsert`, `is_empty`, `fetch_unnotified`, `mark_notified`, `prune`, `get_state`/`set_state`. |
| `src/filters.py` | Keyword `exclude`/`highlight` logic over `Listing.title`. |
| `src/notifier.py` | Telegram `sendPhoto`/`sendMessage`, one message per listing. |
| `src/main.py` | Orchestrator: `scrape` → `dedupe` → `upsert` → notify. `--dry-run`, dead-man switch, baseline guard. |
| `src/config.py` | Loads `config/searches.yaml` + `.env`; `require_env`. |
| `config/searches.yaml` | All tuning: location, price caps, category-driven searches, keyword filters. Edited freely, reloaded each run. |
| `schema.sql` | `listings` (PK = eBay `item_id`) + `state` tables. |
| `archive/fb_marketplace/` | Abandoned Facebook spike (see history). Not wired in. |

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
3. **`distanceFromPickupLocation` is coarse** — nearby items all report a floored
   value (~5), far items report real values (135, 205, 5155...). Good enough as a
   local/not-local gate at the radius boundary; useless for fine ranking.
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

## Config model (`config/searches.yaml`)

- `location.postcode` seeds the ENDUSERCTX header; `radius_km` enforced client-side.
- `condition_ids: [3000, 7000]` = Used + For parts or not working.
- Each search: `name`, `category_ids` (list, queried one-by-one), optional
  `query`, `max_price` (GBP, server-side cap).
- `filters.exclude_keywords`: high-confidence *part* words that never appear in a
  whole-unit title. **Gotchas:** do NOT exclude `motor` (Dyson **Motorhead** is a
  whole vacuum), `fan` (fan oven), `pump` (heat-pump dryer), or "spares or
  repairs" (a whole faulty unit — that's what we want; it's a *highlight*).
- `filters.highlight_keywords`: fault words (faulty, broken, spares or repairs…)
  that flag the repair sweet spot with 🔧 in alerts.

## Deployment (GitHub Actions)

- Public repo `jrbahou-333/ebay_scraper` → unlimited free Actions minutes.
- `.github/workflows/scrape.yml` runs the pipeline on a cron (2-hourly daytime);
  `.github/workflows/test.yml` runs the offline unit tests on every push.
- All five Actions **secrets** are set and verified: `EBAY_CLIENT_ID`,
  `EBAY_CLIENT_SECRET`, `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  (same values as the local `.env`). If a secret is ever rotated, update both.
- Note: GitHub auto-disables cron workflows after 60 days without repo activity.

## History / why eBay (not Facebook)

Originally targeted **Facebook Marketplace**. Logged-out scraping worked from a
home IP but is **hard-blocked from GitHub's datacenter IPs** (login/captcha wall,
GraphQL "Rate limit exceeded"), confirmed across two CI runs. Rather than run a
self-hosted runner, pivoted to eBay's official API (IP-reputation-agnostic). The
working FB fetcher is preserved in `archive/fb_marketplace/` as a reference for a
possible future "FB via self-hosted runner" second source — it is NOT wired in.

## Secrets hygiene

`.env` is gitignored and holds real credentials — **never commit it**. Only
`.env.example` (variable names, no values) is tracked. Never paste tokens/keys
into tracked files, commit messages, or this file.
