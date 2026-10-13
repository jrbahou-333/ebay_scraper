"""Orchestrator: search eBay + Gumtree → dedupe in Postgres → alert new listings on Telegram.

Run:
    python -m src.main                    # full run (writes to DB, sends alerts)
    python -m src.main --dry-run          # search + print only; no DB writes, no sends
    python -m src.main --source gumtree   # one site only (ebay | gumtree); combines with --dry-run
"""

import os
import sys
import time

from . import config, db, filters
from . import notifier as notifier_mod
from .ebay import EbayClient, EbayError
from .gumtree import GumtreeClient, GumtreeError
from .notifier import Notifier

# Dead-man switch: after this many consecutive zero-result runs, warn once per window.
DEADMAN_THRESHOLD = 3
DEADMAN_COOLDOWN_S = 12 * 3600

SOURCES = ("ebay", "gumtree")
SOURCE_NAMES = {"ebay": "eBay", "gumtree": "Gumtree"}
DEADMAN_HINTS = {
    "ebay": "it may be blocked, misconfigured, or credentials expired.",
    "gumtree": "it may be blocked, misconfigured, or Gumtree changed its page layout.",
}

USED_CONDITION_ID = 3000
PARTS_CONDITION_ID = 7000
EBAY_Q_MAX = 100  # Browse API limit on the `q` parameter length
GUMTREE_RADIUS_MILES = 30  # default when a gumtree: block omits radius_miles


def fault_query(query: str | None, fault_keywords) -> str:
    """`query` AND any one fault word, in eBay's `(a, b, "c d")` OR syntax."""
    terms = ", ".join(f'"{k}"' if " " in k else k for k in fault_keywords)
    q = f"{query} ({terms})" if query else f"({terms})"
    if len(q) > EBAY_Q_MAX:
        print(f"  ! fault query is {len(q)} chars (eBay max {EBAY_Q_MAX}); trim fault_keywords")
    return q


def source_cfg(s: dict, source: str) -> dict | None:
    """A search's settings for one site, or None if it doesn't search there.

    Searches name each site in its own block (`ebay:` / `gumtree:`). An older
    search with `category_ids` at the top level is eBay-only.
    """
    if s.get(source):
        return s[source]
    if source == "ebay" and "category_ids" in s:
        return {k: s[k] for k in ("category_ids", "radius_km") if k in s}
    return None


def top_cap(s: dict) -> int:
    """The highest £ cap a search allows (base or premium); the server-side cap."""
    return max([s["max_price"], *(t["max_price"] for t in filters.premium_tiers(s))])


def search_ebay(client: EbayClient, cfg: dict, s: dict, label: str):
    """Run one search's eBay side, honouring its overrides.

    - `ebay.radius_km` (null = UK-wide) overrides location.radius_km.
    - the highest `premium` tier's max_price is the server-side cap; filters.apply
      then holds each title to the tier it matches (or the base `max_price`).
    - `repair_only` makes two requests and merges them: "For parts or not working"
      with the plain query, plus "Used" with a fault-word OR-group (a plain UK-wide
      Used search would be mostly working machines).
    """
    e = source_cfg(s, "ebay")
    loc = cfg["location"]
    radius = e["radius_km"] if "radius_km" in e else loc["radius_km"]
    server_cap = filters.with_buyer_fee(top_cap(s)) / 100  # fee headroom; see with_buyer_fee
    query = e.get("query", s.get("query"))
    if s.get("repair_only"):
        fault_kw = (cfg.get("filters") or {}).get("fault_keywords") or []
        passes = [([PARTS_CONDITION_ID], query),
                  ([USED_CONDITION_ID], fault_query(query, fault_kw))]
    else:
        passes = [(cfg["condition_ids"], query)]

    by_id = {}
    for condition_ids, q in passes:
        for x in client.search(
            category_ids=e["category_ids"],
            query=q,
            label=label,
            max_price=server_cap,
            condition_ids=condition_ids,
            postcode=loc["postcode"],
            country=loc["country"],
            radius_km=radius,
        ):
            by_id.setdefault(x.item_id, x)
    return list(by_id.values())


def search_gumtree(client: GumtreeClient, cfg: dict, s: dict, label: str):
    """Run one search's Gumtree side: one category, the plain query, the top cap.

    Gumtree's `q` has no OR syntax and no condition filter, so `repair_only` and
    the price tiers are left entirely to filters.apply.
    """
    g = source_cfg(s, "gumtree")
    return client.search(
        category=g["category"],
        query=g.get("query", s.get("query")),
        label=label,
        max_price=top_cap(s),
        postcode=cfg["location"]["postcode"],
        radius_miles=g.get("radius_miles", GUMTREE_RADIUS_MILES),
    )


SEARCHERS = {"ebay": (search_ebay, EbayError), "gumtree": (search_gumtree, GumtreeError)}


def scrape(clients: dict, cfg: dict):
    """Run every configured search on every site it names.

    Returns (kept_listings, stats), where stats[source] counts found / dropped /
    ok / failed searches. One site failing never stops the other.
    """
    kept = []
    stats = {src: {"found": 0, "dropped": 0, "ok": 0, "failed": 0} for src in clients}
    for s in cfg["searches"]:
        label = s.get("name") or s.get("query") or str(s.get("category_ids"))
        for src, client in clients.items():
            if not source_cfg(s, src):
                continue
            search, error = SEARCHERS[src]
            try:
                results = search(client, cfg, s, label)
            except error as e:
                print(f"  ! search {label!r} [{src}] failed: {e}")
                stats[src]["failed"] += 1
                continue
            k, d = filters.apply(results, cfg.get("filters") or {}, s)
            kept.extend(k)
            st = stats[src]
            st["found"] += len(results)
            st["dropped"] += d
            st["ok"] += 1
            print(f"  {label!r} [{src}]: {len(results)} found, {d} dropped, {len(k)} kept")
            time.sleep(0.3)  # be polite; well under any rate limit
    return kept, stats


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


