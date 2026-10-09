"""eBay Browse API client — the only module that knows eBay's request/response shape.

Everything downstream sees a `Listing`. Run `python -m src.ebay --probe` to
validate credentials + the search contract end-to-end (Milestone 0).

Two hard-won facts drive the odd-looking request shape (see git history / the
Milestone-0 diagnostics):

  * Locality — eBay's Browse API silently ignores the `pickup*` radius filters on
    standard Buy API access (a deliberately invalid postcode returns identical
    results). So instead we send the buyer's location in the `X-EBAY-C-ENDUSERCTX`
    header, which populates `distanceFromPickupLocation`; sort by `distance`
    (nearest first); and enforce the radius *client-side*, paging until an item
    falls outside it. The distance value is coarse (nearby items all report a
    floored value) but reliably separates local from non-local at the boundary.

  * Relevance — keyword search for appliances is ~90% spare parts (a "washing
    machine" search is 82–94% category 99697 "Washing Machine & Dryer Parts").
    So we search by whole-appliance *category_ids*, not keywords. A `query` may
    still be supplied to refine within a category.
"""

import base64
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field

import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
SCOPE = "https://api.ebay.com/oauth/api_scope"

PAGE_SIZE = 50
MAX_PAGES = 6  # safety cap; we normally stop earlier, at the radius boundary


@dataclass
class Listing:
    item_id: str
    title: str
    price_minor: int | None
    currency: str
    condition: str | None
    location: str | None
    distance_km: float | None
    url: str
    image_url: str | None
    origin_date: str | None
    search_query: str  # the search label this came from
    raw: dict = field(repr=False, default_factory=dict)
    highlights: list[str] = field(default_factory=list)  # matched highlight keywords (set by filters)
    is_auction: bool = False         # buyingOptions has AUCTION (possibly alongside a BIN price)
    end_date: str | None = None      # itemEndDate — set for auctions
    max_price_minor: int | None = None  # the price cap that admitted it (set by filters)

    @property
    def price_str(self) -> str:
        if self.price_minor is None:
            return "—"
        pounds = self.price_minor / 100
        sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(self.currency, "")
        return f"{sym}{pounds:.0f}" if pounds == int(pounds) else f"{sym}{pounds:.2f}"


class EbayError(RuntimeError):
    """Raised for auth/HTTP/contract failures against the eBay API."""


def _build_filter(*, max_price, condition_ids, country) -> str:
    """Compose the eBay Browse `filter` value (a single comma-separated string).

    Only server-honoured filters go here: condition, price (needs priceCurrency),
    and item location country. The radius is NOT here — eBay ignores pickup* — it
    is enforced client-side in `search()`.
    """
    conds = "|".join(str(c) for c in condition_ids)
    return ",".join(
        [
            f"conditionIds:{{{conds}}}",
            f"price:[..{max_price}]",
            "priceCurrency:GBP",
            f"itemLocationCountry:{country}",
        ]
    )


def _context_header(postcode: str, country: str) -> str:
    """X-EBAY-C-ENDUSERCTX value that sets the buyer location, enabling distance.

    eBay wants `contextualLocation=country=<C>,zip=<postcode>` with the inner
    delimiters percent-encoded.
    """
    inner = f"country={country},zip={postcode}"
    return "contextualLocation=" + urllib.parse.quote(inner, safe="")


def _to_pence(price: dict | None) -> int | None:
    if not price or price.get("value") is None:
        return None
    try:
        return round(float(price["value"]) * 100)
    except (TypeError, ValueError):
        return None


def _location_str(item_location: dict | None) -> str | None:
    if not item_location:
        return None
    for key in ("postalCode", "city", "stateOrProvince", "country"):
        if item_location.get(key):
            return item_location[key]
    return None


KM_PER_MILE = 1.609344


def _distance_km(summary: dict) -> float | None:
    """distanceFromPickupLocation in km. EBAY_GB reports MILES (unitOfMeasure 'mi',
    rounded to 5) — before 2026-10 this was misread as km, so old "40km"/"60km"
    radii were really 40/60 miles."""
    dist = summary.get("distanceFromPickupLocation")
    if isinstance(dist, dict) and dist.get("value") is not None:
        try:
            value = float(dist["value"])
        except (TypeError, ValueError):
            return None
        if str(dist.get("unitOfMeasure", "")).lower() in ("mi", "mile", "miles"):
            value *= KM_PER_MILE
        return round(value, 1)
    return None


def _is_auction(summary: dict) -> bool:
    return "AUCTION" in (summary.get("buyingOptions") or [])


def current_price(summary: dict) -> int | None:
    """Price in pence that a buyer faces now. For an auction that's the current
    bid: eBay's `price` on an auction-with-BIN is the Buy It Now price instead."""
    if _is_auction(summary) and summary.get("currentBidPrice"):
        return _to_pence(summary["currentBidPrice"])
    return _to_pence(summary.get("price"))


def _to_listing(summary: dict, label: str) -> Listing:
    image = (summary.get("image") or {}).get("imageUrl")
    if not image and summary.get("thumbnailImages"):
        image = summary["thumbnailImages"][0].get("imageUrl")
    return Listing(
        item_id=summary["itemId"],
        title=summary.get("title", "").strip(),
        price_minor=current_price(summary),
        currency=(summary.get("price") or {}).get("currency", "GBP"),
        condition=summary.get("condition"),
        location=_location_str(summary.get("itemLocation")),
        distance_km=_distance_km(summary),
        url=summary.get("itemWebUrl", ""),
        image_url=image,
        # itemOriginDate is the newlyListed sort key; older responses use itemCreationDate.
        origin_date=summary.get("itemOriginDate") or summary.get("itemCreationDate"),
        search_query=label,
        raw=summary,
        is_auction=_is_auction(summary),
        end_date=summary.get("itemEndDate"),
    )


