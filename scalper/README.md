# Scalper

Scalper collects sports observations in a separate Python worker, persists them
in LISA's SQLite or PostgreSQL database, and exposes them through the native
calendar/price provider contracts. A website request never starts collection.
Read [the design and source research](DESIGN.md) before enabling sole-source mode.
See [verified results and remaining gaps](VALIDATION.md) for the actual probes
and regression checks.
The [blueprint evaluation](BLUEPRINT_REVIEW.md) explains the optional bookmaker
browser worker and which proposed improvements were adopted.
The [improvement review](IMPROVEMENT_REVIEW.md) evaluates the newer performance
and coverage suggestions against measured captures and an isolated batching prototype.
The [first implementation and release checks](RELEASE_VALIDATION.md) document
the production batching, offered-price floor and publication price-expiry behavior.

## Start locally

Use the **same database path and league list** for both processes. For a trial,
use a new database; the example below does not change your existing ledger.

```bash
PYTHONPATH=engine python3 -m lisa.scalper once --database data/scalper-trial.db --leagues soccer_epl --request-limit 4
PYTHONPATH=engine python3 -m lisa.scalper status --database data/scalper-trial.db
PYTHONPATH=engine python3 -m lisa.scalper run --database data/scalper-trial.db --leagues soccer_epl
```

For an existing installation, point the collector at its actual database:

```bash
PYTHONPATH=engine python3 -m lisa.scalper run --database data/lisa.db
```

Then configure LISA and restart its existing generation/settlement workers:

```dotenv
LISA_STORAGE=sqlite
LISA_DATABASE_URL=data/lisa.db
LISA_SCALPER_MODE=supporting
LISA_SCALPER_FIXTURE_MAX_AGE_SEC=900
LISA_SCALPER_QUOTE_MAX_AGE_SEC=300
LISA_BOARD_MIN_OFFER_ODDS=1.18
```

`supporting` checks fresh stored Scalper calendars first, then uses existing
providers if coverage is absent, stale, failed or historical only. Existing
bookmaker adapters and Openfootball bulk history remain available. All
bookmakers already supported can still contribute; there is no Scalper book
allowlist. `only` constructs just the stored Scalper calendar/price bridge and
makes no remote API calls in the prediction process. Previously saved history
remains available; a fresh installation must collect enough history before its
goal and corner models can operate. Its history job reports delegation to the
independent collector and preserves collector failures. `off` is the rollout default.

For PostgreSQL, omit `--database` and supply the same `LISA_STORAGE=postgres`
and `LISA_DATABASE_URL` as LISA. The worker can run locally while a hosted app
reads the shared database. A PostgreSQL worker connecting to the Vercel app's
private schema must use that schema too: set `LISA_SCALPER_PRIVATE_SCHEMA=true`.
Keep database credentials in the normal private environment. The optional
Docker Compose `scalper` profile and [systemd service](../deploy/lisa-scalper.service)
provide supervised process examples. The service uses the same user/path layout
as the existing deployment units; adjust those paths for your host. A persistent loop belongs on a worker
host; a Vercel request cannot host it.

## Collection and inspection

Default collectors: Openfootball CC0 bulk history and ESPN's public,
undocumented football web endpoints, covering
the explicit source league map. Daily league batches supply actual scores,
corners, fouls and shots; bounded match summaries add available cards and period
scores. Missing statistics remain absent. Raw responses, hashes, validators,
cooldowns, source identities and historical cursors survive restart.

One current/previous-season Openfootball file is collected per cycle, with daily
current-season and monthly completed-season caching. Its original vetted parser
and conservative result-availability dates are reused. File dates remain unknown
kickoff anchors and cannot publish upcoming picks or settle predictions. Use
`--no-openfootball` to disable this bootstrap.

Defaults: at most 24 requests per cycle, a 45-second cycle budget, four summary
requests, 90 days of resumable historical day batches, and a 60-second worker
interval. Per-host pacing is deliberately conservative. Collection prioritizes
live schedules and reserves a share of requests/time for enrichment and other
feeds. Known season calendars prune empty future dates. Empty responses are
cached longer. Backfill progresses one league/day per cycle and never advances
its cursor after a failure or exhausted budget.

