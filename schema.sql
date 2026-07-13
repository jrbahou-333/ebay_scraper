-- Applied idempotently at the start of every run (src/db.py:ensure_schema).

CREATE TABLE IF NOT EXISTS listings (
    item_id      TEXT PRIMARY KEY,          -- eBay itemId (stable per listing)
    title        TEXT NOT NULL,
    price_minor  INTEGER,                    -- pence
    currency     TEXT DEFAULT 'GBP',
    condition    TEXT,                       -- e.g. 'For parts or not working'
    location     TEXT,                       -- itemLocation (city / postcode)
    distance_km  REAL,                        -- distanceFromPickupLocation
    url          TEXT NOT NULL,              -- itemWebUrl
    image_url    TEXT,
    search_query TEXT,                        -- the search term that first found it
    origin_date  TIMESTAMPTZ,                -- itemOriginDate (listing creation)
    raw          JSONB,                       -- full eBay itemSummary, for debugging/future fields
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at  TIMESTAMPTZ                 -- NULL = not yet alerted
);

-- Fast "what still needs alerting?" lookup.
CREATE INDEX IF NOT EXISTS listings_unnotified ON listings (notified_at) WHERE notified_at IS NULL;
-- Supports the retention prune.
CREATE INDEX IF NOT EXISTS listings_last_seen ON listings (last_seen);

-- Small key/value store for the dead-man switch (consecutive empty runs, last alert time).
CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
