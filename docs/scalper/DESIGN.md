# Scalper implementation decision

The implemented architecture uses a durable collector and native provider
bridge. The source observations below were inspected on 2026-10-05; current
availability and coverage require a fresh check.

The efficient solution is a durable collection worker and a native LISA provider
bridge. Collect reusable league batches, not one website per prediction. Store
source records independently, resolve identities conservatively, and use LISA's
existing historical reconciliation, model, publication and settlement jobs.
SQLite WAL serves a local installation; the same repository uses managed
PostgreSQL when worker and web application run on different hosts. Network
requests never hold database locks. Redis is unnecessary. The core worker has
no browser dependency; a separate optional browser worker now collects verified
public bookmaker feeds. See [collector setup](README.md) and
[coverage boundaries](COVERAGE.md).

## Evidence and source roles

* ESPN's public [scoreboard](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates=20250921)
  was fetched successfully: three completed EPL events, UTC kickoff, source event
  IDs, home/away identities, actual scores, corners, fouls and shots. The
  [summary](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=740644)
  adds period scores and actual yellow/red cards. These are undocumented web
  endpoints, not an unlimited, supported developer API. The checked October
  2026 scoreboard was empty; a working response is not coverage evidence.
* A direct SportyBet Nigeria upcoming-events request returned 403, while its
  normal browser page successfully supplied the response. The optional browser
  adapter now verifies active prematch offers and separates `lastOddsChangeTime`
  from publisher snapshot confirmations. Pinnacle's public EPL page also
  supplied verified matchups and prices. Cache age can still make them unusable.
* [Sportradar's market catalogue](https://developer.sportradar.com/odds/reference/oc-core-overview)
  distinguishes full-match, first-half and other contracts. Market IDs alone
  must never cause a first-half or corner price to become a full-match goal price.
* Existing [Openfootball history](https://github.com/openfootball/football.json)
  reduces model bootstrap requests. Date anchors cannot become timed upcoming
  fixtures or authoritative settlement observations.
* [Football-data.co.uk's published usage terms](https://football-data.co.uk/data.php)
  now explicitly exclude automated bots/scrapers/AI and commercial or data
  training products from its free individual-use offer. The older proposal's
  CSV recommendation cannot be treated as an unrestricted automated source.
* [StatsBomb open data](https://github.com/hudl/open-data) contains selected
  research competitions; it is not a contemporary universal live feed.
  [FotMob's robots policy](https://www.fotmob.com/robots.txt) disallows /api/* for
  general agents. Other sources remain adapters to add when usable access and
  their payload have been verified. Existing API keys remain optional fallback.

## Collection and failure behaviour

Persist accepted raw JSON with a content hash, retrieval time, conditional
validators and parser version. Validate before replacing the last good resource.
Persist source cooldowns, failure counts and backfill cursors across restart.
Honour Retry-After; 401/403 produce a long cooldown; transient failures use bounded
exponential backoff. A malformed resource is quarantined without deleting good
data. Host pacing, payload limits, cycle deadlines, request caps and a database
lease bound costs and prevent overlapping collectors.

Use upcoming league batches and a rolling recent-results window. Prioritize
live/near-kickoff schedules, then idle calendars. Enrich completed/live events
with bounded summary requests. Resume historical day batches from persisted
cursors. Bootstrap goals from one current/previous-season CC0 file per cycle;
this gives a useful model sample without hundreds of daily requests. Backfill
and source failures must not prevent other collectors running.
Retain one raw snapshot per resource with a bounded retention window; historical
observations remain in LISA's existing archive. Poll cadence measures collection
age, not the publisher's latency or guaranteed micro-betting suitability.

Keep source IDs stable across kickoff revisions. Preserve team qualifiers,
competition and home/away orientation. Ambiguous rematches and contradictory
results remain unresolved. Never fuzzy-merge solely on a team substring.
Extra time and shootouts require explicit regulation-period scores; cumulative
extra-time stats cannot train or settle regulation corner bets.

Serve cached schedules only within their freshness allowance. Final scores may
remain available for history. Retained stale data are inspectable but cannot
silently become fresh schedules or executable quotes. Quote replacement is
atomic per bookmaker event, so suspended/disappeared markets cannot survive a
successful refresh. Retrieval time never substitutes for bookmaker update time
or offer confirmation. Verified browser adapters independently account for HTTP
Date/Age when confirming current snapshots; unchanged prices keep their original
price-change time.

## Prediction boundary

Supply observed records for goals, half-time scores, corners, cards, fouls and
other available statistics. Preserve market, selection, line, period, payout
contract, active state, bookmaker and source time. Only precisely supported
regulation contracts cross the current price bridge. Unknown contracts remain
diagnostics, never guessed odds. An explicit normalized ingestion contract makes
additional authorized feeds and captures pluggable without rewriting the model.

Apply the selection floor of 1.18 to offered odds; fair odds remain model output.
High winning probability and positive expected value remain separate criteria. Missing quotes
mean unknown earning opportunity; missing models mean no defensible prediction.
Same-game combinations need joint prices/dependence modelling; cross-match
accumulators remain research candidates until a combined offer is observed.
No transport architecture guarantees free universal coverage or profitability.

## Acceptance cases before live operation

Test restart recovery, empty versus malformed responses, 429/403 cooldowns,
conditional 304, no overlap, source failover, out-of-order observations,
kickoff revisions, reversed/ambiguous identities, missing versus zero statistics,
extra-time scores, disappeared/suspended prices, invalid/nonfinite odds, stale
quotes, period isolation, sole/supporting provider routing and the 1.18 floor.
Validate collector changes in an isolated temporary database before using the
application ledger. Preserve private credentials and original observations.
