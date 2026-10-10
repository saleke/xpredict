# Checks and acceptance

Install the engine/test dependencies from the repository root as described in
[local operation](LOCAL_RUNNING.md). Offline checks use isolated fixtures and
databases; they do not prove live provider access or profitable forecasts.

## Automated checks

```bash
PYTHONPATH=engine python3 -m pytest engine/tests
node web/tests/test_model_board_render.js
node web/tests/test_render_all_robustness.js
node web/tests/test_accumulator_math.js
node web/tests/test_dashboard_updates.js
python3 scripts/build_vercel.py
```

Real PostgreSQL tests run only when `LISA_TEST_POSTGRES_URL` identifies a
**disposable database whose name ends in `_test`**. They clear that database.
Without it, PostgreSQL-specific checks skip. CI provisions PostgreSQL 18;
[the Docker pilot](PAPER_PILOT.md) provides a local isolated option. Review skips
and failures rather than treating collected test counts as permanent evidence.

For a focused offline publication/settlement check:

```bash
PYTHONPATH=engine python3 scripts/audit_offline.py
```

Scalper parsing, persistence, source cooldowns, identity ambiguity, revocation,
quote timestamps and browser contracts have regressions in
`engine/tests/test_scalper_unittest.py` and `engine/tests/test_scalper_browser.py`.
Price-readiness and pick-feed tests cover expiry and selection/access policies.

## Live deployment smoke

```bash
LISA_DEPLOYMENT_URL=https://xpredict-three.vercel.app python3 scripts/run_deployed_jobs.py --smoke
```

This checks API access, PostgreSQL storage, enforced paper mode, dashboard HTML,
private-path isolation and unauthenticated cron rejection. It does **not** run
provider jobs. The production domain passed these checks on 2026-10-10.
That wiring result did not establish that any collector had run or data had
been published. Use the separate data check:

```bash
LISA_DEPLOYMENT_URL=https://xpredict-three.vercel.app python3 scripts/run_deployed_jobs.py --verify-data
```

It requires recent successful generation, a saved publication and observed
calendar fixtures. Zero picks can be correct when no real offer passes the
quality gates; missing/stale collection is a failed check.
For a protected staging URL, provide the private
`VERCEL_AUTOMATION_BYPASS_SECRET`; never paste it into a URL or report.

The same script without `--smoke` invokes history, generation and settlement and
can spend quota. Run those jobs intentionally with a matching private
`CRON_SECRET`; `--job` narrows the scope. See [deployment](VERCEL.md).
Use `--bootstrap` to run all three jobs and require the saved-data check after
them. A cold start batches match-history and pick writes rather than issuing
one remote SQL request per record. Failed serverless dispatches use a five-minute
retry gate; healthy generation/history dispatches retain their hourly cadence.

## Operational and model acceptance

Verify that jobs complete across restarts/outages, sources expose failures,
publications remain immutable, expired prices lose opportunity status and final
results settle the exact saved contracts. Check current-price coverage separately
from calendar/model coverage. Browser rendering and tier/manual grading checks
are described in [manual verification](MANUAL_VERIFICATION.md).

Historical model metrics and live infrastructure checks establish different
facts. See [models and evaluation](../architecture/MODELS.md). Collect a dated,
chronological record before making accuracy or return claims.

## Reports and cleanup

Generated provider, pilot and model reports go in ignored `data/reports/`.
The previous one-off reports are kept locally under `data/reports/legacy/`.
The one retained versioned model baseline is linked from the model guide.
Do not commit credentials, runtime databases, manual grading sheets or full
provider responses as validation evidence.

Disposable local outputs include `public/`, `.pytest_cache/`, Python bytecode,
and failed `.vercel_python_packages/` builds. Keep the virtual environment,
private `.env*`, active `.vercel/project.json`, database/WAL state and grading
sheets when cleaning a working installation.