`--request-limit`, `--summary-limit`, `--history-days`, `--cycle-seconds` and
`--interval` tune these bounds. Set `--history-days 0` to stop backfill. Circuit
failures distinguish access denial, rate limiting, schema rejection and transport
errors without exposing URLs, credentials or response bodies. Inspect `status`
and LISA's existing coverage plan. Full raw snapshots are retained for 30 days;
latest quotes for two days; normalized events for four years. Accepted final
observations also enter the existing historical repository.

The worker's `ok` state means its attempted requests succeeded. It does **not**
mean every league had events, every team has a model, or executable prices were
available. A cached empty schedule, a blocked feed, missing prices and no
qualifying selection are different states.

## Bookmakers and additional collectors

The independent browser worker opens the normal public pages for **SportyBet
Nigeria and Pinnacle EPL** and captures their verified JSON feeds. Install its
optional dependency and a compatible local Chromium browser:

```bash
python3 -m pip install './engine[scalper-browser]'
python3 -m playwright install chromium
PYTHONPATH=engine python3 -m lisa.scalper browser-once --database data/scalper-trial.db --leagues soccer_epl
PYTHONPATH=engine python3 -m lisa.scalper browser-run --database data/scalper-trial.db --leagues soccer_epl
```

Use the same database as the core worker and LISA to combine prices with
statistics and history. `--browser-bookmaker sportybet` or `pinnacle` selects a
single verified adapter; repeat the argument for both. `--capture-seconds`
sets each capture's 5–45 second budget (default 30), and `--interval` controls
the pause between cycles. Separate source captures run concurrently. Chromium
startup is shared; isolated temporary contexts discard page sessions after each
capture. `--browser-executable` selects a local Chrome/Chromium installation,
`--headed` shows it, and `--browser-proxy` configures an ordinary explicit proxy
without embedded credentials. Missing browser dependencies produce a sanitized
setup error without opening publisher circuits.

The [browser systemd unit](../deploy/lisa-scalper-browser.service) is separate
from statistics collection. Install the optional dependency/browser for that
service user, adjust its paths, and verify `browser-once` before enabling it.
The default Docker application image and core Scalper profile do not include
Chromium; use a separate browser-capable worker host with the shared PostgreSQL
database for that deployment.

Pinnacle currently verifies only its EPL route. SportyBet's landing page supplies
partial event coverage across its explicit league map. A successful browser
capture does not enumerate every market, league or bookmaker. All existing
bookmaker providers remain available through supporting mode; other browser
adapters still need observed schemas and verified route mappings.

Browser quotes retain the actual price-change time when supplied, plus a separate
publisher-confirmation time derived conservatively from HTTP Date/Age. Cached
responses never gain freshness from a new download. Suspensions and disappearing
page offers revoke prior prices; older captures cannot revive them. Following a
recorded source failure, confirmations older than 30 seconds are refused. Current
normalized snapshots remain available for inspection. Generic JSON feeds cannot
claim the built-in browser confirmation method.

The browser collector writes actual price changes/reopenings and hourly
confirmation checkpoints into the existing immutable odds archive, with 30-day
browser history retention. Published priced rows expose `price_updated_at`,
`price_confirmed_at` and `price_freshness_basis`. They distinguish a newly changed
price from a freshly confirmed unchanged offer. Half-time, in-play and other
unsupported contracts remain outside the current regulation prediction bridge.

Published prices carry explicit expiry deadlines and local quote identities.
Publication reads remove expired earning prices and downgrade affected forecasts
and accumulator legs to model-only records. Local suspension, withdrawal or
price changes also invalidate an old offer before its deadline. The response's
`price_readiness` distinguishes usable prices from worker readiness and collection
failures. The 1.18 minimum applies to offered prices; fair odds are model output.
Successful collection records expose `merge_ms` for the batched staging merge.

