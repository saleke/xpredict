# Vercel production testing

The deployment serves `web/` assets through Vercel's CDN and routes `/api/*`
to the native Python request handler. Visitors read committed database state.
Authenticated finite functions run generation, settlement and history backfill;
no permanent worker is expected to survive between invocations. This entry point
enforces paper mode and zero stakes throughout production testing.

Use a **dedicated Supabase PostgreSQL project** for this test deployment.
The application connects directly through the transaction pooler, with one
connection per warm function instance, automatic prepared statements disabled,
SSL required, and transaction-local timeouts/search paths.
[Supabase recommends transaction pooling for serverless clients](https://supabase.com/docs/guides/database/connecting-to-postgres).
Choose a Vercel function region near the database in the Vercel project settings.

Application tables live in `lisa_private`, which is outside the default Supabase
Data API schema and denies schema access to PUBLIC. Keep this schema out of the
Data API's exposed schemas; disable the project's Data API if it has no other
consumer. The app uses its Python authentication endpoints; it does not require
Supabase Auth, anon keys or service-role HTTP keys. Automatic migrations are
serialized by a transaction advisory lock and currently apply versions 1–6.
The connection role needs permission to create and own this private schema.

## Required Vercel Production environment variables

Set these in Vercel's **Environment Variables**, not in committed files:

| Variable | Value / purpose |
| --- | --- |
| `LISA_DATABASE_URL` | Supabase **Transaction pooler** URL (port 6543), including database password and `sslmode=require`; percent-encode password characters as needed |
| `LISA_CREDENTIAL_ENCRYPTION_KEY` | Fernet key for encrypted admin provider overrides; preserve securely across redeployments |
| `CRON_SECRET` | Random secret of at least 32 characters; the scheduler sends `Authorization: Bearer ...` |
| `LISA_ADMIN_EMAILS` | One owner email; bootstrap reserves it before public registration |
| `LISA_OWNER_PASSWORD` | Initial owner password, at least 16 characters; existing accounts require ownership proof and are not silently reset |
| Provider keys | Copy the configured provider variables from your private `.env` into Vercel; local files are excluded from deployment |

Provider variables are `ODDSPAPI_KEY`, `THE_ODDS_API_KEY`,
`FOOTBALL_DATA_TOKEN`, `API_FOOTBALL_KEY`, `ALLSPORTS_API_KEY`, and
`SHARPAPI_KEY`. Confirm exact existing adapter enablement and quota variables
against `.env.example`, especially `LISA_THE_ODDS_ENABLED=1` for the supporting
The Odds API adapter. The legacy `LISA_ENABLE_ODDS_API` poller stays disabled.
Direct OddsPapi is the configured adapter; a RapidAPI key alone does not enable
that direct transport.

Generate a Fernet key using `Fernet.generate_key()` from the installed
`cryptography` package. Generate cron and owner secrets with
`secrets.token_urlsafe(32)`. Store them privately and transfer them through the
provider dashboards. Losing the encryption key makes saved overrides unreadable;
changing it requires an explicit re-encryption or override recovery procedure.
Do not change it casually during API-provider key rotation.

`LISA_STORAGE=postgres`, `LISA_PROVIDER_CREDENTIALS_BACKEND=postgres`, and
`LISA_PAPER_MODE=1` can be set for clarity; the Vercel adapter enforces these
choices. Provider overrides survive function restarts in encrypted table rows.
Admin reads never return their values. In-flight requests finish with their
previous configuration; subsequent jobs reload credentials. Account quota
counters are shared and are not reset by replacing a key.

Do not copy Production database credentials into Preview environments. Previews
need their own database and owner/encryption secrets if their APIs are tested.
All cron requests fail closed unless `VERCEL_ENV=production` and the shared
secret matches.

## Scheduling

The checked-in Vercel cron configuration is compatible with Hobby: each job runs
once daily, providing a daily generation/settlement/backfill fallback. Hobby
cron timing can vary within the scheduled hour. Pro permits more frequent
expressions. [Vercel cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing).

For useful score refresh and prompt settlement on Hobby, use **Supabase Cron**
with `pg_net` to call these Vercel functions. The code is prepared in
`deploy/supabase-scheduler.sql`. Enable Cron, pg_net and Vault through Supabase's
dashboard. In Vault, create:

| Vault name | Value |
| --- | --- |
| `xpredict_origin` | Stable deployment origin, such as `https://your-project.vercel.app`, without a trailing slash |
| `xpredict_cron_secret` | The same value as Vercel's `CRON_SECRET` |
| `xpredict_vercel_bypass` | Optional deployment-protection automation bypass secret, if the stable origin is protected |

Run the prepared SQL in the dedicated project's SQL Editor. It schedules
history hourly at minute 7, generation hourly at minute 17, and settlement every
15 minutes. Named schedules can be updated by rerunning the SQL. Requests are
asynchronous and the database does not hold an application transaction while
the provider work runs. Scheduler requests allow up to 300 seconds; the app
has a 210-second cooperative work budget, small resumable history batches, and
durable leases to prevent overlapping work. Vercel's configured maximum duration
is 300 seconds. [Supabase Cron](https://supabase.com/docs/guides/cron),
[Vault-backed HTTP scheduling](https://supabase.com/docs/guides/functions/schedule-functions),
[Vercel function limits](https://vercel.com/docs/functions/limitations).

Cadence guards prevent redundant invocations from spending provider quota.
The engine's provider caches and reserve budgets still constrain actual data
freshness: a 15-minute settlement trigger does not imply a 15-minute provider
refresh. Initial API-Football corner backfill is capped at three statistic
requests and one season per function invocation, with durable checkpoints.
Model fits are cached as validated JSON parameters keyed by data, configuration
and date to avoid repeating unchanged fits on cold starts.

Cron's SQL success means a request was queued, not that the application job
completed. The SQL file includes a query for HTTP status/timeouts. The app's
Production testing page records starts, completions, failures and interrupted
runs. Hard function termination, DNS blocking or a slow server can still defeat
cooperative deadlines; incomplete runs remain visible and leases eventually expire.

A GitHub Actions scheduled workflow is supplied as an optional hourly fallback,
disabled until `LISA_SCHEDULER_ENABLED=1` is set. Configure the stable
`LISA_DEPLOYMENT_URL` repository variable and `CRON_SECRET` secret to use it.
Prefer Supabase Cron for frequent delivery: Actions schedules can be delayed and
runner minutes count against account limits.
[GitHub scheduling behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## CI/CD

The root directory is the repository root; the preset is **Other**. The build
command and output directory are in `vercel.json`. Python 3.12 is pinned and
dependencies install from root `requirements.txt`. Public assets are built by
`scripts/build_vercel.py`, which excludes private data, reports and tests.
`.vercelignore` also excludes local environments and credentials.

Use the dedicated Vercel project **xpredict** for `saleke/xpredict`. Before
transferring secrets or deploying, verify the linked project ID in
`.vercel/project.json` and inspect the remote project with
`vercel project inspect xpredict --scope alek15`. An existing local link does not establish that the
project belongs to this repository. Keep unrelated projects and their domains
separate.

The production-testing origin is **https://xpredict-three.vercel.app**. Its
Vercel project ID is `prj_cXSQvKFhjWPxoFPw4EmjMNmPOFjw` in scope `alek15`.
Use this primary domain for public checks; generated deployment URLs and the
team alias can require Vercel authentication even after promotion.

Keep an explicit `class handler(VercelHandler)` in `api/index.py`. Vercel's
Python source detector does not recognize an assignment alias such as
`handler = VercelHandler`; that produces an unmatched-function build error even
when the file exists. The static `public/` build and native Python function work
together; no services configuration or catch-all function rewrite is required.
For a local prebuilt deployment, install `uv` and put it on `PATH`, alongside the
Vercel CLI. The Python builder uses it to install the pinned runtime and dependencies.
The root requirements line names the local distribution explicitly:
`lisa-engine[postgres,cloud] @ ./engine`. A bare `./engine[postgres,cloud]`
is inferred as a package named `engine` by Vercel's requirements-to-project
conversion, which conflicts with the declared distribution name `lisa-engine`.

The GitHub workflow runs Python tests against real PostgreSQL 18, frontend
checks and public packaging. A deployment job depends on all these checks. It
builds once, stages the prebuilt production artifact without assigning the
stable domain, verifies API/database configuration, paper mode, static HTML,
private-path isolation and cron authentication, and then promotes that artifact.
The pinned Vercel CLI version is 59.11.7. Actual Vercel build/routing and database
checks must pass against a network-accessible deployment.

Create the Vercel project and a GitHub environment named `production-testing`.
Set GitHub secrets `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, and,
if staged deployments are protected, `VERCEL_AUTOMATION_BYPASS_SECRET`.
Set repository variable `VERCEL_CD_ENABLED=1` when ready to activate deployments
from pushes to main. The Git-based Vercel auto-deployer is disabled in
`vercel.json` to avoid bypassing the test gate or duplicating deployments.
Protect main with the engine check if PRs are used.
[Vercel's CI pattern](https://vercel.com/kb/guide/how-can-i-use-github-actions-with-vercel),
[staging without domain assignment](https://vercel.com/docs/cli/deploy#skip-domain).

Schema creation can occur during a staged smoke test. Future database changes
must remain compatible with the active deployment or use an explicit migration
plan; code promotion/rollback does not revert database data or migrations.

## First live acceptance run

From an ordinary terminal, supply the stable HTTPS origin as
`LISA_DEPLOYMENT_URL` and matching `CRON_SECRET` privately, then run:

```bash
python3 scripts/run_deployed_jobs.py --smoke
python3 scripts/run_deployed_jobs.py
```

The second command uses Python `requests` to call history, generation and
settlement sequentially. It prints only safe status/counter fields and refuses
redirects carrying secrets. It can also run one job with `--job settlement`.
You can trigger due jobs as the owner from the admin Production testing page.

Verify rendered winning/earning/micro/accumulator sections, dated and matched
odds, later final scores and ledger grades, provider credit use, missed daily
publications, phase durations and interrupted runs. Collect a multi-day record,
including quiet days, provider outages and delayed/corrected results. The smoke
check proves deployment wiring, not the quality of predictions or every browser
interaction. The current model's profitability remains unapproved.

This system offers research forecasts for supported pre-match markets. It does
not execute wagers, approve same-game accumulators, or promise unsupported
minute-event bets, player/card props, first-half prices or team corner handicaps.
Model-only opportunities must retain that label. Supabase, Vercel and provider
resource usage must be observed before choosing final coverage and cadence.
