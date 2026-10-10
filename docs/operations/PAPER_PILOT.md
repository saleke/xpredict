# Isolated PostgreSQL paper pilot

The Docker pilot separates its application database from a disposable test
database. Keep private provider settings in `.env`; the launcher creates and
preserves independent credentials in `.env.pilot`.

From the repository root:

```bash
python3 scripts/pilot.py prepare
python3 scripts/pilot.py preflight
python3 scripts/pilot.py checks
python3 scripts/pilot.py start
python3 scripts/pilot.py report
```

Preflight records prerequisites and available local provider-probe evidence in
`data/reports/pilot-readiness.json`. Missing reports are unknown access, not
successful validation. It checks local prerequisites without spending provider
credits or starting services. Generate price reports using the
[provider probes](../providers/VALIDATION.md) when needed.

`checks` builds the test image and runs the Python suite against
`xpredict_pilot_test`, which is deliberately cleared between integration tests.
It does not receive provider credentials. The application's `xpredict_pilot`
database is separate. Never point a test URL at the application database.

`start` runs the isolated paper web/worker/database stack. Read **Production
testing** in the console for starts, completions, publications, score observations
and settlement. Paper mode keeps stakes at zero. Bootstrap or provider outages
can leave the board empty without proving the process failed.

`report` reads recorded evidence without provider requests and saves
`data/reports/pilot-live-report.json`. A nonzero status can indicate operational
blockers; inspect the dated report rather than treating empty output as ready.
A report does not certify model profitability.

```bash
python3 scripts/pilot.py stop
```

Stop preserves evidence and database volumes. Keep the same `.env.pilot`
passwords when restarting existing volumes. Do not delete a volume to rebuild
the app. An existing isolated PostgreSQL installation can use the equivalent
engine pilot commands; inspect `python3 -m lisa pilot --help` and ensure its
connection URLs identify the intended application and disposable test databases.

See [database operations](../architecture/DATABASE.md),
[local operation](LOCAL_RUNNING.md), and [checks](CHECKS.md).
