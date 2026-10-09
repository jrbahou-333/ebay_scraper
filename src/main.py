"""Orchestrator: search eBay → dedupe in Postgres → alert new listings on Telegram.

Run:
    python -m src.main            # full run (writes to DB, sends alerts)
    python -m src.main --dry-run  # search + print only; no DB writes, no sends
"""

import os
import sys
import time

from . import config, db, filters
from . import notifier as notifier_mod
from .ebay import EbayClient, EbayError
from .notifier import Notifier

# Dead-man switch: after this many consecutive zero-result runs, warn once per window.
DEADMAN_THRESHOLD = 3
DEADMAN_COOLDOWN_S = 12 * 3600


USED_CONDITION_ID = 3000
PARTS_CONDITION_ID = 7000
EBAY_Q_MAX = 100  # Browse API limit on the `q` parameter length


def fault_query(query: str | None, fault_keywords) -> str:
    """`query` AND any one fault word, in eBay's `(a, b, "c d")` OR syntax."""
    terms = ", ".join(f'"{k}"' if " " in k else k for k in fault_keywords)
    q = f"{query} ({terms})" if query else f"({terms})"
    if len(q) > EBAY_Q_MAX:
        print(f"  ! fault query is {len(q)} chars (eBay max {EBAY_Q_MAX}); trim fault_keywords")
    return q


def search_one(client: EbayClient, cfg: dict, s: dict, label: str):
    """Run one configured search, honouring its per-search overrides.

    - `radius_km` (null = UK-wide) overrides location.radius_km.
    - the highest `premium` tier's max_price is the server-side cap; filters.apply
      then holds each title to the tier it matches (or the base `max_price`).
    - `repair_only` makes two requests and merges them: "For parts or not working"
      with the plain query, plus "Used" with a fault-word OR-group (a plain UK-wide
      Used search would be mostly working machines).
    """
    loc = cfg["location"]
    radius = s["radius_km"] if "radius_km" in s else loc["radius_km"]
    top_cap = max([s["max_price"], *(t["max_price"] for t in filters.premium_tiers(s))])
    server_cap = filters.with_buyer_fee(top_cap) / 100  # fee headroom; see with_buyer_fee
    if s.get("repair_only"):
        fault_kw = (cfg.get("filters") or {}).get("fault_keywords") or []
        passes = [([PARTS_CONDITION_ID], s.get("query")),
                  ([USED_CONDITION_ID], fault_query(s.get("query"), fault_kw))]
    else:
        passes = [(cfg["condition_ids"], s.get("query"))]

    by_id = {}
    for condition_ids, query in passes:
        for x in client.search(
            category_ids=s["category_ids"],
            query=query,
            label=label,
            max_price=server_cap,
            condition_ids=condition_ids,
            postcode=loc["postcode"],
            country=loc["country"],
            radius_km=radius,
        ):
            by_id.setdefault(x.item_id, x)
    return list(by_id.values())


def scrape(client: EbayClient, cfg: dict):
    """Run every configured search; return (kept_listings, total_found, total_dropped)."""
    kept, found, dropped = [], 0, 0
    searches = cfg["searches"]
    for i, s in enumerate(searches):
        label = s.get("name") or s.get("query") or str(s.get("category_ids"))
        try:
            results = search_one(client, cfg, s, label)
        except EbayError as e:
            print(f"  ! search {label!r} failed: {e}")
            continue
        found += len(results)
        k, d = filters.apply(results, cfg.get("filters") or {}, s)
        dropped += d
        kept.extend(k)
        print(f"  {label!r}: {len(results)} found, {d} dropped, {len(k)} kept")
        if i < len(searches) - 1:
            time.sleep(0.3)  # be polite; well under any rate limit
    return kept, found, dropped


def make_client(cfg: dict) -> EbayClient:
    """Build the eBay client from whatever auth is available.

    Prefers a static EBAY_OAUTH_TOKEN (handy for validating before the Cert ID /
    client secret exists); otherwise uses OAuth client-credentials, which the
    scheduled job relies on since it mints a fresh token every run.
    """
    marketplace = cfg.get("marketplace", "EBAY_GB")
    token = os.environ.get("EBAY_OAUTH_TOKEN")
    if token:
        print("eBay auth: static EBAY_OAUTH_TOKEN")
        return EbayClient(marketplace=marketplace, oauth_token=token)
    creds = config.require_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    return EbayClient(creds["EBAY_CLIENT_ID"], creds["EBAY_CLIENT_SECRET"], marketplace)


def dedupe(kept):
    """Collapse listings that several searches returned to one per item_id."""
    by_id = {}
    for x in kept:
        by_id.setdefault(x.item_id, x)
    return list(by_id.values())


def run_dry(cfg):
    client = make_client(cfg)
    print("DRY RUN — no DB writes, no Telegram sends\n")
    kept, found, dropped = scrape(client, cfg)
    unique = dedupe(kept)
    pickup_km = cfg["location"].get("pickup_km")
    print(f"\n{found} found across searches, {dropped} dropped, {len(unique)} unique kept:\n")
    print("  (🚗 Close by, within pickup_km · 🔨 auction: alerted only in its last hour"
          " · 🔧 fault word in title)\n")
    for x in sorted(unique, key=lambda l: (l.distance_km is None, l.distance_km or 0)):
        flags = (("🚗" if notifier_mod.is_close(x.distance_km, pickup_km) else "  ")
                 + ("🔨" if x.is_auction else "  ")
                 + ("🔧" if x.highlights else "  "))
        dist = f"{x.distance_km}km" if x.distance_km is not None else "?"
        print(f"  {flags} {x.price_str:>7} | {dist:>7} | {(x.condition or '?'):<24.24} | {x.title:.48}")
    return 0


def run(cfg):
    env = config.require_env("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    client = make_client(cfg)
    notifier = Notifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"],
                        pickup_km=cfg["location"].get("pickup_km"))
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
    # Buy It Now only — auctions wait in the DB for src/auctions.py's last-hour alert.
    pending = db.fetch_unnotified(conn)
    print(f"{len(pending)} new Buy It Now listing(s) to alert.")
    sent = 0
    for row in pending:
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
    # eBay auth (token or client id+secret) is validated in make_client; DB and
    # Telegram creds are validated in run().
    return run_dry(cfg) if dry else run(cfg)


if __name__ == "__main__":
    sys.exit(main())
