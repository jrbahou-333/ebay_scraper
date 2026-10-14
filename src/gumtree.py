"""Gumtree search scraper — the only module that knows Gumtree's page shape.

Gumtree has no public API, so this parses the server-rendered search page. Like
src/ebay.py, everything downstream just sees a `Listing` (source="gumtree").
Run `python -m src.gumtree --probe [i]` to see what search i returns, unfiltered.

What live probing (2026-10) established:

  * Reachable from GitHub Actions' datacenter IPs (unlike Facebook): 200s with
    full results on repeated runs. archive/gumtree_probe/ is the gate probe.
  * Gumtree applies the radius (`distance`, MILES from `search_location`) and
    `max_price` (whole £) server-side, so no client-side radius loop like eBay's.
  * Each result is `<div data-q="search-result">` holding an `<a
    data-q="search-result-anchor" href="/p/<category>/<slug>/<numeric id>">`,
    then `data-q` hooks tile-title / tile-description / tile-location /
    tile-price. These are QA hooks, steadier than the generated CSS classes.
  * When a search has few matches, Gumtree appends a `data-q="nearby-results"`
    section ("Results from outside your search … in your extended area") with
    tiles in the SAME markup from far outside the radius. parse_results cuts
    the page there, or a 30-mile search would alert Sheffield as "Close by".
  * No condition field, no auctions, no buyer fee. There's no per-item distance
    either: every result is within the requested radius.
  * An empty search still renders the results page (`data-q="srp"`) with no
    tiles. A page past the end 303-redirects. A 200 without the results page is
    treated as an error (layout change or a block), never as "no results".
  * BOT CHECK: Gumtree sits behind Radware Bot Manager. About a dozen requests
    in a few minutes from one IP got HTTP 247 + a JavaScript challenge page
    (`window.rbzns`, `/kramericaindustries...js`) instead of results. We never
    try to solve it: it raises GumtreeError, the eBay side carries on, and the
    per-site dead-man switch warns if it persists. Keep request volume tiny (a few
    per run, REQUEST_GAP_S apart) and don't loop probes against it.
"""

import html
import re
import sys
import time

import requests

from .ebay import Listing

SEARCH_URL = "https://www.gumtree.com/search"
SITE = "https://www.gumtree.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

PAGE_SIZE = 25   # tiles per full page (live-observed)
MAX_PAGES = 3    # safety cap; local, price-capped searches rarely fill one page
REQUEST_GAP_S = 2.0  # polite delay between requests to Gumtree


class GumtreeError(RuntimeError):
    """Raised when Gumtree errors, blocks us, or the page shape has changed."""


def _text(fragment: str | None) -> str | None:
    """Inner HTML → plain text, whitespace collapsed."""
    if fragment is None:
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split()) or None


def _hook(tile: str, q: str) -> str | None:
    """Text of the element carrying data-q="<q>" (it never nests its own tag)."""
    m = re.search(r'<(\w+)[^>]*data-q="' + q + r'"[^>]*>(.*?)</\1>', tile, re.S)
    return _text(m.group(2)) if m else None


def _to_pence(price_text: str | None) -> int | None:
    """'£ 1,200' / '£40' / '£12.50' → pence. None for 'Free', 'Swap' etc."""
    m = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", price_text or "")
    return round(float(m.group(1).replace(",", "")) * 100) if m else None


def parse_results(page: str, label: str) -> list[Listing]:
    """All result tiles on a search page, as Listings. Tiles missing an id or
    title (adverts, layout surprises) are skipped."""
    # Sparse searches get padded with "Results from outside your search" (e.g.
    # Sheffield on a 30-mile Liverpool search). Only tiles before that are ours.
    nearby = page.find('data-q="nearby-results"')
    if nearby != -1:
        page = page[:nearby]
    page = re.sub(r"<style[^>]*>.*?</style>", "", page, flags=re.S)  # inline CSS bloat
    starts = [m.start() for m in re.finditer(r'data-q="search-result"', page)]
    listings = []
    for i, start in enumerate(starts):
        tile = page[start:starts[i + 1] if i + 1 < len(starts) else None]
        href = re.search(r'href="(/p/[^"]*?/(\d+))"', tile)
        title = _hook(tile, "tile-title")
        if not (href and title):
            continue
        img = re.search(r'<img[^>]*\ssrc="(https://[^"]+)"', tile)
        description = _hook(tile, "tile-description")
        listings.append(Listing(
            item_id=f"gt:{href.group(2)}",  # prefixed: never collides with an eBay id
            title=title,
            price_minor=_to_pence(_hook(tile, "tile-price")),
            currency="GBP",
            condition=None,
            location=_hook(tile, "tile-location"),
            distance_km=None,
            url=SITE + html.unescape(href.group(1)),
            image_url=html.unescape(img.group(1)) if img else None,
            origin_date=None,
            search_query=label,
            raw={"source": "gumtree", "description": description},
            source="gumtree",
        ))
    return listings


