# AllSportsAPI assessment — 3 October 2026

The [v2 documentation](https://allsportsapi.com/soccer-football-api-documentation)
provides date-range fixtures with statistics, scores, team IDs and lineups. Odds
includes double chance, goal totals and BTTS; FullOdds includes named markets and
bookmakers, with correct-score examples. Its examples do not establish bookmaker
quote timestamps, regulation-period corner completeness, or historical corner odds.

The [football plans](https://allsportsapi.com/soccer-football-api) currently display:

| Plan | Monthly displayed price | Calls/hour | Coverage / odds listing |
|---|---:|---:|---|
| Free | $0 | 260 | Two random leagues; odds not listed |
| European | $74 | 2,000 | 42 leagues + UCL/UEL; odds not listed |
| Worldwide | $111 | 2,000 | 800+ leagues; odds listed |
| Ultimate | $149 | 100,000 | Adds live odds and beta live sockets |

Paid plans advertise a 14-day trial. Displayed discounts are not guaranteed renewal
prices. Confirm account entitlements and licensing before purchasing.

## How it could improve this project

AllSportsAPI is an optional supporting calendar/result source and a second offer
source. The existing providers remain configured; it does not replace them.
A date-range response containing fixtures and statistics could eliminate the
separate per-match statistics calls needed by the API-Football adapter. That
potential efficiency improvement depends on actual statistics coverage and response
limits; it has not been measured with authenticated data.

Use the prediction system's independent models and market payout contracts.
Provider-generated probabilities are a benchmark, not evidence that our model has
an edge. A wider source does not automatically validate new markets or profitability.
Do not multiply quotes from different bookmakers into an accumulator offer.

Keep provider IDs and raw provenance, map competitions explicitly, persist dated
results, and capture offers before kickoff for later evaluation. Refuse unknown,
half-time, extra-time and suspended market contracts. Treat retrieval time and
bookmaker update time as separate facts. Keep recommendations disabled for offers
whose currency, market period, bookmaker access or freshness cannot be established.

## Concrete trial check

A read-only probe is available at `scripts/probe_allsports.py`:

```sh
PYTHONPATH=engine python scripts/probe_allsports.py
PYTHONPATH=engine python scripts/probe_allsports.py --league-id <catalog-league-id>
```

Set `ALLSPORTS_API_KEY` in the private environment or `.env`. The first invocation
lists subscription-visible leagues; the second checks recent/upcoming fixtures,
a two-year-old historical sample, statistics names/count completeness and FullOdds
market/bookmaker coverage. Credentials travel in a POST body, and output omits
credentials and complete provider payloads. It does not publish predictions or
change the project database.

Then sample several matchdays in each target league, check regulation corners
against an independent result source, measure update delays, verify bookmaker
access and inspect complete odds payloads for timestamps and market definitions.
That evidence determines which supplementary fields and markets can be trusted.
The supporting adapter is implemented in `engine/lisa/providers/allsports.py` and
enabled by `ALLSPORTS_API_KEY`. Persistent caching, hourly request accounting and
a settlement reserve limit duplicate work. Odds fetching additionally requires
`LISA_ALLSPORTS_ODDS_ENABLED=true` and an entitled plan. The documented odds
contract does not establish quote update times; such offers cannot produce
executable recommendations. Corner counts require a verified statistic type via
`LISA_ALLSPORTS_CORNER_STAT_TYPE`; missing counts remain missing.

Authenticated coverage, provider-specific competition names and update delays
have not been verified because this execution environment cannot reach the API.
Credentials are configured privately. The independent history worker now collects
one monthly AllSports window per league rotation, starting three years back.
Durable checkpoints resume across restarts; failures retain the cursor and wait
24 hours before retrying. The current month refreshes daily. This is implemented
and tested offline, but authenticated historical availability remains unverified.
The latest offer snapshot is retained for inspection, not a complete odds-history
archive. These are explicit remaining integration limits.

## Complementary sources to investigate

- [TotalCorner](https://www.totalcorner.com/page/api) documents corner results and
  historical odds movement. It is a candidate for the corner-price/history gap.
  Verify bookmaker identities, quote timestamps, market periods and historical
  coverage with authenticated samples before building its adapter.
- [Sportmonks xG](https://www.sportmonks.com/football-api/xg-data/) supplies
  fixture/player expected-goals metrics as an add-on. This could add chance-quality
  features beyond final scores. Its stated history begins in 2024, so entitlement,
  league coverage and sufficient training history need checking. No adapter is
  implemented, and purchasing it would not establish a prediction edge.

Prioritize missing data rather than purchasing overlapping calendars. Maintain
source provenance and reject conflicting final results before training or
settlement. Evaluate each added feature against the same chronological holdout
and bookmaker benchmark before enabling stakes.