def make_clients(cfg: dict, only: str | None = None) -> dict:
    """{source: client} for each site some search uses (or just `only`)."""
    used = [src for src in SOURCES
            if (only in (None, src)) and any(source_cfg(s, src) for s in cfg["searches"])]
    return {src: make_client(cfg) if src == "ebay" else GumtreeClient() for src in used}


def dedupe(kept):
    """Collapse listings that several searches returned to one per item_id."""
    by_id = {}
    for x in kept:
        by_id.setdefault(x.item_id, x)
    return list(by_id.values())


def _summary(stats: dict) -> str:
    return " · ".join(f"{SOURCE_NAMES[src]}: {st['found']} found, {st['dropped']} dropped"
                      + (f", {st['failed']} failed" if st["failed"] else "")
                      for src, st in stats.items())


def run_dry(cfg, only=None):
    clients = make_clients(cfg, only)
    print("DRY RUN — no DB writes, no Telegram sends\n")
    kept, stats = scrape(clients, cfg)
    unique = dedupe(kept)
    print(f"\n{_summary(stats)}. {len(unique)} unique kept:\n")
    print("  (🚗 Close by, within pickup_km · 🔨 auction: alerted only when close to finishing"
          " · 🔧 fault word in title · eB/GT = eBay/Gumtree)\n")
    for x in sorted(unique, key=lambda l: (l.source, l.distance_km is None, l.distance_km or 0)):
        row = {"distance_km": x.distance_km, "source": x.source}
        flags = (("🚗" if notifier_mod.close_by(row, cfg["location"].get("pickup_km")) else "  ")
                 + ("🔨" if x.is_auction else "  ")
                 + ("🔧" if x.highlights else "  "))
        tag = "GT" if x.source == "gumtree" else "eB"
        where = f"{x.distance_km}km" if x.distance_km is not None else (x.location or "?")
        detail = x.condition or (x.raw.get("description") or "")
        print(f"  {tag} {flags} {x.price_str:>7} | {where:>12.12} | {detail:<24.24} | {x.title:.48}")
    return 0


def run(cfg, only=None):
    env = config.require_env("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    clients = make_clients(cfg, only)
    notifier = Notifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"],
                        pickup_km=cfg["location"].get("pickup_km"))
    conn = db.connect(env["DATABASE_URL"])
    try:
        db.ensure_schema(conn)
        baseline = {src: db.needs_baseline(conn, src) for src in clients}
        conn.commit()  # don't hold a lock on listings while scraping

        kept, stats = scrape(clients, cfg)
        unique = dedupe(kept)
        print(f"\n{_summary(stats)}. {len(unique)} unique kept. baseline={baseline}")

        for src in clients:
            rows = [x for x in unique if x.source == src]
            db.upsert(conn, rows, mark_notified=baseline[src])
            # A site's first successful run stores what's already listed without
            # alerting it. If every search failed, try again next run.
            if baseline[src] and stats[src]["ok"]:
                db.set_state(conn, f"baseline_done:{src}", True)
                notifier.send_text(
                    f"✅ {SOURCE_NAMES[src]} baseline stored: {len(rows)} listings. "
                    "You'll get alerts for new ones from now on."
                )

        _notify_new(conn, notifier, cfg)

        for src in clients:
            _deadman(conn, notifier, src, stats[src]["found"])

        deleted = db.prune(conn, cfg.get("retention_days", 30))
        if deleted:
            print(f"Pruned {deleted} stale listings.")
        return 0
    finally:
        conn.close()


def _notify_new(conn, notifier, cfg):
    # Buy It Now only — auctions wait in the DB until src/auctions.py finds them
    # close to finishing. (Gumtree has no auctions.)
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


def _deadman(conn, notifier, source: str, found: int):
    """Warn if a site's searches keep coming back empty (silent breakage or a block)."""
    suffix = "" if source == "ebay" else f":{source}"  # eBay keeps its original keys
    streak = db.get_state(conn, "empty_streak" + suffix, 0)
    streak = 0 if found > 0 else streak + 1
    db.set_state(conn, "empty_streak" + suffix, streak)
    if streak < DEADMAN_THRESHOLD:
        return
    last = db.get_state(conn, "deadman_last_ts" + suffix, 0)
    now = time.time()
    if now - last >= DEADMAN_COOLDOWN_S:
        notifier.send_text(
            f"⚠️ {SOURCE_NAMES[source]} searches have returned 0 results for {streak} runs "
            f"in a row — {DEADMAN_HINTS[source]}"
        )
        db.set_state(conn, "deadman_last_ts" + suffix, now)


def _arg(name: str) -> str | None:
    """Value after `name` in argv, e.g. `--source gumtree`."""
    if name in sys.argv[:-1]:
        return sys.argv[sys.argv.index(name) + 1]
    return None


def main():
    dry = "--dry-run" in sys.argv
    only = _arg("--source")
    if only and only not in SOURCES:
        raise SystemExit(f"--source must be one of {', '.join(SOURCES)}")
    config.load_env()
    cfg = config.load_config()
    # eBay auth (token or client id+secret) is validated in make_client; DB and
    # Telegram creds are validated in run().
    return run_dry(cfg, only) if dry else run(cfg, only)


if __name__ == "__main__":
    sys.exit(main())
