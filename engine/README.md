# LISA engine

The data-refinery core of **LISA**: ingest multi-book odds → strip bookmaker margin with
Shin's method → weighted consensus → quality gate → write-once ledger with auto-settlement.

Built as a **stdlib-only Python package** (runtime has zero dependencies) so the whole
pipeline runs locally, in CI, or on a $0 machine. Redis/Postgres drivers are optional extras.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install pytest          # dev only
.venv/bin/python -m pytest -q         # 55 tests

.venv/bin/python -m lisa demo          # full cycle + settlement on bundled fixtures
```

Live API usage (The Odds API key required):

```bash
export LISA_ODDS_API_KEY=your_key
python -m lisa run-cycle               # one ingestion+refinement pass
python -m lisa settle                  # grade pending ledger rows
```

Scheduler (adapted cadence: live → spike → prematch → idle) and live-validation metrics:

```bash
python -m lisa run                     # scheduler loop, runs until stopped
python -m lisa run --once              # single tick (drop into cron)
python -m lisa run --duration 168 --metrics data/metrics.jsonl   # one validation week
python -m lisa report --metrics data/metrics.jsonl               # weekly summary
```

Optional storage drivers:

```bash
# Redis hot+cold (needs: pip install redis)
LISA_STORAGE=redis LISA_REDIS_URL=redis://localhost:6379 python -m lisa run-cycle
# Postgres cold ledger (needs: pip install 'psycopg[binary]')
LISA_STORAGE=postgres LISA_DATABASE_URL=postgresql://localhost:5432/lisa python -m lisa run-cycle
```

## Layout

| Module | Responsibility |
|---|---|
| `lisa/shin.py` | Shin's method de-vig (2-way closed form, n-way iteration) + proportional fallback |
| `lisa/consensus.py` | Per-book de-vig → sharp/margin weighted consensus → stdev/CV agreement |
| `lisa/gate.py` | Quality gate (75% certainty, 5 books, CV ≤ 10%) + EV execution overlay |
| `lisa/pipeline.py` | Stage-1 orchestration: ingest → refine → gate → persist + notify (idempotent) |
| `lisa/settle.py` | 3h-after-kickoff settlement: WIN / LOSS / VOID |
| `lisa/storage.py` | `Storage` interface + in-memory / Redis / Postgres drivers |
| `lisa/client.py` | The Odds API transport (retry/backoff, credit tracking) + fixture client |
| `lisa/parsing.py` | Defensive Odds-API JSON → domain types |
| `lisa/cadence.py` | Pure schedule-state logic (live / spike / prematch / idle) |
| `lisa/scheduler.py` | Tick loop with adapted cadence + credit-budget guard |
| `lisa/tracker.py` | JSONL validation trail + weekly `report` summary |
| `lisa/fixtures.py` | Deterministic bundled payloads covering every gate outcome |
| `lisa/config.py` | `LISA_*` env configuration with sane defaults |
| `lisa/notify.py` | Log / Telegram notifier |

See [`docs/DESIGN.md`](../docs/DESIGN.md) for the full engineering analysis: the math,
edge cases, performance bottlenecks, mitigations and the decisions taken during design
(notably the 85%→75% gate finding and the EV overlay).