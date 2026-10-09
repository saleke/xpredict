# Watching the local paper trial

The current dedicated Scalper trial is at `http://localhost:8080` and has
temporary all-tier read access for manual verification, enabled with
`--unlock-tiers`. It displays an explicit paper-verification banner. This
2026-10-06 trial supersedes the historical startup limitations recorded below.
The [current selection and verification policy](PICK_FEED_POLICY.md) documents
the access switch and frozen grading sheets under `data/manual-grading/`.
Grade each exact market and line individually, then compare with the Ledger;
paper stakes remain zero. Restart without the flag to restore normal tier access.

This checklist tests the data pipeline and public behavior. Model skill and
profitability require separate chronological evaluation and dated bookmaker
quotes. A single winning or losing prediction does not certify either.

## Restart and baseline

Stop the existing launcher with Ctrl+C in its own terminal and wait for its
children to stop. From the project root, using the existing virtual environment:

```bash
source .venv/bin/activate
python3 scripts/local_paper.py
```

Keep this terminal running. Open `http://localhost:8080` and
`http://localhost:8080/admin`; reload the browser with its cache bypassed. The
launcher starts the web process first, checks local readiness, then starts a
worker process containing generation, settlement and history jobs. It preserves
the existing database, predictions, quota reservations and local owner login.

The coding workspace cannot perform this host restart: its attempt on
2026-10-04 was rejected by local socket permissions before a server or worker
started. The previously running host process was not stopped or replaced.

The read-only baseline on that date had seven saved, seven settled predictions,
1,568 historical observations and no upcoming picks. Saved generation was
degraded and history failed. Those old seven predictions are not evidence that
the new Openfootball collector has run.

## Observe these outcomes

| Stage | Success | Report a problem when |
|---|---|---|
| Startup | Dashboard and admin open; the launcher prints its dashboard address and common web/worker database | The launcher exits, the port is occupied, the page cannot load, or one child stops |
| Scheduling | Generation and settlement start on boot and complete repeatedly while the browser is closed | Recorded starts lack completions for ten minutes, or generation/settlement evidence stops advancing for more than three configured intervals |
| History | Supported Openfootball results appear under `history_by_source`; subsequent bounded backfills progress or wait on documented cooldowns | No bulk results are ever collected, or errors persist across the daily cooldown and retry |
| Fixtures | Timed, verified fixtures show the correct teams, league and kickoff; date-only file rows remain provisional | Duplicate matches, incorrect clubs/dates, invented midnight kickoffs or canceled fixtures appear as selections |
| Forecasts | Cold start and each generation cycle automatically reach the nearest verified fixtures with trained teams; earliest kickoffs precede later ones, with stronger probabilities first at the same kickoff | A fixed 24/48-hour or seven-day limit hides available forecasts, a later kickoff displaces an earlier one, or the public page fails to show a saved publication |
| Bookmaker value | Accepted prices identify the exact market, selection and line; earning picks appear only when prices and model gates qualify | An unpriced pick advertises EV, a price belongs to another match/line, or qualifying persisted value picks never render |
| Accumulators | Qualified research combinations meet the adjusted probability threshold and disclose their research status | Repeated matches, unjustified probability increases, or research combinations presented as verified executable offers |
| Kickoff | A saved prediction stops being offered as upcoming and remains in the ledger as awaiting a confirmed result | It disappears from the ledger or is graded from a provisional file/scheduled score |
| Settlement | Confirmed results grade each supported market correctly; pending counts fall and history remains available | Wrong WIN/LOSS/VOID or split grades, changed original predictions, or picks still pending more than 24 hours after kickoff without a stated provider/coverage problem |
| Restart | Original picks, settled results and quota counters survive; cached season files are reused | Data disappears, duplicate original picks appear or account usage resets |

Generation and settlement use `daily_interval_sec` after each completed cycle,
default 300 seconds. History runs on boot and then waits about one hour between
batches. Existing environment/admin overrides can change generation cadence.
At the default, generation or settlement evidence older than about 15 minutes
needs investigation; history evidence older than about three hours also needs
investigation. A slow provider run adds its own bounded duration to the interval.

The public page and **Admin → Production testing** refresh saved observations
about every 20 seconds while visible. Provider caches and quotas determine when
new source data arrives; that screen refresh is not a live-event feed. A
generation heartbeat alone does not establish current bookmaker freshness.
Check worker completion times, provider failures, source coverage and quote
timestamps together. `/api/health` shows web liveness; `/api/ready` separately
checks stored publication/settlement readiness.

An empty day can be correct when no eligible fixtures or qualifying prices
exist. A daily publication with zero counts is different from a missed daily
publication. Empty earning/accumulator lists can reflect absent or stale odds;
unproven labels can reflect sparse history. These states must remain visible.
Missing corners/card/player observations must not turn into fabricated picks.
Paper mode must retain zero recommended stakes.

The local Operations summary also lists blockers for PostgreSQL validation and
previous failed cycles in its reporting window. These can remain after the
current workers recover. Inspect the newest completion and error counts rather
than using that single summary label as proof that the launcher failed.

## Reporting an anomaly

Run in a second terminal from the project root:

```bash
python3 scripts/local_paper.py --diagnose
```

This read-only report includes source history counts, job timestamps, failed
provider names, saved/settled/pending counts and public board categories. It
withholds credentials and provider error text. Send this report with the time
in Africa/Lagos, the page/market affected, expected behavior and observed
behavior. For a grading or identity issue, include the displayed fixture ID,
teams, kickoff, selection and line. Do not send private owner-login files or API
keys. A reproducible mismatch is more useful than a total win/loss count.
