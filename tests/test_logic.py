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


def test_distance_miles_converted_to_km():
    # EBAY_GB really returns miles (live-verified 2026-10): London reads ~180 'mi'.
    s = {"itemId": "1", "distanceFromPickupLocation": {"unitOfMeasure": "mi", "value": "35"}}
    assert _to_listing(s, "x").distance_km == 56.3
    assert _to_listing({"itemId": "2"}, "x").distance_km is None


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


def test_repair_only_keeps_only_faulty():
    fcfg = {"fault_keywords": ["faulty", "spares", "not working"]}
    scfg = {"repair_only": True}
    parts = _mk("1", "Sage Bambino coffee machine", cond="For parts or not working")
    parts.raw["conditionId"] = "7000"
    listings = [
        parts,
        _mk("2", "Sage Bambino FAULTY no steam"),            # Used + fault word
        _mk("3", "Sage Bambino Plus, good condition"),        # Used, working → drop
        _mk("4", "Ninja coffee machine spares or repairs"),
    ]
    kept, dropped = filters.apply(listings, fcfg, scfg)
    assert [l.item_id for l in kept] == ["1", "2", "4"] and dropped == 1


def test_premium_price_tier():
    scfg = {"max_price": 50, "premium": {"max_price": 200, "keywords": ["barista pro", "oracle"]}}
    listings = [
        _mk("1", "Sage Barista Pro faulty", price=12000),     # premium, under £200
        _mk("2", "Sage Bambino faulty", price=12000),         # not premium, over £50
        _mk("3", "Sage Bambino faulty", price=4500),
        _mk("4", "Sage the Oracle broken", price=25000),      # premium, over £200
        _mk("5", "Sage Bambino faulty", price=5270),          # £50 + buyer fee → in
        _mk("6", "Sage Bambino faulty", price=5280),          # just over → out
    ]
    kept, dropped = filters.apply(listings, {}, scfg)
    assert [l.item_id for l in kept] == ["1", "3", "5"] and dropped == 3
    assert kept[0].max_price_minor == 20870 and kept[1].max_price_minor == 5270


def test_multiple_premium_tiers_highest_match_wins():
    scfg = {"max_price": 50, "premium": [
        {"max_price": 200, "keywords": ["barista touch"]},
        {"max_price": 150, "keywords": ["impress"]},
    ]}
    assert filters.price_cap("Sage Barista Express Impress faulty", scfg) == 150
    assert filters.price_cap("Sage Barista Touch Impress faulty", scfg) == 200
    assert filters.price_cap("Sage Bambino faulty", scfg) == 50


def test_part_exclude_spares_whole_word_only():
    cfg = {"exclude_keywords": [" part "]}
    listings = [
        _mk("1", "Sage Duo-Temp Pro / Bottom Case Part Only"),   # a component
        _mk("2", "Dyson V11 motor part"),                         # last word
        _mk("3", "Sage Barista Pro For Parts, Untested"),         # whole broken unit
        _mk("4", "Ninja Luxe Café (For Parts)"),
        _mk("5", "Dyson V10 compartment, comes apart"),
    ]
    kept, _ = filters.apply(listings, cfg)
    assert [l.item_id for l in kept] == ["3", "4", "5"]


def test_auction_fields_use_current_bid():
    summary = {
        "itemId": "v1|9|0", "title": "Sage Barista Express",
        "buyingOptions": ["FIXED_PRICE", "AUCTION"],
        "price": {"value": "167.10", "currency": "GBP"},          # the BIN price
        "currentBidPrice": {"value": "94.30", "currency": "GBP"},
        "itemEndDate": "2026-10-11T12:40:27.000Z",
    }
    x = _to_listing(summary, "sage")
    assert x.is_auction and x.price_minor == 9430
    assert x.end_date == "2026-10-11T12:40:27.000Z"

    bin_only = _to_listing({**summary, "buyingOptions": ["FIXED_PRICE"]}, "sage")
    assert not bin_only.is_auction and bin_only.price_minor == 16710


def test_fault_query_or_group():
    from src.main import fault_query
    assert fault_query("sage", ["faulty", "not working"]) == 'sage (faulty, "not working")'
    assert fault_query(None, ["broken"]) == "(broken)"


def test_notifier_format_is_minimal():
    row = {
        "item_id": "1", "title": "Sage <Bambino> & milk jug", "price_minor": 3000,
        "currency": "GBP", "condition": "For parts or not working", "location": "Bootle",
        "distance_km": 5.0, "url": "https://www.ebay.co.uk/itm/1",
        "image_url": None, "search_query": "sage",
    }
    msg = notifier._format(row, pickup_km=60)
    assert msg.startswith("<b>£30 — ")
    assert "&lt;Bambino&gt; &amp; milk jug" in msg               # HTML-escaped
    assert '<a href="https://www.ebay.co.uk/itm/1">' in msg      # title links to listing
    assert msg.endswith("📍 Close by")
    for noise in ("km", "Bootle", "parts", "search"):
        assert noise not in msg

    far = notifier._format({**row, "distance_km": 200.0, "price_minor": 1250}, pickup_km=60)
    assert "Close by" not in far and "£12.50" in far
    assert "Close by" not in notifier._format({**row, "distance_km": None}, pickup_km=60)

    ending = notifier._format_ending(row, 42, pickup_km=60)
    assert ending.startswith("⏰ Auction ends in 42 min\n<b>£30")
    assert notifier._format_ending(row, 94).startswith("⏰ Auction ends in 1h 34m\n")
    assert notifier._format_ending(row, 120).startswith("⏰ Auction ends in 2h 00m\n")


def test_auction_check_decisions():
    from datetime import datetime, timedelta, timezone
    from src import auctions
    from src.ebay import EbayError

    class FakeClient:
        def __init__(self, result):
            self.result = result

        def get_item(self, item_id):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    soon = datetime.now(timezone.utc) + timedelta(minutes=90)
    iso = soon.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    row = lambda: {"item_id": "1", "title": "Sage Bambino", "price_minor": 3000,
                   "end_date": soon, "max_price_minor": 5270}
    live = lambda bid: {"buyingOptions": ["AUCTION"], "itemEndDate": iso,
                        "currentBidPrice": {"value": bid}}

    def run(result):
        r = row()
        return auctions.check(None, FakeClient(result), None, r, dry=True), r

    status, r = run(live("45.00"))
    assert status == "alert" and r["price_minor"] == 4500         # live bid shown
    assert run(live("60.00"))[0] == "over_budget"                 # bid passed £52.70
    assert run(None)[0] == "ended"                                # 404 from eBay
    status, r = run(EbayError("eBay down"))
    assert status == "alert" and r["price_minor"] == 3000         # stored timing + price


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"OK — {len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
