"""Keyword filtering applied to Listings after the eBay search.

eBay already caps price and condition server-side, so this layer only:
  - drops listings whose title matches an exclude keyword (mostly spare *parts*
    that match an appliance keyword but aren't a whole unit), and
  - tags listings whose title matches a highlight keyword (explicit faults — the
    repair sweet spot) so the notifier can flag them.
"""

from .ebay import Listing


def _matches(title: str, keywords) -> list[str]:
    low = (title or "").lower()
    return [k for k in keywords if k.lower() in low]


def highlights_for(title: str, filters_cfg: dict) -> list[str]:
    """Highlight keywords present in a title. Used at notify time, where listings
    come back from the DB as dicts (not Listing objects) so highlights are recomputed
    rather than persisted — which also lets keyword-config changes apply retroactively.
    """
    return _matches(title, filters_cfg.get("highlight_keywords") or [])


def apply(listings: list[Listing], filters_cfg: dict):
    """Split listings into (kept, dropped) and attach matched highlight keywords.

    Returns kept listings, each with a `.highlights` attribute (list[str]); and the
    count dropped, for logging. Excludes take precedence over highlights.
    """
    exclude = filters_cfg.get("exclude_keywords") or []
    highlight = filters_cfg.get("highlight_keywords") or []

    kept = []
    dropped = 0
    for listing in listings:
        if _matches(listing.title, exclude):
            dropped += 1
            continue
        # Stash matched highlight keywords on the object for the notifier.
        listing.highlights = _matches(listing.title, highlight)
        kept.append(listing)
    return kept, dropped
