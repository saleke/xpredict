# Hybrid deployment: Vercel jobs + a native Scalper worker

The Vercel deployment serves the dashboard and runs **finite** jobs
(generation, settlement, history backfill) on cron invocations; no permanent
worker survives between them. The HTTP Scalper (ESPN scoreboards plus
Openfootball bulk history) cannot run that way: it is a bounded but *continuous*
collector that scrapes public endpoints on its own clock, and the browser
adapters need a real local Chromium. The hybrid shape runs that collector as a
**native worker on an always-on host**, writing into the **same Supabase
database** the Vercel functions read. See `architecture/SYSTEM.md` and
`scalper/DESIGN.md` for why the board gains corners, shots, cards and final
scores from ESPN at no API cost.

```
        public HTTP (ESPN, Openfootball)
                     |
        [ native worker: python3 -m lisa.scalper run ]
                     |
                     v
        Supabase PostgreSQL  -- lisa_private schema --
                     ^
        [ Vercel: dashboard + finite generation/settlement/history jobs ]
```

## Why a schema flag

Both sides must agree on where data lives. The storage layer chooses its schema
by context:

- Vercel functions construct `PostgresStorage(..., serverless=True)` and use
  schema `lisa_private`.
- The native worker defaults to the local/public schema (its classical SQLite
  and Compose audiences). Point it at Supabase and set
  `LISA_SCALPER_PRIVATE_SCHEMA=1` so it also builds
  `PostgresStorage(..., serverless=True)` and writes/reads `lisa_private`.

If the flag is missing, the worker silently writes a separate `public` schema and
the board never sees its fixtures, corners or results.

## Worker environment

Run the worker with the **same** values the Vercel deployment uses for the
database, encryption and credentials backend, so state and overrides agree:

| Variable | Value |
| --- | --- |
| `LISA_STORAGE` | `postgres` |
| `LISA_DATABASE_URL` | Supabase **Transaction pooler** URL (port 6543), `sslmode=require`; same URL as Vercel |
| `LISA_SCALPER_PRIVATE_SCHEMA` | `1` — routes the worker into `lisa_private` |
| `LISA_SCALPER_MODE` | `supporting` — existing API calendars/odds stay active; the worker feeds fixtures, corners, shots, cards and results |
| `LISA_PROVIDER_CREDENTIALS_BACKEND` | `postgres` |
| `LISA_CREDENTIAL_ENCRYPTION_KEY` | Same Fernet key as Vercel (encrypted overrides must decrypt) |
| `LISA_ENABLE_SHARPAPI` / provider keys | Copy from `.env` as needed; the worker's cycle is primarily the Scalper, not the API adapters |

The worker never serves picks and never holds stakes. Paper mode on the
deployment side remains unconditional (`LISA_PAPER_MODE=1` and the Vercel
handler enforce zero stakes).

## Start and verify

```bash
# One bounded collection cycle against Supabase:
LISA_SCALPER_PRIVATE_SCHEMA=1 python3 -m lisa.scalper once --leagues soccer_epl

# Continuous worker (all 14 ESPN leagues), 60s between cycles:
LISA_SCALPER_PRIVATE_SCHEMA=1 python3 -m lisa.scalper run --interval 60

# Health/lease/request-state of the shared repository:
python3 -m lisa.scalper status
```

ESPN league keys the worker knows: `soccer_epl`, `soccer_england_championship`,
`soccer_germany_bundesliga`, `soccer_germany_2_bundesliga`, `soccer_italy_serie_a`,
`soccer_spain_la_liga`, `soccer_france_ligue_one`, `soccer_netherlands_eredivisie`,
`soccer_portugal_primeira_liga`, `soccer_uefa_champions_league`,
`soccer_scotland_premiership`, `soccer_belgium_first_division`,
`soccer_greece_superleague`, `soccer_brazil_serie_a`.

Then confirm from the deployed dashboard (or `scripts/run_deployed_jobs.py
--verify-data`) that fixtures carry `scalper:espn:...` identities and corners
start appearing in the priced feed. Settlement consumes the ESPN final scores;
verify later final scores and ledger grades appear without delay.

## Supervision

`service.tick()` holds a lease, bounds request volume per cycle, and is safe as
a `systemd`/`cron`-restarted process. Prefer one supervised process over
many: the repository lease prevents overlapping collectors from the same
account. A plain `cron` line that runs `once` every minute is an acceptable
fallback if a supervisor is unavailable.

## Optional garnish: browser quote capture

Pinnacle/SportyBet browser capture (`--browser-bookmaker`) requires Chromium and
an always-on host and is deferred; API price adapters (SharpAPI, OddsPapi,
API-Football odds) supply prices instead. The browser route is documented in
`scalper/README.md` and is not needed for the hybrid feed.