`--sportybet` enables a bounded experimental Nigeria upcoming-events request.
This environment returned HTTP 403. That legacy direct adapter does not attest
to publisher freshness: its parsed offers remain research records
and cannot enter the earning ladder. Only one page is read; completeness is
explicitly unverified. A block opens a persistent six-hour circuit. All other
SportyBet markets remain in the raw response for later explicit adapter work.
Use the separately verified browser adapter above for current offered prices.

Connect an authorized collector that supplies the interchange below:

```bash
PYTHONPATH=engine python3 -m lisa.scalper run --feed owned_feed https://your-data-host.example/scalper.json
```

Repeat `--feed NAME HTTPS_URL` for other independent suppliers, or replace ESPN
with `--no-espn`. URLs come only from process startup configuration; URLs inside
payloads are never followed. Configured standard network proxies are supported.
Sources are small adapters: add verified source schemas to `sources.py`, then
return the same `Batch` contract. Keep transport, persistence, quotas and model
logic out of source parsers.

## Version-1 interchange

This illustrative schema is **not a real match or price**. Replace every value
with an actual observed record; retain the original `collected_at` when importing
a capture, and the actual bookmaker's `updated_at` on each generic feed quote.
The built-in browser adapters separately attest to publisher confirmations;
generic interchange imports cannot supply that attestation.

```json
{
  "schema_version": 1,
  "collected_at": "2026-10-05T12:00:00Z",
  "fixtures": [{
    "event_id": "source-event-123",
    "sport_key": "soccer_epl",
    "kickoff": "2026-10-06T19:00:00Z",
    "home_team": "Example Home",
    "away_team": "Example Away",
    "status": "SCHEDULED"
  }],
  "quotes": [{
    "event_id": "source-event-123",
    "book_key": "bet365",
    "book_title": "Bet365",
    "market": "totals",
    "selection": "Over",
    "line": 2.5,
    "period": "regulation",
    "settlement_contract": "regulation",
    "active": true,
    "odds": 1.95,
    "updated_at": "2026-10-05T11:59:50Z"
  }],
  "replace_books": [{"event_id": "source-event-123", "book_key": "bet365"}]
}
```

Final observations require `status: FINISHED`, `home_score`, `away_score` and
`score_period: regulation`. Optional fields include `home_first_half_score`,
`away_first_half_score` and home/away prefixed `corners`, `yellow_cards`,
`red_cards`, `fouls`, `throw_ins`, `shots`, `shots_on_target`, `offsides`,
`passes`, `tackles`, `saves`. Set `statistics_period` when statistics include
extra time; such corners cannot train or settle regulation bets. Half-time,
player, card and other price contracts can be retained, but only exact supported
regulation contracts enter today's model bridge.

Each event/book entry is a **complete quote snapshot**. Even with an empty
`quotes` list, `replace_books` revokes its prior offers. Incremental producers
must materialize their current event/book state before sending it. Suspensions
use `active: false` and may omit odds. A missing bookmaker time uses null and
remains unpriced. Source-scoped IDs and conservative competition/team/kickoff
matching prevent unrelated matches being joined. Corrections are preserved;
out-of-order observations cannot overwrite newer events or revive revoked offers.

For an existing authorized capture:

```bash
PYTHONPATH=engine python3 -m lisa.scalper ingest --database data/lisa.db --source owned_capture --file capture.json
```

## Market boundary

Current goal models can evaluate 1X2, totals, team totals, BTTS, double chance,
draw no bet, offered Asian lines and exact scores. Actual corner history supports
the existing corner baseline once coverage thresholds are met. Collection of
cards, fouls or first-half results supplies research evidence; it does not add
validated models for those markets. Throw-ins, player props and live micro-bets
still need sufficient actual inputs and separate models. Same-game combinations
need joint sportsbook offers and dependence modelling. Cross-match accumulator
candidates remain unpriced research combinations.

Both fair and observed odds below 1.18 are excluded by default from the selection
ladders. Missing offers have no measured EV. Existing evidence and paper-mode
rules continue to govern stakes. This implementation establishes data plumbing,
not free universal access, guaranteed latency or demonstrated profitability.
