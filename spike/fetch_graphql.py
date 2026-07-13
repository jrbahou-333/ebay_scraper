"""Milestone-0 spike, technique (b): logged-out GraphQL POST with custom radius.

Flow: GET the public search page (cookies + LSD token + queryID + variables
shape), then POST the same CometMarketplaceSearchContentContainerQuery back to
/api/graphql/ with our own filters: radius_km=25, newest-first, price cap.

Usage: python spike/fetch_graphql.py [query] [max_price_gbp] [radius_km]
"""

import copy
import json
import re
import sys

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

PRELOADER_RE = re.compile(
    r'"preloaderID":"adp_CometMarketplaceSearchContentContainerQueryRelayPreloader_[^"]*",'
    r'"queryID":"(\d+)","variables":'
)


def brace_match(text, start):
    """Return the JSON object string starting at text[start] == '{'."""
    depth = 0
    for j in range(start, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    raise ValueError("unbalanced braces")


def find_listings(obj, out):
    if isinstance(obj, dict):
        if "marketplace_listing_title" in obj and "id" in obj:
            out[obj["id"]] = obj
        for v in obj.values():
            find_listings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_listings(v, out)


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "washing machine"
    max_price = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    radius_km = int(sys.argv[3]) if len(sys.argv) > 3 else 25

    session = requests.Session()
    session.headers.update(HEADERS)

    page_url = (
        "https://www.facebook.com/marketplace/liverpool/search?query="
        + requests.utils.quote(query)
    )
    resp = session.get(page_url, timeout=30)
    html = resp.text
    print(f"GET page: status={resp.status_code} bytes={len(html)}")

    lsd = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
    pre = PRELOADER_RE.search(html)
    if not lsd or not pre:
        print(f"extraction failed: lsd={bool(lsd)} preloader={bool(pre)}")
        sys.exit(1)
    lsd = lsd.group(1)
    doc_id = pre.group(1)
    variables = json.loads(brace_match(html, pre.end()))
    print(f"lsd={lsd} doc_id={doc_id}")

    v = copy.deepcopy(variables)
    brp = v["params"]["browse_request_params"]
    brp["filter_radius_km"] = radius_km
    brp["filter_price_lower_bound"] = 0
    brp["filter_price_upper_bound"] = max_price * 100  # pence
    brp["commerce_search_sort_by"] = "CREATION_TIME_DESCEND"
    v["count"] = 24
    v["cursor"] = None

    post = session.post(
        "https://www.facebook.com/api/graphql/",
        data={
            "lsd": lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "CometMarketplaceSearchContentContainerQuery",
            "server_timestamps": "true",
            "doc_id": doc_id,
            "variables": json.dumps(v),
        },
        headers={
            "x-fb-lsd": lsd,
            "x-fb-friendly-name": "CometMarketplaceSearchContentContainerQuery",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.facebook.com",
            "referer": page_url,
        },
        timeout=30,
    )
    body = post.text
    print(f"POST graphql: status={post.status_code} bytes={len(body)}")

    # Response can be JSON, `for (;;);`-prefixed, or JSONL when deferred.
    listings = {}
    for line in body.splitlines():
        line = line.removeprefix("for (;;);").strip()
        if not line.startswith("{"):
            continue
        try:
            find_listings(json.loads(line), listings)
        except json.JSONDecodeError:
            continue

    if not listings:
        print("no listings in POST response; first 500 chars:")
        print(body[:500])
        sys.exit(1)

    print(f"parsed listings: {len(listings)}")
    for node in listings.values():
        price = (node.get("listing_price") or {}).get("formatted_amount")
        geo = (node.get("location") or {}).get("reverse_geocode") or {}
        city = geo.get("city") or (geo.get("city_page") or {}).get("display_name")
        print(f"  {price!s:>8} | {city!s:<18} | {node.get('marketplace_listing_title')!s:.60}")
    sys.exit(0)


if __name__ == "__main__":
    main()