class GumtreeClient:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
        self._last_request = 0.0
        self._blocked: str | None = None  # set by a bot check: no more requests this run

    def _get(self, params: dict) -> requests.Response:
        if self._blocked:
            raise GumtreeError(f"skipped: {self._blocked}")
        wait = self._last_request + REQUEST_GAP_S - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            # No redirects: a page past the last one 303s back to page 1.
            return self._session.get(SEARCH_URL, params=params, timeout=30,
                                     allow_redirects=False)
        except requests.RequestException as e:
            raise GumtreeError(f"request failed: {e}") from e
        finally:
            self._last_request = time.time()

    def search(self, *, category: str, max_price: int, postcode: str, radius_miles,
               query: str | None = None, label: str | None = None,
               max_pages: int = MAX_PAGES) -> list[Listing]:
        """Listings in `category` within `radius_miles` of `postcode`, newest first."""
        label = label or query or category
        params = {
            "search_category": category,
            "search_location": postcode,
            "distance": str(radius_miles),
            "max_price": str(int(max_price)),
            "sort": "date",
        }
        if query:
            params["q"] = query
        by_id: dict[str, Listing] = {}
        for page in range(1, max_pages + 1):
            resp = self._get({**params, "page": str(page)} if page > 1 else params)
            if 300 <= resp.status_code < 400 and page > 1:
                break  # past the last page
            if resp.status_code == 247 or "rbzns" in resp.text[:2000]:
                self._blocked = (f"bot check (Radware challenge, HTTP {resp.status_code}) "
                                 "— backing off until next run")
                raise GumtreeError(f"search({label!r}): {self._blocked}")
            if resp.status_code != 200:
                raise GumtreeError(f"search({label!r}) page {page} failed ({resp.status_code})")
            if 'data-q="srp"' not in resp.text:
                raise GumtreeError(
                    f"search({label!r}) page {page}: no results page in the response "
                    "(blocked, or Gumtree changed its layout)")
            tiles = parse_results(resp.text, label)
            for x in tiles:
                by_id.setdefault(x.item_id, x)
            if len(tiles) < PAGE_SIZE:
                break
        else:
            print(f"  ! {label!r} gumtree: hit the {max_pages}-page cap; "
                  "oldest results were cut — narrow the query or radius")
        return list(by_id.values())


def _probe():
    """Run search i's Gumtree side (default 0) and print the raw results."""
    from . import config
    from .main import gumtree_categories, search_gumtree

    cfg = config.load_config()
    s = cfg["searches"][int(sys.argv[2]) if len(sys.argv) > 2 else 0]
    g = s.get("gumtree")
    if not g:
        print(f"search {s.get('name')!r} has no gumtree: block")
        sys.exit(2)
    print(f"Probing Gumtree: {s.get('name')!r} categories={gumtree_categories(g)} "
          f"query={s.get('query')!r} radius={g.get('radius_miles')}mi")
    try:
        listings = search_gumtree(GumtreeClient(), cfg, s, s.get("name") or "probe")
    except GumtreeError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print(f"\nOK — {len(listings)} raw listings (before keyword filters):\n")
    for x in listings:
        print(f"  {x.price_str:>7} | {(x.location or '?'):<28.28} | {x.title:.50}")
        print(f"          {x.url}")
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        _probe()
    else:
        print("usage: python -m src.gumtree --probe [search_index]")
        sys.exit(2)
