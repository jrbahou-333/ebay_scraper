"""Postgres persistence: schema, upsert, notify-diff, dead-man state, retention prune.

One row per eBay item_id. Re-seeing an item refreshes last_seen (and distance);
it never creates a second row, so adding search terms does not multiply rows.
"""

from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .ebay import Listing

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, autocommit=False)


def _iter_statements(sql: str):
    """Yield individual statements from a script, skipping comment-only fragments.

    psycopg3's execute() runs a single statement, so we split schema.sql on ';'.
    Safe here because the schema contains no semicolons inside literals or bodies.
    """
    for chunk in sql.split(";"):
        meaningful = [
            ln for ln in chunk.splitlines()
            if ln.strip() and not ln.strip().startswith("--")
        ]
        if meaningful:
            yield chunk


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        for statement in _iter_statements(SCHEMA_PATH.read_text()):
            cur.execute(statement)
    conn.commit()


def is_empty(conn) -> bool:
    """True if the listings table has no rows (used for the baseline guard)."""
    with conn.cursor() as cur:
        cur.execute("SELECT NOT EXISTS (SELECT 1 FROM listings)")
        return cur.fetchone()[0]


def parse_ts(value: str | None):
    """eBay ISO timestamp ('...Z') → aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def upsert(conn, listings, *, mark_notified: bool) -> None:
    """Insert new listings / refresh existing ones.

    New rows get notified_at pre-set to now() when mark_notified is True (baseline
    run), so the first ever run doesn't alert on the whole backlog. Existing rows
    keep their notified_at (never re-alert) and just bump last_seen + distance,
    plus the auction fields (which also backfills rows from before they existed).
    """
    notified_expr = "now()" if mark_notified else "NULL"
    sql = f"""
        INSERT INTO listings (
            item_id, title, price_minor, currency, condition, location,
            distance_km, url, image_url, search_query, origin_date, raw, notified_at,
            is_auction, end_date, max_price_minor
        ) VALUES (
            %(item_id)s, %(title)s, %(price_minor)s, %(currency)s, %(condition)s, %(location)s,
            %(distance_km)s, %(url)s, %(image_url)s, %(search_query)s, %(origin_date)s, %(raw)s, {notified_expr},
            %(is_auction)s, %(end_date)s, %(max_price_minor)s
        )
        ON CONFLICT (item_id) DO UPDATE SET
            last_seen       = now(),
            distance_km     = EXCLUDED.distance_km,
            price_minor     = EXCLUDED.price_minor,
            title           = EXCLUDED.title,
            is_auction      = EXCLUDED.is_auction,
            end_date        = EXCLUDED.end_date,
            max_price_minor = EXCLUDED.max_price_minor
    """
    with conn.cursor() as cur:
        for x in listings:
            cur.execute(
                sql,
                {
                    "item_id": x.item_id,
                    "title": x.title,
                    "price_minor": x.price_minor,
                    "currency": x.currency,
                    "condition": x.condition,
                    "location": x.location,
                    "distance_km": x.distance_km,
                    "url": x.url,
                    "image_url": x.image_url,
                    "search_query": x.search_query,
                    "origin_date": parse_ts(x.origin_date),
                    "raw": Jsonb(x.raw),
                    "is_auction": x.is_auction,
                    "end_date": parse_ts(x.end_date),
                    "max_price_minor": x.max_price_minor,
                },
            )
    conn.commit()


_ALERT_COLS = """item_id, title, price_minor, currency, condition, location,
                 distance_km, url, image_url, search_query"""


def _dicts(cur) -> list[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_unnotified(conn) -> list[dict]:
    """Buy It Now rows still awaiting a Telegram alert (notified_at IS NULL).

    Auctions are excluded: they're alerted once, only when close to finishing,
    by fetch_ending_auctions / src/auctions.py.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ALERT_COLS}
            FROM listings
            WHERE notified_at IS NULL AND NOT COALESCE(is_auction, false)
            ORDER BY origin_date DESC NULLS LAST, first_seen DESC
            """
        )
        return _dicts(cur)


def fetch_ending_auctions(conn, within_min: int) -> list[dict]:
    """Auctions ending in the next `within_min` minutes with no ending alert yet."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ALERT_COLS}, end_date, max_price_minor
            FROM listings
            WHERE is_auction AND ending_alerted_at IS NULL
              AND end_date > now() AND end_date <= now() + make_interval(mins => %s)
            ORDER BY end_date
            """,
            (within_min,),
        )
        return _dicts(cur)


def mark_ending_alerted(conn, item_id: str) -> None:
    """Record the ending alert as done (sent, or skipped as ended/over budget).
    Also sets notified_at so the row reads as alerted everywhere else."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE listings SET ending_alerted_at = now(),
                                   notified_at = COALESCE(notified_at, now())
               WHERE item_id = %s""",
            (item_id,),
        )
    conn.commit()


def mark_notified(conn, item_id: str) -> None:
    """Record a successful send immediately, so a later crash never re-alerts it."""
    with conn.cursor() as cur:
        cur.execute("UPDATE listings SET notified_at = now() WHERE item_id = %s", (item_id,))
    conn.commit()


def prune(conn, retention_days: int) -> int:
    """Delete listings not seen in retention_days; return the count removed.

    Called after notifying, so an item that reappeared this run (last_seen = now())
    is always newer than the cutoff and safe. retention_days <= 0 is a testing knob
    that prunes anything not upserted this instant.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM listings WHERE last_seen < now() - make_interval(days => %s)",
            (retention_days,),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted


# --- dead-man switch state (small key/value store) ---

def get_state(conn, key: str, default=None):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def set_state(conn, key: str, value) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO state (key, value, updated_at) VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, Jsonb(value)),
        )
    conn.commit()
