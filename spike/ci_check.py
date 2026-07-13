"""Milestone-0 decision gate v2: what works from a GitHub Actions runner IP?

Learned from v1: runners get HTTP 200 but a stripped page (no listings, no
doc_id) while the lsd token survives. This version probes the fallbacks:

  1. GET search page x3 (fresh session each) -> listings? doc_id? page markers?
  2. POST GraphQL with page doc_id if found, else HARDCODED doc_id and
     synthetic variables -> listings?
  3. GET item page (known id fallback)       -> coords + description?

Optional env FB_COOKIES ("datr=...; sb=...") — anonymous logged-out device
cookies, no account attached — to test whether device reputation unlocks
results from datacenter IPs. Saves fetched HTML/JSON to SPIKE_OUT_DIR for
artifact upload.

Exit 0 if any search path yields listings AND item enrichment works.
"""

import copy
import json
import os
import re
import sys
import time

import requests

from fetch_graphql import HEADERS, PRELOADER_RE, brace_match, find_listings

PAGE_URL = "https://www.facebook.com/marketplace/liverpool/search?query=washing%20machine"
OUT_DIR = os.environ.get("SPIKE_OUT_DIR", ".")

# From a successful local extraction (2026-07-13). Rotates over time; the
# scraper re-extracts it from the page whenever possible.
FALLBACK_DOC_ID = "27769277389363955"

# Minimal viable variables for CometMarketplaceSearchContentContainerQuery,
# shape copied from the page-embedded preloader.
SYNTHETIC_VARIABLES = {
    "buyLocation": {"latitude": 53.4084, "longitude": -2.9916},
    "contextual_data": None,
    "count": 24,
    "cursor": None,
    "params": {
        "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": "washing machine"},
        "browse_request_params": {
            "commerce_enable_local_pickup": True,
            "commerce_enable_shipping": True,
            "commerce_search_and_rp_available": True,
            "commerce_search_and_rp_category_id": [],
            "commerce_search_and_rp_condition": None,
            "commerce_search_and_rp_ctime_days": None,
            "filter_location_latitude": 53.4084,
            "filter_location_longitude": -2.9916,
            "filter_price_lower_bound": 0,
            "filter_price_upper_bound": 5000,
            "filter_radius_km": 65,
            "commerce_search_sort_by": "CREATION_TIME_DESCEND",
        },
        "custom_request_params": {
            "browse_context": None,
            "contextual_filters": [],
            "referral_code": None,
            "referral_ui_component": None,
            "saved_search_strid": None,
            "search_vertical": "C2C",
            "seo_url": None,
            "serp_landing_settings": {"virtual_category_id": ""},
            "surface": "SEARCH",
            "virtual_contextual_filters": [],
        },
    },
    "savedSearchID": None,
    "savedSearchQuery": "washing machine",
    "scale": 2,
    "shouldDeferNonCritical": False,
    "shouldIncludePopularSearches": True,
    "topicPageParams": {"location_id": "liverpool", "url": None},
    "__relay_internal__pv__GHLShouldChangeMarketplaceSponsoredDataFieldNamerelayprovider": False,
}

# Known-live listing id captured locally today, used if search yields nothing.
FALLBACK_ITEM_ID = "1557130949452080"


def new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    raw = os.environ.get("FB_COOKIES", "").strip()
    for pair in raw.split(";"):
        if "=" in pair:
            k, _, v = pair.strip().partition("=")
            s.cookies.set(k, v, domain=".facebook.com")
    return s


def parse_embedded(html):
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
    return listings


def page_markers(html):
    title = re.search(r"<title[^>]*>([^<]*)</title>", html)
    return {
        "title": title.group(1)[:60] if title else None,
        "log_in_strings": html.count("Log in"),
        "consent": html.count("consent"),
        "marketplace_strings": html.count("marketplace_listing_title"),
    }


def check_search_page(session, attempt):
    resp = session.get(PAGE_URL, timeout=30)
    html = resp.text
    with open(os.path.join(OUT_DIR, f"page_attempt{attempt}.html"), "w") as f:
        f.write(html)
    lsd = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
    pre = PRELOADER_RE.search(html)
    listings = parse_embedded(html)
    print(
        f"1.{attempt} search page: status={resp.status_code} bytes={len(html)} "
        f"listings={len(listings)} lsd={bool(lsd)} doc_id={bool(pre)} {page_markers(html)}"
    )
    variables = json.loads(brace_match(html, pre.end())) if pre else None
    return (lsd.group(1) if lsd else None), (pre.group(1) if pre else None), variables, listings


def check_graphql(session, lsd, doc_id, variables, label):
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
    with open(os.path.join(OUT_DIR, f"graphql_{label}.txt"), "w") as f:
        f.write(resp.text[:200000])
    listings = {}
    for line in resp.text.splitlines():
        line = line.removeprefix("for (;;);").strip()
        if line.startswith("{"):
            try:
                find_listings(json.loads(line), listings)
            except json.JSONDecodeError:
                continue
    err = re.search(r'"errors":\[\{"message":"([^"]*)"', resp.text)
    print(
        f"2. graphql POST ({label}): status={resp.status_code} bytes={len(resp.text)} "
        f"listings={len(listings)} error={err.group(1) if err else None}"
    )
    return listings


def check_item_page(session, listing_id):
    resp = session.get(f"https://www.facebook.com/marketplace/item/{listing_id}/", timeout=30)
    html = resp.text
    with open(os.path.join(OUT_DIR, "item_page.html"), "w") as f:
        f.write(html)
    coords = re.search(r'"location":\{"latitude":([0-9.-]+),"longitude":([0-9.-]+)', html)
    desc = '"redacted_description"' in html
    print(
        f"3. item page {listing_id}: status={resp.status_code} bytes={len(html)} "
        f"coords={coords.groups() if coords else None} description={desc}"
    )
    return bool(coords)


def main():
    print(f"FB_COOKIES set: {bool(os.environ.get('FB_COOKIES'))}")
    lsd = doc_id = variables = None
    page_listings = {}
    session = new_session()

    for attempt in range(1, 4):
        l, d, v, listings = check_search_page(session, attempt)
        lsd = lsd or l
        doc_id = doc_id or d
        variables = variables or v
        page_listings.update(listings)
        if listings:
            break
        time.sleep(5)
        session = new_session()

    gql_listings = {}
    if lsd:
        time.sleep(3)
        gql_listings = check_graphql(
            session,
            lsd,
            doc_id or FALLBACK_DOC_ID,
            variables or SYNTHETIC_VARIABLES,
            "page-extracted" if doc_id else "hardcoded-fallback",
        )
    else:
        print("2. graphql POST: skipped (no lsd at all)")

    time.sleep(3)
    all_listings = {**page_listings, **gql_listings}
    item_id = next(iter(all_listings), FALLBACK_ITEM_ID)
    item_ok = check_item_page(session, item_id)

    search_ok = bool(all_listings)
    print(f"\nRESULT: page={len(page_listings)} graphql={len(gql_listings)} "
          f"search={'OK' if search_ok else 'BLOCKED'} item={'OK' if item_ok else 'BLOCKED'}")
    sys.exit(0 if (search_ok and item_ok) else 1)


if __name__ == "__main__":
    main()
