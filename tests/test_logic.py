"""Offline regression tests for the parsing/filtering/formatting logic.

No network or DB required. Run either way:
    python -m tests.test_logic      # plain asserts, prints OK
    pytest                          # if pytest is installed
"""

from src import filters, notifier
from src.ebay import Listing, _build_filter, _location_str, _to_listing, _to_pence


def _mk(item_id, title, price=3000, dist=5.0, cond="Used"):
    return Listing(item_id, title, price, "GBP", cond, "Bootle", dist,
                   f"https://www.ebay.co.uk/itm/{item_id}", "https://img/x.jpg",
                   "2026-07-13T09:00:00Z", "washing machine", {"itemId": item_id})


def test_filter_string():
    # No pickup* params: eBay silently ignores them (see src/ebay.py docstring);
    # locality is the ENDUSERCTX header + client-side radius check instead.
    assert _build_filter(
        max_price=50, condition_ids=[3000, 7000], country="GB",
    ) == "conditionIds:{3000|7000},price:[..50],priceCurrency:GBP,itemLocationCountry:GB"


def test_to_pence():
    assert _to_pence({"value": "30.00"}) == 3000
    assert _to_pence({"value": "12.50"}) == 1250
    assert _to_pence(None) is None
    assert _to_pence({"value": None}) is None


def test_location_prefers_postcode_then_city():
    assert _location_str({"city": "Bootle", "postalCode": "L20"}) == "L20"
    assert _location_str({"city": "Bootle"}) == "Bootle"
    assert _location_str({"country": "GB"}) == "GB"
    assert _location_str(None) is None


def test_to_listing_maps_fields_and_trims():
    summary = {
        "itemId": "v1|123|0", "title": "  Hotpoint washing machine  ",
        "price": {"value": "30.00", "currency": "GBP"},
        "condition": "For parts or not working",
        "itemLocation": {"postalCode": "L20 ***", "country": "GB"},
        "distanceFromPickupLocation": {"value": 6.23, "unit": "km"},
        "image": {"imageUrl": "https://i/x.jpg"},
        "itemWebUrl": "https://www.ebay.co.uk/itm/123",
        "itemOriginDate": "2026-07-13T09:10:11.000Z",
    }
    x = _to_listing(summary, "washing machine")
    assert x.item_id == "v1|123|0"
    assert x.title == "Hotpoint washing machine"
    assert x.price_minor == 3000 and x.price_str == "£30"
    assert x.distance_km == 6.2
    assert x.location == "L20 ***"
    assert x.origin_date == "2026-07-13T09:10:11.000Z"


def test_price_str_formats():
    assert _mk("1", "x", price=3000).price_str == "£30"
    assert _mk("1", "x", price=1250).price_str == "£12.50"
    assert _mk("1", "x", price=None).price_str == "—"


def test_filters_exclude_and_highlight():
    cfg = {
        "exclude_keywords": ["door seal", "pcb", "handle"],
        "highlight_keywords": ["faulty", "spares or repair", "not working"],
    }
    listings = [
        _mk("1", "Hotpoint washing machine FAULTY door lock"),
        _mk("2", "Washing machine door seal genuine part"),
        _mk("3", "Bosch washing machine PCB board spare"),
        _mk("4", "Beko washing machine spares or repair"),
        _mk("5", "Indesit washing machine, good working order"),
    ]
    kept, dropped = filters.apply(listings, cfg)
    assert dropped == 2
    assert [l.item_id for l in kept] == ["1", "4", "5"]
    assert kept[0].highlights == ["faulty"]
    assert kept[1].highlights == ["spares or repair"]
    assert kept[2].highlights == []


def test_highlights_for_handles_none():
    cfg = {"highlight_keywords": ["faulty"]}
    assert filters.highlights_for("Faulty dryer", cfg) == ["faulty"]
    assert filters.highlights_for(None, cfg) == []


def test_notifier_format_escapes_and_flags():
    row = {
        "item_id": "1", "title": "Hotpoint <washer> & dryer", "price_minor": 3000,
        "currency": "GBP", "condition": "For parts or not working", "location": "Bootle",
        "distance_km": 6.2, "url": "https://www.ebay.co.uk/itm/1",
        "image_url": None, "search_query": "washing machine", "highlights": ["faulty"],
    }
    msg = notifier._format(row)
    assert msg.startswith("🔧")
    assert "£30" in msg and "6.2 km" in msg
    assert "&lt;washer&gt; &amp; dryer" in msg     # HTML-escaped
    assert '<a href="https://www.ebay.co.uk/itm/1">' in msg   # title links to listing
    assert not msg.splitlines()[-1].startswith("https://")    # no bare URL line

    plain = notifier._format({**row, "highlights": [], "condition": "Used", "price_minor": 1250})
    assert plain.startswith("🏷") and "£12.50" in plain


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"OK — {len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
