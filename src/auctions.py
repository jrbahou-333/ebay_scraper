"""Auction alerts: one Telegram message per auction, only when it's close to finishing.

The scrape (src/main.py) stores auctions but never alerts them. This runs hourly
(cron-job.org → .github/workflows/auctions.yml), finds stored auctions ending
within `auction_alert_minutes` (120, so alerts land 60-120 min before the end),
and sends each one: "⏰ Auction ends in 1h 34m" + price, linked title, Close by.

Timing comes from the end date stored at scrape time — no eBay call needed. eBay
is asked once more only for the live bid (shown as the price) and to skip
auctions that ended early or went over budget; if eBay is unreachable the alert
still goes out with the last-seen price.

Run:
    python -m src.auctions            # alert + mark in DB
    python -m src.auctions --dry-run  # print what would be alerted; no sends, no marks
                                      # (still applies schema migrations, which are idempotent)
"""

import sys
import time
from datetime import datetime, timezone

from . import config, db
from .ebay import EbayError, current_price
from .main import make_client
from .notifier import Notifier, _price_str


def run(cfg, dry: bool) -> int:
    names = ["DATABASE_URL"] if dry else ["DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    env = config.require_env(*names)
    client = make_client(cfg)
    notifier = None if dry else Notifier(
        env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"],
        pickup_km=cfg["location"].get("pickup_km"),
    )
    window = cfg.get("auction_alert_minutes", 120)

    conn = db.connect(env["DATABASE_URL"])
    try:
        db.ensure_schema(conn)
        rows = db.fetch_ending_auctions(conn, window)
        print(f"{len(rows)} auction(s) ending within {window} min.")
        for row in rows:
            check(conn, client, notifier, row, dry)
        return 0
    finally:
        conn.close()


def check(conn, client, notifier, row, dry: bool) -> str:
    """Alert one auction unless eBay says it has ended or gone over budget.
    Returns what it decided: "ended", "over_budget", "alert" or "send_failed"."""
    item_id = row["item_id"]
    try:
        item = client.get_item(item_id)
        live = True
    except EbayError as e:
        print(f"  ! {item_id}: {e} — alerting from stored data")
        item, live = None, False

    now = datetime.now(timezone.utc)
    end = row["end_date"]
    if live:
        live_end = db.parse_ts(item.get("itemEndDate")) if item else None
        if item is None or (live_end and live_end <= now):
            print(f"  {item_id}: ended/removed — skipping")
            if not dry:
                db.mark_ending_alerted(conn, item_id)
            return "ended"
        bid = current_price(item)
        cap = row.get("max_price_minor")
        if bid is not None and cap is not None and bid > cap:
            print(f"  {item_id}: bid {_price_str(bid, 'GBP')} over cap {_price_str(cap, 'GBP')} — skipping")
            if not dry:
                db.mark_ending_alerted(conn, item_id)
            return "over_budget"
        if bid is not None:
            row["price_minor"] = bid
        end = live_end or end

    minutes_left = max(1, round((end - now).total_seconds() / 60))
    print(f"  {item_id}: ALERT {minutes_left} min left, "
          f"{_price_str(row['price_minor'], 'GBP')} — {row['title'][:50]}")
    if dry:
        return "alert"
    if notifier.send_ending(row, minutes_left):
        db.mark_ending_alerted(conn, item_id)
        time.sleep(1.0)
        return "alert"
    print(f"  send failed for {item_id}; will retry next run")
    return "send_failed"


def main():
    config.load_env()
    return run(config.load_config(), dry="--dry-run" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
