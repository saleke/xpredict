# The Odds API supporting prices

The daily worker now has a separate The Odds API v4 price adapter. Existing fixture/result providers remain active. The legacy pipeline remains disabled. Enable supporting prices with `LISA_THE_ODDS_ENABLED=1`; private workspace configuration now enables it under the user's authorization, while the template defaults to disabled.

Credentials come from private `THE_ODDS_API_KEY` or owner-only Admin → Settings → Data Providers → `the_odds_api`. Updates rebuild the adapter on the next worker cycle. The unmetered sports catalog and quota headers are checked before paid requests after startup or replacement. Query authentication follows the documented contract; credential URLs, raw response bodies and exception text never enter reports. Redirects are blocked and requests are not automatically retried.

Defaults are 500 monthly credits, 50 reserved, 14 daily credits, one region (`eu`), and markets `h2h,totals`. A league request costs two credits: up to seven paid league requests per UTC day, and at most 434 credits in 31 days. Requests are limited to fixture-bearing leagues in the calendar feed and active catalog. The cycle deadline/request cap also applies. Least-recently-collected priority persists in storage so partial budgets do not always favor the first league.

Reservations atomically enforce both daily and key-specific subscription limits. Server-reported usage raises the local usage floor. Replacing a key requires fresh quota verification; replacement does not imply fresh account credits or bypass the shared daily cap. Failed/empty attempts retain conservative reservations even if provider billing costs less. Billing renewal is not inferred from calendar boundaries: the persistent subscription usage floor does not automatically decrease. Validate renewal semantics before relying on uninterrupted monthly operation.

The whitelist covers full-match three-way results and goal totals with validated quarter lines. Two-way results are rejected rather than interpreted as draw-no-bet. Prices preserve actual market/bookmaker update times; missing times remain unknown and future times are rejected. Exact normalized teams, same league and bounded kickoff gap are required; ambiguous matches are withheld. The existing feed archives independent observations. Player props, cards, corners, first-half, live micro bets and executable same-game accumulators are not priced by this adapter.

The default price cache lasts 24 hours. This saves credits but often fails executable freshness requirements. The model-evidence and freshness gates remain unchanged; more provider access does not establish profitability. Current model approval is still absent.

Run the one-league local check from the project root:

```sh
python scripts/probe_the_odds_api.py --output docs/the-odds-api-validation-local.json
```

It reads private credentials, makes at most one paid league request unless cached, and stores quota/cache data only. It does not publish forecasts or send notifications. The sandbox probe remains DNS-blocked; see `the-odds-api-validation.json`. Tests use synthetic responses and exercise contracts, caching, concurrent reservations, usage floors, key replacement and ambiguous matching. Live account access and PostgreSQL execution remain unverified.

Official contract: [The Odds API v4 documentation and costs](https://the-odds-api.com/liveapi/guides/v4/).
