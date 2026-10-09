# Scalper improvement review

Reviewed on 2026-10-06 (Africa/Lagos), against repository revision `6e2907e`.
Public-source observations were made on 2026-10-05 UTC. This evaluates
[how_to_improve.md](../how_to_improve.md) and the coverage document actually
named [scappers_current_coverage.md](../scappers_current_coverage.md).
This review records the pre-implementation baseline. See
[the first release](RELEASE_VALIDATION.md) for the subsequently integrated batching,
selection and price-readiness changes and completed PostgreSQL verification.

The highest-value improvements are obtainable without replacing Scalper's
independent-worker architecture: correct price eligibility at publication,
batch database merges, expand verified bookmaker routes with isolated view
state, and normalize richer data already downloaded. An EPL-specific FPL
collector is also a promising addition. A broad asynchronous rewrite and a
generic streaming adapter have weaker evidence of immediate benefit.

## What the documents get right, and what needs correction

| Claim or suggestion | Finding |
| --- | --- |
| Thousands of individual quote writes are inefficient | Confirmed. Replayed SportyBet/Pinnacle captures required 1,865/1,585 SQL statements. A research candidate reduced these to 25/23. |
| Blocking browser assets saves 70–85% and makes collection sub-second | Unestablished. One conservative image/font/media-blocking sample per book reduced measured response body bytes by approximately 7%. Neither capture was sub-second. Aggressive stylesheet/tracker blocking was not tested. |
| Every cycle launches a new Chrome process | The existing collector shares one browser process across sources. It recreates isolated contexts/pages, so repeated SPA startup remains a real cost. |
| Core ingestion is just unpooled urllib | Incomplete. The shared transport has pooled direct connections; its proxy path uses urllib. Synchronous pacing can consume the budget, but an async client cannot eliminate a same-host rate limit. |
| Browser polling takes 5–15 minutes | Incorrect as a default. The worker pauses 60 seconds after capture; actual cadence includes capture duration. This is not a seconds-level live guarantee. |
| All 14 leagues can simply be mapped to Pinnacle | Needs source-by-source verification. Additional Bundesliga and La Liga routes were observed; only the Bundesliga sample had fresh eligible prices. |
| Scalper is 35–40% covered, with four requirements fully complete | Unsupported percentage. The categories are unweighted and mix availability, normalization, modeling and executable offers. Schedules, identities, contracts and history all have scope restrictions. |
| Missing referee, formation, crosses and player information requires paid feeds | Too broad. A downloaded ESPN summary already contained these fields. Most are currently raw or unused. One sample does not establish complete league coverage. |
| OpenLigaDB already extracts goal minutes | Incorrect for the current adapter. It extracts final results and fixture identities but discards goal events and half-time results. |
| OpenLigaDB is unmetered | Its published schema specifies 60 requests/minute/IP and recommends checking last-change dates. |
| The Odds API adapter supports period/corner odds | The provider's external catalogue and this implementation differ. The current adapter accepts only `h2h` and `totals`. |
| A free API-Football account supplies current-season enrichment | Unverified. Free access is 100 requests/day with restricted available seasons. Entitlement must be checked for the actual account and competition. |
| Supporting mode already implements a 90%/10% surgical enrichment strategy | It implements provider fallback and existing provider capabilities. No automatic value-aware gap planner or 30-minute pre-kickoff enrichment workflow exists. |
| StatsBomb open data is CC0 and a comprehensive live substitute | Incorrect framing. It has a separate licence and attribution requirements, selected historical competitions and partial 360 coverage. It is a research source, not a current live feed. |
| A WebSocket observed on a page establishes a real-time odds feed | Insufficient. SportyBet exposed three frames totaling 171 bytes in a short sample; their semantics were not established. No Pinnacle socket or SSE message appeared in that sample. |

