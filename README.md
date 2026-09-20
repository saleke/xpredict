# xpredict — LISA

Automated sports-prediction engine ("LISA"): a data refinery that turns multi-book betting
odds into mathematically disciplined, high-certainty picks.

Concept: strip each bookmaker's margin with **Shin's method** (corrects favourite–longshot
bias), blend per-book true probabilities into a **sharp/margin-weighted consensus**, then
emit only **Top Picks** cleared by a quality gate (certainty ≥ 75% + book agreement + EV
overlay), auto-settled against official results in a write-once ledger.

## Repository layout

```
architecture/   original product docs (4-stage pipeline map, system overview)
docs/           engineering design & analysis (DESIGN.md)
engine/         implementation — Python, stdlib-only runtime (see engine/README.md)
```

## Quickstart

```bash
cd engine
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q        # 60 tests
.venv/bin/python -m lisa demo        # full cycle + settlement on bundled fixtures
```

Live mode needs a [The Odds API](https://the-odds-api.com) key:

```bash
export THE_ODDS_API_KEY=your_key   # LISA_ODDS_API_KEY also accepted
python -m lisa run-cycle              # ingest → refine → gate → persist
python -m lisa settle                 # grade pending picks (WIN / LOSS / VOID)
python -m lisa run --once             # scheduler tick (cadence: live/spike/prematch/idle)
python -m lisa report                 # weekly live-validation metrics summary
```

## Design highlights

* **Gate is 75%, not 85%** — verified: Shin's correction pushes post-de-vig favourites below
  85% in real markets, so the original 85% gate would emit ~zero picks (see
  [`docs/DESIGN.md`](docs/DESIGN.md), Finding A).
* **EV overlay** — probability is not edge: each book's EV is measured against a
  *leave-one-out* consensus (every book except itself), and the engine only recommends a
  book when its price clears that fair price (`EV = P_true(excl. b) × odds − 1 > 0`).
* **Write-once ledger** — `dedupe_key = match_id::market::outcome`, state machine
  `TRIGGER_ALERT → … → SETTLED (WIN|LOSS) | VOID`; re-running cycles never duplicates.
* **Zero-infra by default** — in-memory storage + fixture client; Redis/Postgres are opt-in
  drivers behind `LISA_STORAGE`.

Full engineering analysis — math, edge cases, performance bottlenecks, mitigations — in
[`docs/DESIGN.md`](docs/DESIGN.md).