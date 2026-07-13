"""eBay Browse API client — the only module that knows eBay's request/response shape.

Everything downstream sees a `Listing`. Run `python -m src.ebay --probe` to
validate credentials + the search contract end-to-end (Milestone 0).
"""

import base64
import sys
import time
from dataclasses import dataclass, field

import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"


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
    search_query: str
    raw: dict = field(repr=False, default_factory=dict)
    highlights: list[str] = field(default_factory=list)  # matched highlight keywords (set by filters)

    @property
    def price_str(self) -> str:
        if self.price_minor is None:
            return "—"
        pounds = self.price_minor / 100
        sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(self.currency, "")
        return f"{sym}{pounds:.0f}" if pounds == int(pounds) else f"{sym}{pounds:.2f}"


class EbayError(RuntimeError):
    """Raised for auth/HTTP/contract failures against the eBay API."""


def _build_filter(*, max_price, condition_ids, postcode, country, radius_km) -> str:
    """Compose the eBay Browse `filter` value (a single comma-separated string).

    Local-pickup radius requires all four pickup* fields together; price requires
    priceCurrency alongside it.
    """
    conds = "|".join(str(c) for c in condition_ids)
    parts = [
        f"conditionIds:{{{conds}}}",
        f"price:[..{max_price}]",
        "priceCurrency:GBP",
        f"pickupCountry:{country}",
        f"pickupPostalCode:{postcode}",
        f"pickupRadius:{radius_km}",
        "pickupRadiusUnit:km",
    ]
    return ",".join(parts)


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
    for key in ("city", "postalCode", "stateOrProvince", "country"):
        if item_location.get(key):
            return item_location[key]
    return None


def _distance_km(summary: dict) -> float | None:
    dist = summary.get("distanceFromPickupLocation")
    if isinstance(dist, dict) and dist.get("value") is not None:
        try:
            return round(float(dist["value"]), 1)
        except (TypeError, ValueError):
            return None
    return None


def _to_listing(summary: dict, query: str) -> Listing:
    image = (summary.get("image") or {}).get("imageUrl")
    if not image and summary.get("thumbnailImages"):
        image = summary["thumbnailImages"][0].get("imageUrl")
    return Listing(
        item_id=summary["itemId"],
        title=summary.get("title", "").strip(),
        price_minor=_to_pence(summary.get("price")),
        currency=(summary.get("price") or {}).get("currency", "GBP"),
        condition=summary.get("condition"),
        location=_location_str(summary.get("itemLocation")),
        distance_km=_distance_km(summary),
        url=summary.get("itemWebUrl", ""),
        image_url=image,
        # itemOriginDate is the newlyListed sort key; older responses use itemCreationDate.
        origin_date=summary.get("itemOriginDate") or summary.get("itemCreationDate"),
        search_query=query,
        raw=summary,
    )


class EbayClient:
    def __init__(self, client_id: str, client_secret: str, marketplace: str = "EBAY_GB"):
        self._client_id = client_id
        self._client_secret = client_secret
        self.marketplace = marketplace
        self._session = requests.Session()
        self._token = None
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
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
        query: str,
        *,
        max_price,
        condition_ids,
        postcode: str,
        country: str,
        radius_km,
        limit: int = 50,
    ) -> list[Listing]:
        """Run one newest-first Browse search; return parsed Listings (possibly empty)."""
        token = self._get_token()
        params = {
            "q": query,
            "sort": "newlyListed",
            "limit": str(limit),
            "filter": _build_filter(
                max_price=max_price,
                condition_ids=condition_ids,
                postcode=postcode,
                country=country,
                radius_km=radius_km,
            ),
        }
        resp = self._session.get(
            SEARCH_URL,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise EbayError(
                f"search({query!r}) failed ({resp.status_code}): {resp.text[:400]}"
            )
        summaries = resp.json().get("itemSummaries") or []
        return [_to_listing(s, query) for s in summaries]


def _probe():
    """Milestone-0 gate: token + one live search, printed for inspection."""
    from . import config

    config.load_env()
    env = config.require_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    cfg = config.load_config()
    loc = cfg["location"]

    query = sys.argv[2] if len(sys.argv) > 2 else "washing machine"
    max_price = int(sys.argv[3]) if len(sys.argv) > 3 else 50

    client = EbayClient(
        env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"], cfg.get("marketplace", "EBAY_GB")
    )
    print(f"Probing eBay: query={query!r} max_price=£{max_price} "
          f"radius={loc['radius_km']}km postcode={loc['postcode']} "
          f"conditions={cfg['condition_ids']}")
    try:
        listings = client.search(
            query,
            max_price=max_price,
            condition_ids=cfg["condition_ids"],
            postcode=loc["postcode"],
            country=loc["country"],
            radius_km=loc["radius_km"],
        )
    except EbayError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)

    print(f"\nOK — {len(listings)} listings:\n")
    for x in listings[:15]:
        dist = f"{x.distance_km}km" if x.distance_km is not None else "?"
        print(f"  {x.price_str:>7} | {dist:>7} | {(x.condition or '?'):<26.26} | {x.title:.50}")
        print(f"          {x.url}")
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        _probe()
    else:
        print("usage: python -m src.ebay --probe [query] [max_price]")
        sys.exit(2)
