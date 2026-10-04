# PostgreSQL paper pilot

This pilot verifies collection, daily generation, saved forecasts, independent settlement and history backfill against an isolated PostgreSQL database. It does not certify model profitability or place bets. Source account entitlements still need live validation.

## Local launch

Use a machine with Docker and Docker Compose, internet access and private provider keys in the project's `.env`. The launcher uses standard Python; the image installs the engine and PostgreSQL dependencies. Run from the project folder:

```sh
python scripts/pilot.py prepare
python scripts/pilot.py preflight
python scripts/pilot.py checks
python scripts/pilot.py start
python scripts/pilot.py report
```

The prepare command creates `.env.pilot` with separate random administrator/application passwords and private file permissions. It never prints or overwrites existing passwords and rejects incomplete existing files. Preflight writes `docs/pilot-readiness.json` with sanitized prerequisite status, including local report availability; it does not spend provider credits or start services. The checks command builds a separate test image, starts the database and runs the full Python pytest suite, including actual PostgreSQL release tests, against `xpredict_pilot_test`. That test database is deliberately cleared between integration tests; the application's `xpredict_pilot` database is separate. Checks do not receive provider credentials, and pytest is excluded from the ordinary runtime image.

Start runs web and independent generation/settlement/history threads. Visit **http://localhost:8090**. The ordinary deployment port, database and volumes are separate. The database has no public or host port. The application role owns the pilot databases but cannot create other databases/roles or act as a PostgreSQL superuser. The private provider credential file is on the pilot application-data volume; admin replacements inside this pilot do not alter another deployment's credential store. Initial keys come from private `.env`.

Report reads recorded evidence without provider requests and writes `docs/pilot-live-report.json`. A nonzero exit code can mean that operational blockers are present; read the saved report. A newly started pilot may have no publication yet while providers/history load. Fix reported failures rather than interpreting an empty report as readiness. Stop without deleting evidence using `python scripts/pilot.py stop`. Repeated checks do not delete the main pilot database. Keep `.env.pilot` with the same passwords when restarting existing volumes.

## Paper mode and evidence

The pilot command and Docker configuration force paper mode. Ordinary runtime edits cannot disable it, restore an approved model artifact, activate the legacy odds client or authorize stakes. Saved individual/accumulator stake fractions and forecast-ledger recommendations are zero. No bot or notification delivery service is launched.

Schema migration 5 adds indexed cycle records for generation, settlement and backfill. A start is committed before execution, then completed once with timestamps, duration, safe state/count fields and a failure flag, excluding provider exception text. Starts without completion after ten minutes are reported separately from fresh active cycles, preserving evidence of crashes and interrupted runs. Supporting history failures remain visible even when primary history succeeds. Existing migrations add match observations, shared quota ledgers and odds history. A report distinguishes a missing daily publication from a saved empty slate, records micro-pick/accumulator counts, grades by market, overdue unsettled forecasts, stale job evidence, odds-observation coverage and stake violations. Completed days are assessed only after the recorded pilot began.

Keep the pilot running through actual kickoffs and final results, then rerun the report. Operational acceptance requires PostgreSQL checks passing, recurrent publications, current evidence from all jobs, dated prices, zero stake violations and resolved late settlements. Forecast totals and UI counts are evidence of publication, not verified market completeness. Test the dashboard manually as well: daily forecasts, micro markets, accumulators and previous results should match the stored report; missing statistics/odds remain explicit.

Paper result counts are not financial returns. Price quality, calibration, bookmaker baselines, chronological holdouts, uncertainty and realistic payouts require separate empirical evaluation. The existing profitability evidence gate remains unsatisfied. Months of observations may be needed for adequate per-market samples; a few days of operational success cannot approve stakes.

## Existing isolated PostgreSQL

If using a separate managed database, put its connection URL in private `.env` as `LISA_PILOT_DATABASE_URL`. The database name must end in `_pilot`; the command refuses a generic production database. Install the PostgreSQL extra (`python -m pip install -e './engine[postgres]'`). `python -m lisa pilot --once` executes one bounded sequence; `python -m lisa pilot` runs continuously; `python -m lisa pilot --report` reads evidence. These commands initialize application tables but never truncate the pilot database. Set `LISA_TEST_POSTGRES_URL` to a different database ending in `_test` only for destructive integration tests.

## Current validation

The sandbox can run SQLite lifecycle tests and validate Compose configuration, but cannot reach providers, install psycopg from the network or access the Docker daemon. No PostgreSQL/live pilot has been started here. The local direct OddsPapi report has not yet been supplied. The existing The Odds API local report recorded a failed sample with no prices; its account access remains unresolved. See `pilot-readiness.json` for prerequisite status and return `pilot-live-report.json` after running locally.
