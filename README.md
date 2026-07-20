# eBay local-pickup sniper — Liverpool

Watches eBay UK for cheap / faulty items worth repairing and flipping, available
for **local collection** within a ~30-minute drive of Liverpool, and pings new
listings to **Telegram** the moment they appear. Runs free on **GitHub Actions**,
every 4 hours during the day.

**Currently hunting:** Dyson V10-and-up cordless vacuums (≤£120) and KitchenAid
stand mixers (≤£150). Earlier white-goods searches (washing machines, dryers,
dishwashers, fridge-freezers, ovens) are preserved commented-out in the config —
everything it hunts is defined in [`config/searches.yaml`](config/searches.yaml).

It uses eBay's official **Browse API** (OAuth) — not scraping — so it runs reliably
from GitHub's cloud runners. (An earlier Facebook Marketplace prototype is archived
in [`archive/fb_marketplace/`](archive/fb_marketplace/); it's blocked from datacenter
IPs, which is why the project moved to eBay.)

## How it works

```
config/searches.yaml ──▶ src/ebay.py ──▶ src/filters.py ──▶ src/db.py ──▶ src/notifier.py
   search terms          Browse API       keyword drop/       Postgres        Telegram
   price/condition/      (newest first)    highlight           dedupe by       (new only)
   radius                                                      item_id
```

Each run: search every term → drop spare-*part* listings & flag explicit faults →
upsert into Postgres (one row per eBay item) → Telegram-alert only rows never seen
before → prune listings unseen for 30 days so the DB stays small.

- **One alert per listing, ever.** `notified_at` is set only after a successful
  send, so crashes never double-send and failed sends retry next run.
- **First run is quiet.** On an empty database it stores a baseline and sends a
  single summary instead of alerting on the entire backlog.
- **Dead-man switch.** Three consecutive empty runs triggers one warning message
  (guards against silent breakage that still exits green).

## Setup

You need four free accounts/credentials. Put them in GitHub **repository secrets**
(Settings → Secrets and variables → Actions), and in a local `.env` (copy
`.env.example`) if you want to run it on your machine.

### 1. eBay Browse API — `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`
1. Sign in / register at <https://developer.ebay.com> (free).
2. **Application keys** → create a **Production** keyset.
3. Copy the **App ID (Client ID)** → `EBAY_CLIENT_ID` and **Cert ID (Client Secret)**
   → `EBAY_CLIENT_SECRET`.
4. If prompted, accept the **Buy API** terms (needed before search returns data).

### 2. Neon Postgres — `DATABASE_URL`
1. Create a free project at <https://neon.tech>.
2. Copy the **pooled** connection string (starts `postgresql://…?sslmode=require`)
   → `DATABASE_URL`. The schema is created automatically on first run.

### 3. Telegram — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
1. In Telegram, message **@BotFather** → `/newbot` → copy the token →
   `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message (so it can message you back).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser → find
   `"chat":{"id":...}` → that number → `TELEGRAM_CHAT_ID`.

### 4. GitHub
The scraper runs on the cron in [`.github/workflows/scrape.yml`](.github/workflows/scrape.yml)
(every 4 hours, 06:00–22:00 UTC; nothing overnight). Add the five secrets above,
then use the **Actions** tab → *scrape* → **Run workflow** to trigger a run
manually at any time. Offline unit tests run on every push (*test* workflow).

> GitHub disables scheduled workflows after **60 days without a repo commit** — push
> any change occasionally to keep it alive.

## Running locally

Linux / macOS:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then fill in your credentials

python -m src.ebay --probe    # verify eBay creds + one live search
python -m src.main --dry-run  # search + print only, no DB writes, no Telegram
python -m src.main            # full run
python -m tests.test_logic    # offline unit tests
```

Windows (PowerShell) — the `PYTHONUTF8` line stops the console mangling £ and emoji:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env       # then fill in your credentials

$env:PYTHONUTF8 = "1"
.venv\Scripts\python -m src.main --dry-run
```

## Configuration

Everything tunable lives in [`config/searches.yaml`](config/searches.yaml): the
searches (category + optional query + price cap), the centre `postcode` and
`radius_km`, the eBay `condition_ids` (3000 = Used, 7000 = For parts or not
working), `retention_days`, and the exclude / highlight keyword lists. The file
opens with a **how-to-edit cheat sheet** — a copy-paste search template, the
query syntax, and the confirmed eBay GB category ids. Edit and commit — changes
take effect on the next run (and the push resets GitHub's 60-day cron timer).
