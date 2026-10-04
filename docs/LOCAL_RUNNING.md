# Run locally without Docker

Use an ordinary Linux or macOS terminal on your computer (or WSL on Windows).
This coding workspace denies both listening sockets and outbound provider
requests. Changing from curl to Python requests cannot change that policy.
The launcher does not need Docker, PostgreSQL binaries, or a paid hosting plan.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e './engine[postgres,cloud,validation]'
python3 scripts/local_paper.py
```

Keep provider keys in the private `.env`. `.env.example` is a blank template.
Existing owner-controlled provider overrides in `data/provider-credentials.json`
also apply. Neither file is copied into the public site.

Open `http://localhost:8080` for the dashboard and
`http://localhost:8080/admin` for the admin console. The launcher creates
`data/localhost-owner.env` with a local owner email and a random password,
preserving them on subsequent runs. Open that private file locally to obtain
your login. If `LISA_ADMIN_EMAILS` and `LISA_OWNER_PASSWORD` are already set,
the launcher uses those instead. A single bootstrap owner is supported.

The launcher starts the web server first, checks its health, and then starts the
three autonomous workers. Ctrl+C stops both processes. Provider authentication,
quota reservations, fixture collection, history backfill, model fitting, saved
boards, and final-result settlement use the actual engine. Stakes remain zero
and no Telegram bot starts. The functional ledger and usage counters persist
at `data/localhost-paper.db`; restarting does not reset them.

Use **Production testing** in the admin console to watch job completions,
daily publications, phase durations, fixture score observations, and settlement
counts. The page reads saved observations every 20 seconds while visible; it
does not trigger provider traffic. Workers and provider cache policies determine
when new scores arrive. A cached observation is labelled as such; minute-level
live coverage is not implied. The public board already exposes winning picks,
earning picks, supported micro markets, and accumulators. Prices and earning
opportunities still require eligible, fresh bookmaker quotes.

History backfill may take multiple cycles. An empty/unproven board during
bootstrap or a provider outage is evidence to investigate, not a substitute
prediction. Review provider errors, job records, cache ages and quota status
before treating the system as operational.

Openfootball bulk goal history is enabled by default without a key. Its files
are cached in the same database across restarts; it supports eight European
leagues and current/previous season bootstrap. A file date or clock string is
not an authenticated kickoff, so an official calendar feed still needs to verify
upcoming fixtures. TheSportsDB is now opt-in because its free calendar responses
can be clipped. Existing explicit `.env` settings take precedence over defaults.
See [LEAN_DATA_STACK.md](LEAN_DATA_STACK.md) for settings and coverage limits.

The forecast window now adapts automatically on every generation cycle,
including a fresh database and a cold start. `board_window_hours` (24 by default)
is the initial horizon. If it lacks the configured number of forecastable
fixtures (12 by default), the worker extends it to the nearest verified matches
with trained teams, rounded up to an hour. It stops once that volume is reached,
includes ties at the last kickoff, and uses all available forecastable fixtures
when fewer exist. No fixed 48-hour or seven-day ceiling excludes the next match.
The worker chooses the final window before a single price-fetch pass and board
build. The report exposes configured/effective hours, expansion and next kickoff.

An earlier local intervention saved a `board_window_hours=168` override. That
existing override remains the initial horizon; fresh databases use 24 hours
and automatically expand as needed. Admin → Settings can change the initial
horizon. No manual override is required for automatic discovery. Early forecasts
are paper research and should be reviewed again near kickoff; current prices
and model evidence still govern any value or stake claims.

Predictions are listed by earliest kickoff, then highest winning probability.
Each winning fixture keeps its strongest eligible selection. Positive expected
value is still required for earning picks. Accumulators order by their earliest
leg and adjusted joint probability; their legs follow kickoff order.

Daily Board defaults to **Next Matches**, including verified future fixtures
within the selected forecast horizon even beyond this week. Before training is
available, it still collects the nearest verified calendar fixtures. Today, Tomorrow
and This Week remain explicit date filters. An empty period links to Next
Matches when other dates are available. Restart the launcher once after this
code update, then reload the browser with its cache bypassed; future restarts
use the automatic policy without a manual refresh.

Validation: [nearest-fixtures-validation.json](nearest-fixtures-validation.json)
records a replay of the real saved data on a private backup. With the default
24-hour initial horizon it automatically reached 133 hours and produced 12
forecasts starting at the nearest kickoff, using no network requests. Separate
fresh-database/restart regressions verified autonomous startup without overrides.
The replay is not a new live provider or deployment test.

If the dashboard is empty, use this credential-safe, read-only check:

```bash
python3 scripts/local_paper.py --diagnose
```

It reports saved predictions, upcoming versus awaiting results, collected
observations, job states and failed provider names. It also detects a separate
`data/lisa.db`: starting `lisa start` alone can read that empty database while
the launcher worker writes `data/localhost-paper.db`. Stop the old local
processes and start `python3 scripts/local_paper.py` to keep web and worker on
the same ledger. After a code update, restart the launcher and reload the
browser with its cache bypassed.

For the restart procedure, success criteria and anomaly-reporting checklist,
see [LOCAL_TEST_ACCEPTANCE.md](LOCAL_TEST_ACCEPTANCE.md). The diagnostic command
also reports history counts by provider and validated worker timestamps.

The public dashboard polls saved data every 20 seconds, including forecasts
that were absent on its first load. Model research selections remain visible
without bookmaker prices. After kickoff, saved predictions appear in the
ledger as awaiting a confirmed result until the settlement worker grades them.
The Daily Board shows only saved fixtures and scores; a view change never
fetches providers. Price-dependent earning picks and accumulators can stay
empty when quote collection or freshness checks fail.

For a prerequisite check without starting services:

```bash
python3 scripts/local_paper.py --check
```

Choose another port with `--port 8090`. To exercise real PostgreSQL, provide a
dedicated testing database URL as `LISA_DATABASE_URL` in `.env` and run:

```bash
python3 scripts/local_paper.py --postgres
```

Native PostgreSQL mode uses the application's `public` schema; Vercel uses
`lisa_private`. These are separate ledgers and quota counters. Avoid running
the same provider account simultaneously against separate local and deployed
ledgers: free-tier limits apply to the account, not to each installation.
Do not point native testing at a production application's database.

Local functional tests do not prove Vercel routing, pooled PostgreSQL behavior,
scheduled delivery or model profitability. Those remain deployment acceptance
checks in [VERCEL_DEPLOYMENT.md](VERCEL_DEPLOYMENT.md).
