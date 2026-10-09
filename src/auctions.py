"""Auction checker: one Telegram alert per auction, in its last hour.

The 4-hourly scrape (src/main.py) stores auctions but never alerts them. This runs
every ~15 min (.github/workflows/auctions.yml), finds stored auctions ending within
`auction_alert_minutes`, re-reads each from eBay for the live bid, and alerts the
ones still under the price cap that admitted them.

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
    lead = cfg.get("auction_alert_minutes", 60)

    conn = db.connect(env["DATABASE_URL"])
    try:
        db.ensure_schema(conn)
        rows = db.fetch_ending_auctions(conn, lead)
        print(f"{len(rows)} auction(s) ending within {lead} min.")
        for row in rows:
            check(conn, client, notifier, row, dry)
        return 0
    finally:
        conn.close()


def check(conn, client, notifier, row, dry: bool) -> None:
    """Refresh one auction from eBay; alert it unless it has ended or gone over budget."""
    item_id = row["item_id"]
    try:
        item = client.get_item(item_id)
    except EbayError as e:
        print(f"  ! {item_id}: {e} — will retry next run")
        return

    now = datetime.now(timezone.utc)
    end = db.parse_ts(item.get("itemEndDate")) if item else None
    if item is None or (end and end <= now):
        print(f"  {item_id}: ended/removed — skipping")
        if not dry:
            db.mark_ending_alerted(conn, item_id)
        return

    bid = current_price(item)
    cap = row.get("max_price_minor")
    if bid is not None and cap is not None and bid > cap:
        print(f"  {item_id}: bid {_price_str(bid, 'GBP')} over cap {_price_str(cap, 'GBP')} — skipping")
        if not dry:
            db.mark_ending_alerted(conn, item_id)
        return

    row["price_minor"] = bid if bid is not None else row["price_minor"]
    minutes_left = max(1, round(((end or row["end_date"]) - now).total_seconds() / 60))
    print(f"  {item_id}: ALERT {_price_str(row['price_minor'], 'GBP')}, "
          f"{minutes_left} min left — {row['title'][:50]}")
    if dry:
        return
    if notifier.send_ending(row, minutes_left):
        db.mark_ending_alerted(conn, item_id)
        time.sleep(1.0)
    else:
        print(f"  send failed for {item_id}; will retry next run")


def main():
    config.load_env()
    return run(config.load_config(), dry="--dry-run" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
