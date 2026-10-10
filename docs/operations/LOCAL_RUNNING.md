# Local operation

Run commands from the repository root on a host that can listen locally and
reach the configured providers. Python 3.12 matches the deployment runtime.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e './engine[test,postgres,cloud,validation]'
```

On a fresh checkout, copy `.env.example` to `.env` and configure providers.
Preserve existing private configuration, database files and owner credentials.

```bash
python3 scripts/local_paper.py
```

Dashboard: `http://localhost:8080`. Console: `http://localhost:8080/admin`.
Use `--port 8090` for another port. Keep the terminal running; Ctrl+C stops
the web and worker processes.

## Credentials and saved state

The launcher creates private `data/localhost-owner.env` credentials only if no
single `LISA_ADMIN_EMAILS`/`LISA_OWNER_PASSWORD` owner was configured. Open that
private file locally for the login; never commit or share it.

SQLite paper operation persists to `data/localhost-paper.db`. Use this launcher
consistently: starting another command with `data/lisa.db` can show a different
empty ledger. Restarting does not reset predictions, result grades or quotas.
Configured provider overrides also apply. The launcher starts the web server,
checks it, then starts independent jobs. Paper stakes remain zero; it does not
start the optional Telegram bot.

```bash
python3 scripts/local_paper.py --check
python3 scripts/local_paper.py --diagnose
```

Prerequisites are separate from saved-state diagnostics. Diagnose reports saved
predictions, future/awaiting results, job timestamps, provider failures and
history counts without returning credentials.

## Forecasts and observations

The initial forecast horizon expands to the nearest verified forecastable
fixtures when fewer than the discovery target are available. The target does
not cap all fixtures within the selected window. Existing runtime overrides
remain effective; inspect settings rather than assuming template defaults.

The board and calendar display earliest kickoff first. Today/Tomorrow/This Week
remain explicit date filters; Next Matches can include later dates. Public tabs
refresh saved data, not providers. Model research can exist without bookmaker
prices; current offers and quality gates determine visible betting opportunities.
Price-dependent sections can honestly be empty.

Openfootball supplies cached goal history for mapped leagues without a key.
It cannot verify kickoffs or settle saved bets from file dates. History bootstrap
can take several cycles. Inspect **Production testing**, collection ages,
provider quota, trained teams and current price matches when a board is empty.
See [provider roles](../providers/DATA_SOURCES.md).

## PostgreSQL and Scalper

For an isolated native PostgreSQL test, configure a dedicated `LISA_DATABASE_URL`:

```bash
python3 scripts/local_paper.py --postgres
```

Native PostgreSQL defaults to the `public` schema; Vercel uses `lisa_private`.
These are separate ledgers and quota counters. Provider free-tier limits apply
to the account, so concurrent installations must not assume separate allowances.
Use [the Docker pilot](PAPER_PILOT.md) for isolated integration tests.

Scalper collectors are separate processes. Use the **same database and schema**
as the consumer and verify stored coverage before enabling `supporting` or
`only`. See [Scalper setup](../scalper/README.md).

For manual tier access use `--unlock-tiers`; restart without it to restore the
normal ladder. See [manual verification](MANUAL_VERIFICATION.md). Local success
does not prove serverless routing, scheduled delivery or profitability; use
[deployment checks](CHECKS.md) for those separate boundaries.
