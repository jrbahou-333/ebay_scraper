"""Gumtree decision-gate probe: does Gumtree serve search results to this IP?

Run locally (home IP) or in Actions (.github/workflows/gumtree_probe.yml) to see
whether GitHub's datacenter IPs are blocked, as they were for Facebook. Prints one
line per URL; in Actions each line is also a ::notice:: annotation, which the
public GitHub API serves without auth.

    python archive/gumtree_probe/probe.py
"""

import html
import os
import re
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
BASE = "https://www.gumtree.com/search"
COMMON = {"search_location": "L1 8JQ", "distance": "30", "max_price": "200", "sort": "date"}
PROBES = [
    ("sage", {**COMMON, "search_category": "coffee-machines", "q": "sage"}),
    ("ninja", {**COMMON, "search_category": "coffee-machines", "q": "ninja"}),
    ("all-coffee-p2", {**COMMON, "search_category": "coffee-machines", "page": "2"}),
]
BLOCK_MARKERS = ("captcha", "cf-chl", "access denied", "are you a robot", "px-captcha")


def _field(tile: str, q: str) -> str | None:
    m = re.search(r'data-q="' + q + r'"[^>]*>(.*?)</div', tile, re.S)
    return " ".join(html.unescape(re.sub("<[^>]+>", " ", m.group(1))).split()) if m else None


def probe(name: str, params: dict, session: requests.Session) -> bool:
    try:
        resp = session.get(BASE, params=params, timeout=30)
    except requests.RequestException as e:
        report(f"{name}: request error {e!r}")
        return False
    body = resp.text
    starts = [m.start() for m in re.finditer(r'data-q="search-result"', body)]
    tiles = [body[i:i + 6000] for i in starts]
    blocked = [m for m in BLOCK_MARKERS if m in body.lower()]
    sample = "; ".join(f"{_field(t, 'tile-price')} {(_field(t, 'tile-title') or '?')[:40]}"
                       for t in tiles[:3])
    ok = resp.status_code == 200 and len(tiles) > 0
    report(f"{name}: {'PASS' if ok else 'FAIL'} status={resp.status_code} "
           f"bytes={len(body)} tiles={len(tiles)} block_markers={blocked} | {sample}")
    return ok


def report(line: str) -> None:
    print(line)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=gumtree-probe::{line}")


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    results = []
    for name, params in PROBES:
        results.append(probe(name, params, session))
        time.sleep(2)
    report(f"OVERALL: {'PASS' if all(results) else 'FAIL'} ({sum(results)}/{len(results)})")
    return 0  # never fail the job: the annotations carry the verdict


if __name__ == "__main__":
    sys.exit(main())
