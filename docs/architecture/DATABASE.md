# Database architecture

PostgreSQL is the shared application database for hosted operation. Docker/CI
use PostgreSQL 18; Vercel connects to managed Supabase through its transaction
pooler. Native hosting separates web and worker processes; Vercel runs finite
authenticated jobs. SQLite remains an explicit local/offline backend.

```mermaid
flowchart LR
  Visitors --> Web[Web API and dashboard]
  Providers[Fixture, result and odds providers] --> Generation[Generation job]
  Providers --> Settlement[Independent settlement job]
  Generation --> DB[(PostgreSQL)]
  Settlement --> DB
  Web --> DB
  Telegram[Optional Telegram process] --> DB
```

## Implemented boundaries

- PostgreSQL stores authentication, sessions, the prediction ledger, final scores,
  publication snapshots, telemetry, verification records, bankroll profiles,
  notification outbox, and worker leases. Web and workers share those repositories.
- Each operation borrows its own connection from a bounded pool (maximum 10 per
  native process; one per warm Vercel instance). Pool checkout, connection establishment, statements, and locks have
  timeouts. Exceptions roll back before returning a connection to the pool.
- Shared business queries use bound values and portable SQL. Backend-specific
  schema, parameter syntax, locking, connections, and database inspection are
  explicit. PostgreSQL never inherits SQLite connection/file behavior.
- Versioned migrations 1–6 run transactionally under a PostgreSQL advisory lock;
  migration 5 adds recorded paper-pilot job cycles.
  An unversioned legacy PostgreSQL database or newer unknown schema fails startup
  rather than receiving an unsafe guessed migration. Preserve existing data;
  start new installations with a dedicated database and back up before migrations.
- Generation and settlement have independent leases. Lease acquisition uses an
  atomic conditional update; publication locks the generation lease row and checks
  ownership before atomically inserting forecasts and replacing the board snapshot.
  Provider network requests and model fitting occur outside write transactions.
- Generation preserves first-publication probabilities/prices and line identity.
  Settlement has its own thread and provider cache and does not require the model
  or the odds feed. Final results can still be delayed by provider cache/freshness.
- An admin refresh request is persisted, so the web process can signal a separate
  worker. Workers check it within five seconds of their next polling wait; it does
  not interrupt an already-running provider cycle. Runtime overrides are reloaded
  by the worker, and readiness checks both jobs in separate-worker mode.
- Redis is not required. Saved boards and small operational snapshots fit the
  relational database. Introduce a shared cache only after measuring a bottleneck.

Publication payloads remain JSON text and prediction timestamps remain canonical
UTC ISO text for compatibility with existing Python repositories. The indexed
ledger is relational. Typed timestamp/JSONB conversions would require a distinct
migration and corresponding repository changes; they are not silently assumed.

## Run the stack

Configure private `.env` with independent PostgreSQL admin/application passwords
and the desired provider credentials. Preserve existing passwords and volumes.
Existing local SQLite files are not automatically imported as production history.

```sh
docker compose config --quiet
docker compose up --build -d db web worker
curl --fail http://127.0.0.1:8080/api/ready
```

The stack builds PostgreSQL dependencies and the Python timezone-data fallback
into the image, so Africa/Lagos works even on minimal OS images. The database has
no published host port; web binds only to localhost. PostgreSQL 18 data uses the
`/var/lib/postgresql` volume, matching the
[official image](https://hub.docker.com/_/postgres). A non-superuser application
role owns the application database; admin credentials are confined to the DB
service. The initialization script runs only against a new database volume.
Do not delete the database volume to restart/rebuild the application.

Configure the desired [calendar and price adapters](../providers/DATA_SOURCES.md)
in `.env`. No-provider or provider-failure operation reports
unready; an HTTP-successful liveness endpoint is not proof of daily predictions.
Add TLS/reverse proxy and off-host backups before exposing a production service.

The optional Telegram profile runs one bot process (no provider ingestion):

```sh
docker compose --profile telegram up -d telegram
```

It requires real Telegram configuration. Its temporary conversation state and
payment-key store remain process-local; verified paid checkout remains disabled.
Do not scale bot polling to multiple replicas or promise restart-persistent billing.

## Backups and verification

For PostgreSQL, use pg_dump custom-format snapshots and test pg_restore on a fresh
isolated database. Example:

```sh
docker compose exec -T db pg_dump -U postgres -d lisa -Fc > lisa-backup.dump
```

The dump contains user data and requires private file permissions and off-host
storage. Prefer a managed PostgreSQL service with tested point-in-time recovery
when moving to production. The SQLite backup script applies only to SQLite.

[Pool behavior](https://www.psycopg.org/psycopg3/docs/advanced/pool.html) and
[PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html)
were checked against official documentation.

Run PostgreSQL integration checks only with `LISA_TEST_POSTGRES_URL` pointing to
a disposable database whose name ends in `_test`. CI provisions PostgreSQL 18;
the [paper pilot](../operations/PAPER_PILOT.md) supplies a local isolated option.
See [validation](../operations/CHECKS.md) for reproducible checks and skipped-test
conditions. Connectivity and a successful migration do not prove fresh data supply.

Systemd templates also use PostgreSQL and separate web/worker units. Adjust their
user and installation paths and provide a reachable PostgreSQL URL in the host
`.env`; the Compose-only internal hostname `db` is not a host-run connection URL.
See [Vercel production testing](../operations/VERCEL.md) for the hosted deployment,
its private schema, encryption key and transaction-pooler connection settings.