Sources: [Playwright routing/cache behavior](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route),
[OpenLigaDB schema and limits](https://api.openligadb.de/swagger/v1/swagger.json),
[API-Football plans](https://www.api-football.com/),
[The Odds API usage and market costs](https://the-odds-api.com/liveapi/guides/v4/),
[StatsBomb open-data repository](https://github.com/hudl/open-data).
Code findings refer to this checkout; probe results are recorded in
[the evidence file](research/2026-10-05-findings.json).

The useful coverage distinction is:

| Capability | Current usable scope | Next gap |
| --- | --- | --- |
| Calendars and identities | ESPN has an explicit 14-league map; bookmaker event identities are source-scoped | A map is not continuous fixture completeness; canonical players and competition stages need work |
| Historical goal results | Openfootball has eight league mappings and the collector bootstraps current/previous seasons | Unknown kickoff times and absent odds restrict time-sensitive evaluation; it is not a rich multi-season player/statistics history |
| Team and period statistics | Available ESPN regulation counts and half scores are normalized | Source/league completeness, period-specific corners/cards and exact event timing remain gaps |
| Executable market prices | Pinnacle EPL plus the partial SportyBet Nigeria landing view; supported regulation contracts only | Other leagues/books and corner/card/player/throw-in contracts need independently verified feeds |
| Player, referee and tactical context | Some raw ESPN summaries already contain records | Typed histories, point-in-time availability and corresponding prediction models are missing |
| Odds evaluation history | Prospective active-price changes and checkpoints, retained for 30 days | Longer history, closed-offer transitions and complete fixture/provenance context |
| Live micro markets | Some current match states are collected by polling | No verified low-latency event/odds stream or executable micro-market modeling coverage |

First-half prices captured by Pinnacle are retained but excluded from the current
regulation bridge. Correct-score or other model forecasts do not establish that
Scalper has an offered bookmaker price for those selections.

## Correctness should precede more frequent collection

### Apply the odds floor to the offered price

`board_min_offer_odds` and `board_min_fair_odds` both default to 1.18.
[The board](../engine/lisa/board.py) filters both. For a binary, no-push outcome,
fair odds are `1 / probability`, so the fair-odds floor excludes probabilities
above approximately **84.75%**. This conflicts with selecting strong winning
chances whenever the actual bookmaker price meets the requested minimum.

Illustrative arithmetic, not a validated selection: a calibrated 90% probability
at an offered price of 1.22 has fair odds of 1.111 and expected net return
`0.90 * 1.22 - 1 = 0.098` per unit. The current fair floor rejects it.
Keep the 1.18 **offered** floor, separate model sanity bounds from price
eligibility, and preserve calibration/evidence requirements. Push and quarter-line
contracts require their existing settlement-aware calculation rather than this
binary example.

The earning ladder also sorts by kickoff and model probability before EV.
Evaluate a separate ranking objective based on conservative expected return,
uncertainty and exposure. The winning ladder can retain its probability focus.
More markets alone do not establish a better prediction or a profitable edge.

### Expire executable prices when reading a published board

The Scalper provider checks quote age during generation. However,
[DailyService.read](../engine/lisa/daily_service.py) removes passed kickoffs
without rechecking price age. Its readiness threshold can be 900 seconds with
the default 300-second generation interval, while quote validity defaults to
300 seconds. Consequently, a stored board can continue displaying an expired
price after an outage or between refreshes.

Persist an explicit price-validity deadline and source/view identity; check
eligibility locally on each read. Expired earning entries and accumulator legs
must lose executable-price status; forecasts may remain available. Distinguish
model readiness from price readiness. Reads must continue to perform no remote
collection. A newly observed source suspension/failure should invalidate prices
even if their original deadline has not elapsed. Validation needs controlled-clock
tests around expiry, recorded failure grace, suspended offers and worker outages.

## Measured database improvement

Saved public browser captures were read from the local trial database without
modifying it. Each merge was replayed into a separate temporary SQLite database.
The refresh timing is the median of three repetitions after the first merge.

| Sample | Fixtures / quotes | Current statements | Candidate statements | Current refresh | Candidate refresh |
| --- | ---: | ---: | ---: | ---: | ---: |
| SportyBet | 50 / 1,612 | 1,865 | 25 | 210.97 ms | 179.97 ms |
| Pinnacle EPL | 20 / 1,482 | 1,585 | 23 | 143.09 ms | 123.61 ms |

This reduces statement counts by **98.7% and 98.5%**. These are SQLite results;
they do not establish remote PostgreSQL latency. For illustration only, a serial
10 ms per-statement network overhead would be 18.65/15.85 seconds before batching
and 0.25/0.23 seconds afterward. Actual PostgreSQL performance and concurrency
still require a real integration run.

The [isolated candidate](research/batched_repository_candidate.py) pre-encodes
quotes, fetches prior event/quote-set state in bounded groups, and uses portable
multi-row inserts and grouped deletions inside the existing transaction. It keeps
source locking, identity validation, final-result protection, newer-snapshot
precedence, statistics enrichment and empty-set revocation. It is **not wired
into production**. Existing Scalper regressions pass against it: **65 passed,
2 PostgreSQL-dependent tests skipped, 5 subtests passed**.

Do not use `ON CONFLICT DO NOTHING` to conceal contradictory observations.
The project's PostgreSQL wrapper does not currently expose `executemany`;
Psycopg's automatic pipelining also differs from issuing a single multi-row SQL
statement. See [Psycopg's pipeline documentation](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html).

Reproduce with a Python environment containing the project's test dependencies:

```bash
python scalper/research/benchmark_merges.py --database data/localhost-scalper.db
python scalper/research/benchmark_merges.py --database data/localhost-scalper.db --candidate
python scalper/research/run_candidate_tests.py -q
```

The benchmark needs previously saved public browser captures. It performs no
network calls and writes only temporary databases. Different captures or machines
will produce different timings and counts. The test runner replaces the repository
class only inside its process.

## Obtainable coverage expansion

### Verified bookmaker views, rather than inferred league IDs

Normal public Pinnacle pages exposed compatible guest schemas for:

| Competition | Observed publisher league ID | Events / quotes | Fresh regulation quotes at capture |
| --- | ---: | ---: | ---: |
| Bundesliga | 1842 | 18 / 1,343 | 857 |
| La Liga | 2196 | 20 / 1,474 | 0 |

An isolated version of the current parser with its three EPL-specific constants
parameterized accepted both payloads. La Liga's market response carried HTTP
`Age: 605`; a current response Date did not make its prices fresh. This confirms
schema compatibility, **not fresh executable coverage** for La Liga. Production
still enables only Pinnacle EPL.

A source catalogue should store canonical competition, verified publisher ID,
page URL, response roles, region, parser version and verification date. Scope raw
resources, leases and missing-offer reconciliation to each page/view. Reusing
`browser:landing` unchanged across leagues can cause one page to revoke another's
offers. Pagination and partial landing pages require explicit coverage boundaries;
absence from a partial view is not proof that a bookmaker closed a whole league.
Keep source-wide access/rate circuits separate from a single view's schema error.
Expand existing supported bookmakers only after observing and testing their
actual markets and settlement contracts.

### Extract more value from existing ESPN summaries

One completed EPL match summary returned HTTP 200 and included:

- possession, crosses, accurate crosses, blocked shots and interceptions;
- venue, neutral-site context and a referee assignment;
- two formations and 40 roster records with source athlete IDs, starters,
  positions, substitutions and available individual statistics;
- 15 structured key events and 88 commentary entries.

The [observed ESPN response](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=740644)
and its hash are recorded in the evidence file.

The current parser maps only a subset of team counts and preserves raw rosters.
Extend typed normalization before adding another per-match network request.
Possession requires a percentage type; source/player identities and observation
times require their own contracts. The sampled player records contain shots,
shots on target, fouls, cards and goals/assists, but do not demonstrate complete
player minutes, passes or tackles. The match ended 0–0, so goal/VAR correction
semantics and comprehensive shot chronology were **not** verified.

Use structured event IDs, periods and ordering; commentary text is not a reliable
event counter. Referee names alone are insufficient stable identities. Historical
formations or lineups retrieved after a match cannot be presented as information
known before kickoff. Validate multiple matches and competitions, then test feature
contribution chronologically before enabling new models.

### Add FPL as a bounded EPL enrichment source

The official public [bootstrap feed](https://fantasy.premierleague.com/api/bootstrap-static/)
returned 20 teams and 667 players, including xG/xA, minutes, starts, status/news,
playing-chance estimates and set-piece orders. The
[fixture feed](https://fantasy.premierleague.com/api/fixtures/) returned 380
current-season fixtures. One
[player history sample](https://fantasy.premierleague.com/api/element-summary/411/)
contained five current match records and four prior-season **aggregate** records.
This is evidence of a useful source, not an implemented Scalper integration or
a multi-league/player-prop dataset.

Cache the bulk feeds according to their observed freshness; collect histories
only for relevant players within a bounded budget. Namespace identities by
provider/season, preserve stable player codes where supplied, and handle transfers,
fixture postponements and provisional results. Estimated playing chance is not
a confirmed lineup. Aggregates do not replace per-match histories. Data returned
today must not leak into yesterday's backtest. FPL player costs are fantasy-game
values, never bookmaker odds.

## Efficiency, persistence and hybrid enrichment

The conservative browser experiment preserved scripts, stylesheets and API calls:
Pinnacle response body bytes changed from 4.06 MB to 3.80 MB; SportyBet from
2.13 MB to 1.98 MB. These are single samples with differing cache/network
conditions, not a causal benchmark or total browser memory measurement.
Pinnacle's lean sample also had stale prices. JavaScript remains a major cost.
Blocking all non-XHR requests would prevent SPA bootstrapping; introducing
Playwright routing disables HTTP cache.

Trial bounded-lived public guest contexts, with source-specific asset rules,
measured bytes/CPU/RSS and a full-page fallback for filter-caused failures.
Reuse needs coherent calendar and market observations: the Pinnacle parser
rejects parts captured more than 30 seconds apart. A warm stream cannot attach
new prices to an indefinitely old event catalogue. Recycle failed or oversized
pages and preserve source isolation.

Prioritize nearby kickoffs, changing markets and coverage gaps; refresh distant
fixtures less often. Keep bounded host budgets and jitter. Concurrent work across
different hosts may help, but fetching 24 requests at 0.5 requests/second on one
host still needs roughly 46 seconds of pacing after the initial request. Preserve
calendar/backfill/enrichment reservations rather than disguising oversubscription
with async tasks. The existing collector already caches empty schedules and
prunes known season boundaries.

Browser history currently retains active fresh price changes/reopenings and hourly
checkpoints for 30 days. It does not provide an immutable history of all suspended
offers, and individual quote records lack complete canonical fixture context.
Keep raw payload retention bounded, but retain compact normalized observations
longer, with kickoff revisions, closure/gap events and identity provenance. Derive
closing prices only from eligible pre-kickoff observations; collection gaps cannot
be silently filled from later prices. This matters more to eventual model
validation than faster downloads alone.

Supporting mode is a sound default for redundant sources. Make surgical enrichment
explicit: a capability/entitlement registry, missing-field queue, pre-kickoff
deadlines, persistent quota reservations and priority by expected decision value.
Reserve capacity for settlement and recovery. Never assume every advertised
endpoint/market is accessible to a particular free account. The Odds API charges
credits by requested markets and regions and reserves historical access for paid
plans. [OddsPapi documents unmetered historical calls](https://oddspapi.io/us/docs/requests-and-quota),
but actual account, market and season coverage still needs verification.
No private account entitlement tests were made in this review.

## Recommended implementation order and acceptance criteria

| Order | Deliverable | Evidence required before activation |
| --- | --- | --- |
| 1 | Offered-price floor correction and read-time quote expiry | Controlled-clock tests; high-probability/acceptable-offer cases; stale, suspended and unavailable prices cannot remain executable. Preserve settlement/evidence controls. |
| 2 | Batched merges and isolated verified bookmaker-view catalogue | Existing merge regressions plus real PostgreSQL concurrency/rollback tests; a failed or partial view cannot revoke another view; fresh quoted-fixture coverage increases measurably. |
| 3 | ESPN typed enrichment and bounded FPL collector | Multi-match coverage fixtures, source-ID resolution, unit validation, transfer/provisional-result handling and point-in-time provenance. Missing fields remain absent. |
| 4 | Longer compact history, adaptive scheduling and enrichment planner | Restart-safe quotas/cursors; collector outages recorded as gaps; closing-price provenance; measured useful-data yield and request/CPU costs. |
| 5 | New market models or verified streaming contracts | Exact bookmaker market/period/settlement mapping, sufficient historical observations, chronological out-of-sample calibration and return uncertainty. Retain the existing EvidenceGate until a scope qualifies. |

Replace the coverage percentage with a per-league/bookmaker/market matrix showing
source availability, normalized fields, historical sample counts, model support,
fresh quoted fixtures and approved/settleable selections. Add unmatched identities,
cache Age, source heartbeats, capture/commit latency and publisher-confirmation
lag. Existing price-match outcomes already record mapping failures; surface their
actionable details rather than claiming all mismatches are silent.

Accumulator prices must correspond to a combination actually offered by a usable
bookmaker. Multiplying the best single-leg prices across different books is not
proof of an executable accumulator. Same-match selections need valid joint prices
and dependence treatment. Evaluate forecasting changes with chronological held-out
Brier/log loss and staking changes with historical executable prices, costs,
exposure and uncertainty. The aim is useful, evidenced selections, not maximum
market count.

## Changes made during this review

The batching implementation and runners are isolated research artifacts. No new
bookmaker routes, feeds, odds-floor settings or prediction models were activated.
Two small reliability fixes were needed: correct the Redis connection keyword
`dLecode_responses` to `decode_responses`, with a constructor regression; and
make the existing browser-history test's kickoff relative to its observation
clock instead of expiring at a fixed calendar date.

Relevant production regressions: **95 passed, 2 PostgreSQL-dependent tests skipped,
9 subtests passed**. Candidate regressions: **65 passed, 2 PostgreSQL-dependent
tests skipped, 5 subtests passed**. No real PostgreSQL integration, sustained
browser benchmark, account-entitlement verification or profitable-market approval
is implied by these results. No commit or push was made for this review.
