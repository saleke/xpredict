# xPredict (LISA)

xPredict collects football schedules, results and bookmaker prices, evaluates
supported markets, and publishes a curated pick feed with an auditable ledger.
The feed selects one qualifying headline per match, then displays matches in
kickoff order. More calculations do not mean more published picks.

Production testing: **https://xpredict-three.vercel.app**. This deployment uses a
single Python serverless function, static dashboard assets and Supabase
PostgreSQL. Paper mode is enforced; stakes are zero. Deployment smoke checks
verify the application wiring, not predictive accuracy or profitability.

## Start locally

From the repository root, with Python 3.12 recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e './engine[test,postgres,cloud,validation]'
```

For a fresh checkout, copy `.env.example` to `.env` and fill the fields needed
for your providers. Preserve an existing private `.env`.

```bash
python3 scripts/local_paper.py
```

Open `http://localhost:8080` and `/admin`. The launcher uses a persistent local
SQLite ledger and starts the web process and independent workers. It creates
private local owner credentials when none were configured. Use `--check` for
prerequisites, `--diagnose` for saved-state diagnostics, or `--unlock-tiers` for
temporary all-tier paper verification. See [local operation](docs/operations/LOCAL_RUNNING.md).

## How it works

1. Provider adapters and the optional independent Scalper workers collect
   observed schedules, history, results and exact bookmaker offers.
2. League goal models and the separate corner model evaluate supported
   contracts. Models, sample coverage and price availability are distinct.
3. The curated feed rechecks price freshness and applies the configured odds,
   probability and value gates. Defaults are offered odds at least **1.18**,
   positive-payout probability at least **55%**, and model EV at least **3%**.
4. Publication saves the board and original ledger observations transactionally.
   Web requests read saved state; they do not fetch providers or fit models.
5. Independent settlement jobs grade exact market contracts using confirmed
   final results. Missing results remain pending.

The default headline feed has no numerical cap and never pads a short slate.
Free and paid tiers share the same quality floor; access controls determine
coverage and qualifying alternatives. Cross-match accumulators remain research
combinations until a bookmaker confirms a combined offer.

Scalper can supplement providers or supply the stored calendar/price bridge on
its own. Persistent collectors run on a separate host, not inside a Vercel
request. See [Scalper setup and contracts](docs/scalper/README.md).

## Documentation

Start with the [documentation index](docs/README.md).

| Task | Guide |
| --- | --- |
| Deploy or smoke-test Vercel | [Production testing](docs/operations/VERCEL.md) |
| Run Docker/PostgreSQL locally | [Paper pilot](docs/operations/PAPER_PILOT.md) |
| Understand storage and workers | [System architecture](docs/architecture/SYSTEM.md), [database](docs/architecture/DATABASE.md) |
| Integrate a frontend | [API contract](docs/architecture/FRONTEND_API.md), [frontend development](docs/frontend/DEVELOPMENT.md) |
| Understand pick quality and tier access | [Pick-feed policy](docs/product/PICK_FEED.md) |
| Configure data supply | [Provider roles](docs/providers/DATA_SOURCES.md), [Scalper coverage](docs/scalper/COVERAGE.md) |
| Run checks or grade paper picks | [Validation](docs/operations/CHECKS.md), [manual verification](docs/operations/MANUAL_VERIFICATION.md) |

## Checks

```bash
PYTHONPATH=engine python3 -m pytest engine/tests
node web/tests/test_model_board_render.js
node web/tests/test_render_all_robustness.js
node web/tests/test_accumulator_math.js
node web/tests/test_dashboard_updates.js
python3 scripts/build_vercel.py
```

Real PostgreSQL tests need `LISA_TEST_POSTGRES_URL` pointing to a disposable
`*_test` database; they clear that test database. The CI workflow provisions its
own PostgreSQL instance. Provider probes can spend quota and are separate from
offline tests. Generated reports belong under ignored `data/reports/`.

## Repository

| Path | Purpose |
| --- | --- |
| `engine/lisa/` | Models, providers, storage, jobs, authentication and HTTP handlers |
| `engine/tests/` | Python regression and PostgreSQL integration tests |
| `api/index.py` | Vercel Python entry point |
| `web/` | Dashboard, operator console and browser tests |
| `scripts/` | Local launchers, builds, validation, import and research tools |
| `deploy/` | Compose/systemd support and Supabase scheduler SQL |
| `docs/` | Maintained guides and explicitly dated research |
| `data/` | Private local databases, observations, grading sheets and reports; ignored |

The current prediction product is football oriented. Collecting cards, fouls,
throw-ins, player observations or half-time results does not supply validated
models or executable offers for those markets. Booking codes, verified paid
checkout, live micro-betting and executable same-game accumulators are not
completed product capabilities. See [current boundaries](docs/architecture/SYSTEM.md).
