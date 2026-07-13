"""Milestone-0 spike: can we fetch FB Marketplace search results logged-out?

Technique (a): GET the public search page and parse listing JSON embedded in
<script type="application/json"> blobs. Prints what it finds and saves the raw
HTML for inspection. Exit code 0 = listings found, 1 = blocked/empty.

Usage: python spike/fetch_one.py [query] [max_price]
"""

import json
import os
import sys

import requests

SCRATCH = os.environ.get("SPIKE_OUT_DIR", ".")

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


def find_listings(obj, out):
    """Recursively collect dicts that look like marketplace listing nodes."""
    if isinstance(obj, dict):
        if "marketplace_listing_title" in obj and "id" in obj:
            out[obj["id"]] = obj
        for v in obj.values():
            find_listings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_listings(v, out)


def extract_json_blobs(html):
    """Yield parsed JSON from every <script type="application/json"> tag."""
    marker = 'type="application/json"'
    pos = 0
    while True:
        i = html.find(marker, pos)
        if i == -1:
            return
        start = html.find(">", i) + 1
        end = html.find("</script>", start)
        if start == 0 or end == -1:
            return
        pos = end
        try:
            yield json.loads(html[start:end])
        except json.JSONDecodeError:
            continue


def summarize(node):
    price = (node.get("listing_price") or {}).get("formatted_amount") or (
        node.get("listing_price") or {}
    ).get("amount")
    geo = (node.get("location") or {}).get("reverse_geocode") or {}
    city = geo.get("city") or (geo.get("city_page") or {}).get("display_name")
    photo = ((node.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
    return {
        "id": node.get("id"),
        "title": node.get("marketplace_listing_title"),
        "price": price,
        "city": city,
        "has_photo": bool(photo),
        "url": f"https://www.facebook.com/marketplace/item/{node.get('id')}/",
    }


def run(label, url):
    print(f"\n=== {label} ===\n    {url}")
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(url, timeout=30, allow_redirects=True)
    html = resp.text
    out_path = os.path.join(SCRATCH, f"fb_{label}.html")
    with open(out_path, "w") as f:
        f.write(html)

    print(f"status={resp.status_code} final_url={resp.url[:100]} bytes={len(html)}")
    for needle in ("marketplace_listing_title", "login_form", '"login"', "checkpoint"):
        print(f"  contains {needle!r}: {html.count(needle)}")

    listings = {}
    for blob in extract_json_blobs(html):
        find_listings(blob, listings)

    print(f"  parsed listings: {len(listings)}")
    for node in list(listings.values())[:8]:
        s = summarize(node)
        print(f"    {s['price']!s:>8} | {s['city']!s:<20} | {s['title']!s:.60}")
    return listings


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "washing machine"
    max_price = sys.argv[2] if len(sys.argv) > 2 else "50"
    q = requests.utils.quote(query)

    base = f"https://www.facebook.com/marketplace/liverpool/search?query={q}&maxPrice={max_price}"
    plain = run("plain", base)
    sorted_ = run("sorted", base + "&sortBy=creation_time_descend&daysSinceListed=7&exact=false")

    ok = bool(plain or sorted_)
    print(f"\nRESULT: {'OK — listings parsed' if ok else 'BLOCKED or empty'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
