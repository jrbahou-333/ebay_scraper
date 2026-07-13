"""Orchestrator: search eBay → dedupe in Postgres → alert new listings on Telegram.

Run:
    python -m src.main            # full run (writes to DB, sends alerts)
    python -m src.main --dry-run  # search + print only; no DB writes, no sends
"""

import sys
import time

from . import config, db, filters
from .ebay import EbayClient, EbayError
from .notifier import Notifier

# Dead-man switch: after this many consecutive zero-result runs, warn once per window.
DEADMAN_THRESHOLD = 3
DEADMAN_COOLDOWN_S = 12 * 3600


def scrape(client: EbayClient, cfg: dict):
    """Run every configured search; return (kept_listings, total_found, total_dropped)."""
    loc = cfg["location"]
    kept, found, dropped = [], 0, 0
    searches = cfg["searches"]
    for i, s in enumerate(searches):
        try:
            results = client.search(
                s["query"],
                max_price=s["max_price"],
                condition_ids=cfg["condition_ids"],
                postcode=loc["postcode"],
                country=loc["country"],
                radius_km=loc["radius_km"],
            )
        except EbayError as e:
            print(f"  ! search {s['query']!r} failed: {e}")
            continue
        found += len(results)
        k, d = filters.apply(results, cfg.get("filters") or {})
        dropped += d
        kept.extend(k)
        print(f"  {s['query']!r}: {len(results)} found, {d} dropped, {len(k)} kept")
        if i < len(searches) - 1:
            time.sleep(0.3)  # be polite; well under any rate limit
    return kept, found, dropped


def dedupe(kept):
    """Collapse listings that several searches returned to one per item_id."""
    by_id = {}
    for x in kept:
        by_id.setdefault(x.item_id, x)
    return list(by_id.values())


def run_dry(cfg, env):
    client = EbayClient(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"], cfg.get("marketplace", "EBAY_GB"))
    print("DRY RUN — no DB writes, no Telegram sends\n")
    kept, found, dropped = scrape(client, cfg)
    unique = dedupe(kept)
    print(f"\n{found} found across searches, {dropped} dropped, {len(unique)} unique kept:\n")
    for x in sorted(unique, key=lambda l: (l.distance_km is None, l.distance_km or 0)):
        flag = "🔧" if x.highlights else "  "
        dist = f"{x.distance_km}km" if x.distance_km is not None else "?"
        print(f"  {flag} {x.price_str:>7} | {dist:>7} | {(x.condition or '?'):<24.24} | {x.title:.48}")
    return 0


def run(cfg, env):
    client = EbayClient(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"], cfg.get("marketplace", "EBAY_GB"))
    notifier = Notifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    conn = db.connect(env["DATABASE_URL"])
    try:
        db.ensure_schema(conn)
        baseline = db.is_empty(conn)

        kept, found, dropped = scrape(client, cfg)
        unique = dedupe(kept)
        print(f"\n{found} found, {dropped} dropped, {len(unique)} unique kept. baseline={baseline}")

        db.upsert(conn, unique, mark_notified=baseline)

        if baseline:
            notifier.send_text(
                f"✅ Baseline stored: {len(unique)} listings. "
                "You'll get alerts for new ones from now on."
            )
        else:
            _notify_new(conn, notifier, cfg)

        _deadman(conn, notifier, found)

        deleted = db.prune(conn, cfg.get("retention_days", 30))
        if deleted:
            print(f"Pruned {deleted} stale listings.")
        return 0
    finally:
        conn.close()


def _notify_new(conn, notifier, cfg):
    pending = db.fetch_unnotified(conn)
    print(f"{len(pending)} new listing(s) to alert.")
    sent = 0
    for row in pending:
        row["highlights"] = filters.highlights_for(row["title"], cfg.get("filters") or {})
        if notifier.send_listing(row):
            db.mark_notified(conn, row["item_id"])  # commit per-send: crash-safe, no double-sends
            sent += 1
            time.sleep(1.0)  # stay under Telegram's ~30 msg/s, and gentle overall
        else:
            print(f"  send failed for {row['item_id']}; will retry next run")
    print(f"Sent {sent}/{len(pending)}.")


def _deadman(conn, notifier, found: int):
    """Warn if searches keep coming back empty (silent API/credential breakage)."""
    streak = db.get_state(conn, "empty_streak", 0)
    streak = 0 if found > 0 else streak + 1
    db.set_state(conn, "empty_streak", streak)
    if streak < DEADMAN_THRESHOLD:
        return
    last = db.get_state(conn, "deadman_last_ts", 0)
    now = time.time()
    if now - last >= DEADMAN_COOLDOWN_S:
        notifier.send_text(
            f"⚠️ eBay appliance scraper has returned 0 results for {streak} runs in a row — "
            "it may be blocked, misconfigured, or credentials expired."
        )
        db.set_state(conn, "deadman_last_ts", now)


def main():
    dry = "--dry-run" in sys.argv
    config.load_env()
    cfg = config.load_config()
    needed = ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"]
    if not dry:
        needed += ["DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    env = config.require_env(*needed)
    return run_dry(cfg, env) if dry else run(cfg, env)


if __name__ == "__main__":
    sys.exit(main())
