# Resources and implementation plan

Architecture follow-up: PostgreSQL is now the selected pre-production backend.
Shared repositories, authentication, pooling, migrations, independent settlement,
and separate-process deployment are implemented. See
[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md) for current startup and
validation status; older migration recommendations below describe the prior state.


Checked 2026-10-03. Separate implementation status from production evidence.

## Environment

Created `.venv` and enabled imports of the local engine through a workspace `.pth`
file. Runtime uses Python's standard library, including SQLite; Node and Docker CLI
already exist. Attempted installation of the declared test/PostgreSQL extras and
then pytest/psycopg directly. Package-host DNS/network access failed; neither extra
is installed. No cached wheels were found in the searched workspace/tool locations.
The Docker daemon is inaccessible. Installing packages cannot remove sandbox/network
restrictions or supply API credentials. Production Docker targets Python 3.12;
the local Python environment is 3.14.

Initialized the **local development** `data/lisa.db`, including the worker schema.
SQLite integrity check passes and WAL is active. It contains no imported live
predictions and reports not ready. This is not a configured production deployment.

On a network-enabled development host, use:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e './engine[test]'
PYTHONPATH=engine .venv/bin/python -m pytest engine/tests
```

Add the `postgres` extra only when implementing/testing the PostgreSQL migration.
Redis is optional, not required by the current deployment.

## Database decision

Use SQLite WAL for the current one-server, one-generation-worker launch. Short
transactions, a persistent local volume, indexed pending rows, and a worker lease
fit this workload. SQLite allows concurrent readers but one writer, so several
networked app servers should not share its file. See
[SQLite deployment guidance](https://www.sqlite.org/whentouse.html).

Choose managed PostgreSQL for the eventual multi-server product, with connection
pooling, tested backups, and point-in-time recovery. This is a recommendation,
not an implemented migration. The current PostgresStorage is a partial ledger
adapter; the daily worker, auth, admin, telemetry, and publication transactions
depend on SQLite. Changing LISA_STORAGE alone disables the daily service.

Migration sequence: define a complete repository interface; implement PostgreSQL
transactions and lease acquisition; migrate auth/telemetry/publications with explicit
schema versions; copy and compare ledger counts/keys/grades; test concurrency and
restore; cut over one worker before scaling web readers. Preserve entry probabilities
and prices and the immutable selection identity throughout.

For SQLite, create consistent backups with:

```sh
python3 scripts/backup_database.py data/lisa.db backups/lisa-20261003.db
```

The tool refuses an existing destination and checks the completed snapshot.
Schedule it externally, transfer backups off-host, and test restoring on an isolated
instance. Stop the application before replacing a live database during restoration;
handle the live WAL/SHM files as part of the stopped-instance restore procedure.
Do not back up a running WAL database by copying only its main file.

## Implemented resource coverage

| Source | Current adapter role | Important limitation |
|---|---|---|
| football-data.org | Season fixtures, results, model training | Token required; free plan lists 12 competitions, delayed schedules/scores, 10 calls/minute. Registry currently maps 10 of its competitions. |
| OpenLigaDB | Fixtures/results/training for Bundesliga and 2. Bundesliga | Community-maintained, unauthenticated; current adapter registry uses two leagues although the service lists more. |
| TheSportsDB | Supplementary upcoming fixtures and cross-checks | Current adapter supplies no adequate season history for its exclusive leagues; a fixture alone does not make a team model-ready. |
| SharpAPI | Current bookmaker prices matched to fixtures | Free plan: 2 books, 12 calls/minute, 60-second delay. Current integration fetches moneyline and totals, not priced BTTS/correct-score markets. |
| Packaged archive | Historical legacy backtest | Not a live calendar or evidence validating the current Dixon–Coles model. |

Sources: [football-data pricing](https://www.football-data.org/pricing),
[OpenLigaDB](https://www.openligadb.de/),
[TheSportsDB API](https://www.thesportsdb.com/api.php),
[SharpAPI pricing](https://sharpapi.io/pricing).

Blank LISA_BOARD_LEAGUES selects credential-supported official leagues. With no
football-data token, default coverage remains the two OpenLigaDB leagues. Do not
infer today's actual available matches/markets from provider advertising: credentials
and authenticated response coverage must be checked per league and market.

An additional broad football provider is a candidate, not yet integrated.
[API-Football](https://www.api-football.com/pricing) lists 100 requests/day free
with restricted seasons, and $19/month for 7,500 requests/day. Validate history,
league/market coverage, stable IDs, and bookmaker suitability before selecting it.
Purchasing a plan alone does not add an adapter or feed its fields into the model.

## Daily operation and next improvements

The implemented background worker runs on `lisa start`, immediately and at a
configurable interval (default 300 seconds after a cycle finishes). It fetches,
fits/reuses the model, settles eligible older selections from final results, and
publishes saved boards. HTTP visitors do not initiate generation. Results are
stored with source/final score. Missing finals remain pending. Quiet days may have
zero forecasts. Monitor `/api/ready`, last publication age, provider failures,
fixture coverage, quote coverage, and oldest unsettled selection.

For greater efficiency and reliability, next separate calendar/history ingestion,
price refresh, and settlement into independently scheduled jobs. Persist canonical
fixture/team IDs and provider aliases. Cache historical seasons durably, fetch
results incrementally, rotate league fetch order after budget exhaustion, and
refresh quotes selectively near kickoff. Keep one transactional publication ledger.
Settlement should continue even if model fitting or the price provider fails; the
current worker settles independently of whether a board exists, but an unexpected
exception before results return can delay settlement until the next cycle.

Winning ranking sorts by estimated probability. Earning ranking requires an actual
matched quote and estimated positive EV (`p * decimal_odds - 1`), with risk filters.
Neither proves a profitable strategy. Next priorities are walk-forward testing of
the current model by league/market, calibration, training sufficiency, quote age
checks, exposure caps across related selections, and closing-price capture.
Lineups, injuries, xG, and other richer data improve nothing until the model explicitly
uses them and out-of-sample evaluation demonstrates an improvement.
