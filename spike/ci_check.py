"""Milestone-0 decision gate: run the three logged-out fetches from wherever
this executes (locally or a GitHub Actions runner) and report what works.

Checks:
  1. GET search page  -> embedded listings + lsd + doc_id extractable
  2. POST GraphQL     -> newest-first listings
  3. GET item page    -> listing coordinates + description present

Exit 0 only if search works (1 or 2) AND item enrichment (3) works.
"""

import copy
import json
import re
import sys
import time

import requests

from fetch_graphql import HEADERS, PRELOADER_RE, brace_match, find_listings

PAGE_URL = "https://www.facebook.com/marketplace/liverpool/search?query=washing%20machine"


def check_search_page(session):
    resp = session.get(PAGE_URL, timeout=30)
    html = resp.text
    lsd = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
    pre = PRELOADER_RE.search(html)
    listings = {}
    marker = 'type="application/json"'
    pos = 0
    while True:
        i = html.find(marker, pos)
        if i == -1:
            break
        start = html.find(">", i) + 1
        end = html.find("</script>", start)
        if start == 0 or end == -1:
            break
        pos = end
        try:
            find_listings(json.loads(html[start:end]), listings)
        except json.JSONDecodeError:
            continue
    print(
        f"1. search page: status={resp.status_code} bytes={len(html)} "
        f"listings={len(listings)} lsd={bool(lsd)} doc_id={bool(pre)}"
    )
    if not (lsd and pre):
        return None, None, None, listings
    variables = json.loads(brace_match(html, pre.end()))
    return lsd.group(1), pre.group(1), variables, listings


def check_graphql(session, lsd, doc_id, variables):
    v = copy.deepcopy(variables)
    brp = v["params"]["browse_request_params"]
    brp["filter_price_upper_bound"] = 5000
    brp["commerce_search_sort_by"] = "CREATION_TIME_DESCEND"
    v["count"] = 24
    v["cursor"] = None
    resp = session.post(
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
            "referer": PAGE_URL,
        },
        timeout=30,
    )
    listings = {}
    for line in resp.text.splitlines():
        line = line.removeprefix("for (;;);").strip()
        if line.startswith("{"):
            try:
                find_listings(json.loads(line), listings)
            except json.JSONDecodeError:
                continue
    print(f"2. graphql POST: status={resp.status_code} listings={len(listings)}")
    return listings


def check_item_page(session, listing_id):
    url = f"https://www.facebook.com/marketplace/item/{listing_id}/"
    resp = session.get(url, timeout=30)
    html = resp.text
    coords = re.search(r'"location":\{"latitude":([0-9.-]+),"longitude":([0-9.-]+)', html)
    desc = '"redacted_description"' in html
    print(
        f"3. item page {listing_id}: status={resp.status_code} bytes={len(html)} "
        f"coords={coords.groups() if coords else None} description={desc}"
    )
    return bool(coords)


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    lsd, doc_id, variables, page_listings = check_search_page(session)
    time.sleep(3)

    gql_listings = {}
    if lsd:
        gql_listings = check_graphql(session, lsd, doc_id, variables)
    else:
        print("2. graphql POST: skipped (no lsd/doc_id)")
    time.sleep(3)

    all_listings = {**page_listings, **gql_listings}
    item_ok = False
    if all_listings:
        item_ok = check_item_page(session, next(iter(all_listings)))
    else:
        print("3. item page: skipped (no listings found)")

    search_ok = bool(all_listings)
    print(f"\nRESULT: search={'OK' if search_ok else 'BLOCKED'} item_enrichment={'OK' if item_ok else 'BLOCKED'}")
    sys.exit(0 if (search_ok and item_ok) else 1)


if __name__ == "__main__":
    main()
