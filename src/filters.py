"""Keyword filtering applied to Listings after the eBay / Gumtree searches.

The sites already cap price (and eBay condition) server-side, so this layer only:
  - drops listings whose title matches an exclude keyword (mostly spare *parts*
    that match an appliance keyword but aren't a whole unit),
  - for `repair_only` searches, drops anything not explicitly faulty,
  - enforces the per-search price tier (base `max_price`, or the highest matching
    `premium` tier's max_price for titles naming a premium model), and
  - tags listings whose title matches a highlight keyword (explicit faults — the
    repair sweet spot), shown in --dry-run output.
"""

from .ebay import Listing

PARTS_CONDITION_ID = "7000"  # "For parts or not working"


def _matches(title: str, keywords) -> list[str]:
    # Space-padded so a keyword like " part " also matches as the first/last word.
    low = f" {(title or '').lower()} "
    return [k for k in keywords if k.lower() in low]


def cap_pence(pounds, source: str = "ebay") -> int:
    """A £ cap → the most a listing may show in pence. Only eBay adds a buyer fee."""
    return with_buyer_fee(pounds) if source == "ebay" else round(pounds * 100)


def with_buyer_fee(pounds) -> int:
    """A £ cap → pence, plus eBay UK's Buyer Protection Fee (£0.70 + 4% for £20-£300).

    The API returns private sellers' prices fee-inclusive: a £50 asking price comes
    back as £52.70. So the caps in searches.yaml mean the seller's asking price.
    """
    return round(pounds * 104) + 70


def is_faulty(listing: Listing, filters_cfg: dict) -> bool:
    """Condition 'For parts or not working', or a fault word in the title.

    Gumtree has no condition field, so its description snippet is checked too
    ("Sage Bambino" titles often hide "won't heat up" in the body).
    """
    if str(listing.raw.get("conditionId")) == PARTS_CONDITION_ID:
        return True
    if "not working" in (listing.condition or "").lower():
        return True
    text = listing.title
    if listing.source == "gumtree":
        text = f"{text} {listing.raw.get('description') or ''}"
    return bool(_matches(text, filters_cfg.get("fault_keywords") or []))


def premium_tiers(search_cfg: dict) -> list[dict]:
    """`premium` as a list of {max_price, keywords} tiers (a single dict is allowed)."""
    premium = search_cfg.get("premium") or []
    return [premium] if isinstance(premium, dict) else list(premium)


def price_cap(title: str, search_cfg: dict) -> int:
    """The £ cap for this title: the highest premium tier whose keywords it names,
    else the base max_price."""
    caps = [t["max_price"] for t in premium_tiers(search_cfg)
            if _matches(title, t.get("keywords") or [])]
    return max([search_cfg["max_price"], *caps])


def apply(listings: list[Listing], filters_cfg: dict, search_cfg: dict | None = None):
    """Split listings into (kept, dropped) and attach matched highlight keywords.

    Returns kept listings, each with `.highlights` (list[str]) and, when a search
    config is given, `.max_price_minor` (the cap that admitted it); and the count
    dropped, for logging. Excludes take precedence over everything else.
    """
    exclude = filters_cfg.get("exclude_keywords") or []
    highlight = filters_cfg.get("highlight_keywords") or []
    search_cfg = search_cfg or {}

    kept = []
    dropped = 0
    for listing in listings:
        if _matches(listing.title, exclude):
            dropped += 1
            continue
        if search_cfg.get("repair_only") and not is_faulty(listing, filters_cfg):
            dropped += 1
            continue
        if "max_price" in search_cfg:
            cap = cap_pence(price_cap(listing.title, search_cfg), listing.source)
            if listing.price_minor is not None and listing.price_minor > cap:
                dropped += 1
                continue
            listing.max_price_minor = cap
        # Stash matched highlight keywords on the object for the notifier.
        listing.highlights = _matches(listing.title, highlight)
        kept.append(listing)
    return kept, dropped