class EbayClient:
    """eBay Browse client.

    Auth is either OAuth2 client-credentials (mints its own ~2h application token
    from client_id/secret) or a pre-supplied `oauth_token` (a static token, e.g.
    for validation before the Cert ID / client secret is available).
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        marketplace: str = "EBAY_GB",
        oauth_token: str | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self.marketplace = marketplace
        self._static_token = oauth_token
        self._session = requests.Session()
        self._token = None
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._static_token:
            return self._static_token
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not (self._client_id and self._client_secret):
            raise EbayError(
                "no eBay credentials: set EBAY_CLIENT_ID + EBAY_CLIENT_SECRET, "
                "or EBAY_OAUTH_TOKEN for a static token"
            )
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        resp = self._session.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if resp.status_code != 200:
            raise EbayError(
                f"OAuth token request failed ({resp.status_code}): {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + payload.get("expires_in", 7200)
        return self._token

    def search(
        self,
        *,
        category_ids,
        max_price,
        condition_ids,
        postcode: str,
        country: str,
        radius_km,
        query: str | None = None,
        label: str | None = None,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> list[Listing]:
        """Return whole-appliance Listings within `radius_km`, nearest first.

        `radius_km=None` means UK-wide (no radius cut-off; only `max_pages` bounds it).
        eBay's Browse API accepts only ONE category per request, so we query each
        category id separately and merge (deduping by item id — a listing can only
        live in one category anyway, but a `query` refinement could overlap).
        """
        label = label or query or ",".join(str(c) for c in category_ids)
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "X-EBAY-C-ENDUSERCTX": _context_header(postcode, country),
            "Content-Type": "application/json",
        }
        filter_str = _build_filter(
            max_price=max_price, condition_ids=condition_ids, country=country
        )
        by_id: dict[str, Listing] = {}
        for cat in category_ids:
            for listing in self._search_category(
                cat, filter_str, headers, radius_km, query, label, page_size, max_pages
            ):
                by_id.setdefault(listing.item_id, listing)
        return list(by_id.values())

    def _search_category(
        self, category_id, filter_str, headers, radius_km, query, label, page_size, max_pages
    ) -> list[Listing]:
        """Page one category's distance-sorted results, stopping at the radius edge."""
        kept: list[Listing] = []
        for page in range(max_pages):
            params = {
                "sort": "distance",
                "limit": str(page_size),
                "offset": str(page * page_size),
                "filter": filter_str,
                "category_ids": str(category_id),
            }
            if query:
                params["q"] = query
            resp = self._session.get(SEARCH_URL, params=params, headers=headers, timeout=30)
            if resp.status_code != 200:
                raise EbayError(
                    f"search({label!r} cat={category_id}) failed "
                    f"({resp.status_code}): {resp.text[:400]}"
                )
            summaries = resp.json().get("itemSummaries") or []
            if not summaries:
                break
            stop = False
            for s in summaries:
                listing = _to_listing(s, label)
                if (radius_km is not None and listing.distance_km is not None
                        and listing.distance_km > radius_km):
                    stop = True  # distance-sorted, so everything after is further too
                    break
                kept.append(listing)
            if stop or len(summaries) < page_size:
                break
        else:
            print(f"  ! {label!r} cat={category_id}: hit the {max_pages}-page cap; "
                  "furthest results were cut — narrow the query or radius")
        return kept

    def get_item(self, item_id: str) -> dict | None:
        """Fetch one item's live detail (current bid, end date). None if it's gone."""
        resp = self._session.get(
            ITEM_URL + urllib.parse.quote(item_id, safe=""),
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            },
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise EbayError(f"get_item({item_id}) failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()


def _make_probe_client(cfg):
    """Build a client from whatever eBay auth is available (token or id+secret)."""
    from . import config

    marketplace = cfg.get("marketplace", "EBAY_GB")
    token = os.environ.get("EBAY_OAUTH_TOKEN")
    if token:
        print("(using EBAY_OAUTH_TOKEN — static token)")
        return EbayClient(marketplace=marketplace, oauth_token=token)
    env = config.require_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    return EbayClient(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"], marketplace)


def _probe():
    """Milestone-0 gate: token + one live search (with its per-search overrides), printed."""
    from . import config

    config.load_env()
    cfg = config.load_config()
    loc = cfg["location"]
    s = cfg["searches"][int(sys.argv[2]) if len(sys.argv) > 2 else 0]

    from .main import search_one

    client = _make_probe_client(cfg)
    radius = s["radius_km"] if "radius_km" in s else loc["radius_km"]
    print(
        f"Probing eBay: {s.get('name', s.get('query'))!r} "
        f"categories={s['category_ids']} max=£{s['max_price']} premium={s.get('premium')} "
        f"radius={radius or 'UK-wide'}km postcode={loc['postcode']} repair_only={s.get('repair_only', False)}"
    )
    try:
        listings = search_one(client, cfg, s, s.get("name") or "probe")
    except EbayError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)

    print(f"\nOK — {len(listings)} raw listings (before keyword filters):\n")
    for x in sorted(listings, key=lambda l: (l.distance_km is None, l.distance_km or 0)):
        dist = f"{x.distance_km}km" if x.distance_km is not None else "?"
        kind = f"AUCTION ends {x.end_date}" if x.is_auction else "BIN"
        print(f"  {x.price_str:>7} | {dist:>7} | {(x.condition or '?'):<20.20} | {kind:<36} | {x.title:.44}")
        print(f"          {x.url}")
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        _probe()
    else:
        print("usage: python -m src.ebay --probe [search_index]")
        sys.exit(2)
