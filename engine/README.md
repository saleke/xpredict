# LISA engine

The Python package behind xPredict. It contains football models, interchangeable
data providers, the Scalper supply component, shared SQLite/PostgreSQL
repositories, scheduled jobs, authentication and HTTP handlers.

Install from the repository root:

```bash
python3 -m pip install -e './engine[test,postgres,cloud,validation]'
PYTHONPATH=engine python3 -m pytest engine/tests
python3 -m lisa --help
```

The `postgres` extra installs database drivers; `cloud` installs credential
encryption support; `validation` installs the HTTP client for deployed checks.
The separate `scalper-browser` extra requires a compatible Chromium installation.
See [local operation](../docs/operations/LOCAL_RUNNING.md) for the application
launcher and [Scalper](../docs/scalper/README.md) for collectors.

| Area | Modules |
| --- | --- |
| Goal/corner modelling | `league_model`, `dixon_coles`, `corners`, `model_policy` |
| Market contracts and selection | `markets`, `market_policy`, `board`, `pick_feed`, `price_readiness` |
| Data supply | `providers/`, `scalper/`, `history_service`, `odds_history` |
| Publication and settlement | `daily_service`, `settlement_service`, `calendar_snapshot` |
| Persistence | `storage`, `postgres_storage`, `postgres_schema`, `provider_credentials` |
| Web, accounts and operations | `server`, `serverless`, `admin_api`, `auth`, `runtime`, `observability` |
| Legacy consensus/research | `shin`, `consensus`, `gate`, `pipeline`, `backtest`, `walkforward` |

The legacy consensus pipeline is separate from the active daily model-based
feed. Its historical backtest is not evidence for the current strategy.
See [system architecture](../docs/architecture/SYSTEM.md),
[model boundaries](../docs/architecture/MODELS.md), and the
[documentation index](../docs/README.md).
