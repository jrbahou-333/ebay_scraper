# Facebook Marketplace spike (archived)

This is the **Milestone-0 validation spike** from the project's original Facebook
Marketplace design. It is not part of the live eBay pipeline — it is kept here as a
proven, working logged-out FB fetcher for a possible **future second source**
(see "Out of scope for v1" in the plan: *FB Marketplace via a self-hosted /
residential runner*).

## What we learned (why we pivoted to eBay)

Logged-out FB Marketplace fetching **works perfectly from a residential IP** but is
**hard-blocked from GitHub Actions' datacenter IPs** — the search page, the GraphQL
API (`"Rate limit exceeded"`), and item pages all return a login/captcha wall. Since
the whole point was a free cloud cron, we switched to eBay's official OAuth Browse
API, which is indifferent to runner IP reputation.

## Files

- `fetch_one.py` — GET the public search page, parse listing JSON embedded in
  `<script type="application/json">` blobs. Works from a home IP.
- `fetch_graphql.py` — scrape the `lsd` token + `doc_id` from the page, then POST
  `CometMarketplaceSearchContentContainerQuery` to `/api/graphql/` with a custom
  radius / newest-first sort. Works from a home IP.
- `ci_check.py` — the decision-gate probe: runs all techniques and reports what
  works. Imports `fetch_graphql`, so run it from **inside this directory**:
  `cd archive/fb_marketplace && python ci_check.py`
- `spike.yml` — the `workflow_dispatch` workflow that ran `ci_check.py` on a GitHub
  runner (how we proved the datacenter-IP block). Reference only — not installed
  under `.github/workflows/`.

## If reviving FB as a source

Run it from an environment with a residential IP (self-hosted runner on a home
machine or a Raspberry Pi). The FB page markup / GraphQL `doc_id` rotate over time,
so expect to re-scrape the `doc_id` from the page each run (already handled in
`fetch_graphql.py`) and to refresh selectors if parsing returns zero.
