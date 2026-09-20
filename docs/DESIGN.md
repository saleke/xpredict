# LISA — Engine Design & Engineering Analysis

Status: **MVP implemented** (pure-Python engine, stdlib-only, runs with zero infrastructure)
Companion docs: [`architecture/entry.md`](../architecture/entry.md), [`architecture/plan.md`](../architecture/plan.md)

---

## 1. Problem statement (restated without the marketing)

LISA ingests multi-book betting odds for **NBA, La Liga and Bundesliga 1X2 (h2h) markets**,
strips each bookmaker's margin with **Shin's method** (which corrects the favourite–longshot
bias), blends per-book "true probabilities" into a **consensus** with an agreement metric, and
emits only **Top Picks** that clear a quality gate. Live state lives in a TTL'd **hot cache**;
picks are written as **write-once ledger rows** that are auto-settled against official results
(WIN / LOSS / VOID). Delivery (web dashboard, Telegram, Stripe tiers) is downstream of the
engine and out of scope for the MVP.

The pipeline is four stages:

```
Stage 1  ingest   – The Odds API, one request per sport per cycle (h2h, decimal)
Stage 2  math     – Shin de-vig per book → weighted consensus → stdev/CV agreement
Stage 3  gate     – certainty >= threshold, dispersion <= max_cv, EV overlay for execution
Stage 4  store    – Redis hot cache (TTL) + Postgres / in-memory write-once ledger,
                    settlement cron stamps WIN/LOSS/VOID 3h after kickoff
```

## 2. The three findings that shaped the design

### Finding A — the original 85% gate is mathematically nearly unreachable

Shin's method corrects *downward* the raw implied probability of favourites (that is the
favourite–longshot bias correction). Verified numbers from this implementation:

| Market | Raw implied fav | Shin true prob |
|---|---|---|
| NBA ML `1.16 / 5.20` | 86.2% | **83.5%** |
| Soccer 1X2 `1.30 / 5.00 / 9.00` | 76.9% | ≈ **75%** |

A post-de-vig `P_true ≥ 85%` essentially never occurs in these leagues: it needs a raw
favourite shorter than ~`1.13` in a 2-way market, which NBA produces a handful of times per
season, and 1X2 soccer never. The 85% gate as documented would emit ≈ 0 picks — killing the
subscription tiers, the premium alerts and the dashboard.

**Decision (product owner, confirmed):** gate at **≥ 75%** post-de-vig consensus — still highly
selective — and add an **EV overlay** (Finding B) so recommendations are value-driven.

### Finding B — probability ≠ edge

A de-vigged consensus probability is the market's *best estimate*, but betting at the fair
price is 0 EV and betting at a vigged price is negative EV. The engine therefore computes, for
every book, `EV = P_true × odds − 1` against the consensus, and only attaches an
**execution recommendation** to books where EV > 0. The pick itself is emitted on certainty
alone (`LISA_REQUIRE_POSITIVE_EV=false`); the product layer can hide picks without an
execution if it wants the value-guarantee story.

> Caveat (documented, leave-one-out is a future refinement): the EV of a book is computed
> against a consensus that *includes* that book's own line — mildly self-referential. The
> effect is small: with ≥ 5 books no single book dominates. Leave-one-out EV is a clean
> follow-up.

### Finding C — polling loses line movement between cycles

Pre-match, a 60 min cadence drops to 15 min in the 90 min before kickoff (docs' design), and
live windows poll each sport per cycle. `book.last_update` is checked against freshness
windows (4 h pre-match, 5 min live) so stale lines are down-weighted/dropped, never served
as fresh.

## 3. The math — Shin's method (verified)

Model: the bookmaker prices against insiders (bet only with edge) and noise bettors; the
insider share `z` is embedded in every line and skews the margin hardest onto longshots.

Algorithm (verified against the reference implementation `mberk/shin`):

```
inputs:  decimal odds o_i (i = 1..n, n >= 2)
io_i = 1 / o_i,  S = Σ io_i

n == 2:  closed form  z = ((S−1)(d²−S)) / (S(d²−1)),  d = io₁ − io₂
n >= 3:  fixed point  z ← (Σᵢ √(z² + 4(1−z)·ioᵢ²/S) − 2) / (n − 2)
                     until |Δz| < 1e-12  (max 1000 iters)

pᵢ = (√(z² + 4(1−z)·ioᵢ²/S) − z) / (2(1−z)),  Σp = 1
```

Guards: `z` clamped to `[0, 0.5]`; probabilities validated (`0 < p < 1`, finite, Σ = 1 within
1e-9); degenerate/unstable inputs fall back to **proportional de-vig** (`pᵢ = ioᵢ/S`) and the
fallback is recorded in `ShinResult.method` for observability — a recoverable degradation,
never an exception storm.

**Consensus:** per-book probs are blended with weights `w_book = sharp_multiplier(2.0) ×
1/margin` for sharp anchors (pinnacle, circa), else `1 × 1/margin`. Tight lines (sharp) and
low-margin books dominate. Agreement is the sample stdev / CV of per-book `P_true` for the
isolated top outcome; `cv > 0.10` ⇒ market is untrustworthy regardless of how high the blend
looks (plausibility is checked *before* certainty in the gate).

## 4. Quality gate (Stage 3)

Order of checks (deliberate):

1. `n_books >= min_books_alert` (5) — else `insufficient_books`
2. `cv <= max_cv` (0.10) — else `high_dispersion`  ← **checked before certainty**
3. `p_top >= gate_threshold` (0.75) — else `below_threshold`
4. EV overlay: best execution = argmax EV over books with `EV >= ev_min` (0.0);
   `require_positive_ev=true` suppresses the pick when no book clears fair price.

**EV uses a leave-one-out reference.** Each book's edge is measured against the
consensus recomputed over every book *except itself*
(`Consensus.loo_probability`): `EV_b = P_top(excl. b) × odds_b − 1`. Otherwise a
lagging soft book's own (biased) probabilities dilute the very reference it is
judged against, understating the edge. Gate certainty still uses the all-book
consensus; only the execution overlay uses the LOO reference.

Suppression reasons feed `CycleReport.suppressed` (`"match_id:reason"`) for observability.

## 5. State machine & ledger

```
                            ┌────────────  Pending  ────────────┐
                            │                                  │
RECALC → TRIGGER_ALERT ─────┼──> CONFIRMED → PENDING_SETTLEMENT ┼──> SETTLED (WIN|LOSS)
   (per-cycle, never        │                                  │        or VOID
    persisted alone)        └──────────────────────────────────┘
```

* Ledger row = **write-once**; `dedupe_key = match_id::market::outcome` is the primary key.
  `INSERT … ON CONFLICT DO NOTHING` (Postgres) / `SET NX` (Redis) / dict check (memory):
  re-running a cycle never duplicates.
* `settle_pick` only transitions rows whose state is pending; terminal rows are immutable.
* Settlement: 3 h after kickoff (configurable) pull the scores endpoint, stamp WIN/LOSS.
  `postponed|cancelled|suspended|abandoned` rows go **VOID** once they are past
  `settle_after + grace (24 h)`. No official score yet ⇒ row stays pending (skipped, counted).

## 6. Edge cases → mitigations

| # | Edge case | Mitigation |
|---|---|---|
| E1 | <5 usable books | Telemetry continues from ≥3 (`min_books_telemetry`); alerts need ≥5; never fabricate |
| E2 | Book missing an outcome / suspended | Book dropped from the pool; consensus over an identical outcome set only |
| E3 | Alert spam / probability flapping | Picks are emit-once (ledger); live re-alerts gated by `alert_min_delta` (1.5pp) + `alert_cooldown_sec` (600 s) |
| E4 | 3-way soccer draws | Shin's n-way iteration handles n=3 natively; draw is a first-class outcome in `Score.winner()` |
| E5 | 85% gate unreachable | Resolved → 75% + EV overlay (Finding A) |
| E6 | NBA OT / long games | Scores endpoint returns OT totals; settlement reads final `completed` state, not a hard clock |
| E7 | Postponed / void games | `VOID` state after grace window; never WIN/LOSS |
| E8 | DST / timezones | All times UTC everywhere; API `commence_time` is ISO-8601 UTC |
| E9 | Upstream 4xx/5xx/429 | Exponential backoff + retry (transport client); per-sport degradation so one sport never kills the cycle; errors surfaced in `CycleReport` |
| E10 | Stale lines | `book.last_update` vs `stale_prematch_sec`/`stale_live_sec`; stale books dropped |
| E11 | Duplicate settlement / races | Idempotent upserts; terminal rows immutable; state-gated UPDATEs |
| E12 | Odds outliers / junk | Sanity bounds `[1.01, 1001]`; validation errors exclude the book, not the match |
| E13 | Numerical instability | z clamped, Σp validated, proportional fallback flagged in `method` |
| E14 | Fixture-level malformed payload | Parsers skip bad records defensively; one bad game never kills a cycle |
| E15 | Missing `last_update` on a book | Treated as "don't know" → kept (conservative); revisit for live mode |

## 7. Performance bottlenecks → mitigations

| # | Bottleneck | Reality check & fix |
|---|---|---|
| P1 | Shin compute | ~200 solves/day, <100 iterations each: non-issue in pure Python; already O(n) per solve |
| P2 | API credit budget | Free tier = 500 credits/mo: budget **is** the constraint → keep to 1 req/sport/cycle, degrade cadence near kickoff, monitor `x-requests-remaining` (client tracks it). Production: $30/mo 20K tier |
| P3 | Redis memory / index growth | Trivial footprint; TTL does GC; live-index prunes dead keys on scan |
| P4 | Postgres writes | Tiny volume; PK dedupe; indexed `state`, `match_id` |
| P5 | Cron overlap / concurrent cycles | Cycles are idempotent; a scheduler wrapper should add an advisory lock (roadmap) |
| P6 | Crons firing across DST | UTC + explicit scheduler timezone (roadmap) |
| P7 | Fan-out to many subscribers | Alert happens once per pick in the engine; fan-out lives in the delivery layer with its own rate limits (roadmap) |
| P8 | Observability | `CycleReport`/`SettlementReport` give per-cycle counts, suppression reasons and errors; CLI prints JSON |

## 8. Configuration (env-driven, sane defaults)

| Variable | Default | Meaning |
|---|---|---|
| `THE_ODDS_API_KEY` (or `LISA_ODDS_API_KEY`) | — | The Odds API key (required for `run-cycle`/`settle` without `--fixtures`) |
| `THE_ODDS_API_BASE_URL` (or `LISA_API_BASE_URL`) | `https://api.the-odds-api.com` | API host override; `/v4/` path is appended by `client.py` |
| `LISA_REGIONS` / `LISA_MARKETS` | `eu,us` / `h2h` | API query shape |
| `LISA_SPORTS` | the 9 scope leagues | Comma-separated keys; only the 9 scope keys are permitted — unknown keys fail fast at startup |
| `LISA_GATE_THRESHOLD` | `0.75` | Certainty gate (Finding A) |
| `LISA_MIN_BOOKS_TELEMETRY` / `LISA_MIN_BOOKS_ALERT` | `3` / `5` | Coverage floors |
| `LISA_MAX_CV` | `0.10` | Max dispersion for the top outcome |
| `LISA_EV_MIN` / `LISA_REQUIRE_POSITIVE_EV` | `0.0` / `false` | Execution overlay |
| `LISA_SHARP_KEYS` / `LISA_SHARP_MULTIPLIER` | `pinnacle,circa` / `2.0` | Sharp anchor weighting |
| `LISA_MARGIN_WEIGHTED` | `true` | Inverse-margin weighting |
| `LISA_STALE_PREMATCH_SEC` / `LISA_STALE_LIVE_SEC` | `14400` / `300` | Freshness windows |
| `LISA_STORAGE` | `inmemory` | `inmemory` \| `redis` \| `postgres` |
| `LISA_REDIS_URL` / `LISA_DATABASE_URL` | — | Connection strings (lazy-imported deps) |
| `LISA_SETTLE_AFTER_HOURS` / `LISA_SETTLE_GRACE_HOURS` | `3.0` / `24.0` | Settlement schedule |
| `LISA_ALERT_MIN_DELTA` / `LISA_ALERT_COOLDOWN_SEC` | `0.015` / `600` | Live re-alert policy |
| `LISA_TELEGRAM_TOKEN` / `LISA_TELEGRAM_CHAT_ID` | — | Telegram notifier |

### Scope boundary (the 9 predictable leagues)

Outgoing calls are hard-limited to this whitelist (`config.SCOPE_LEAGUES`); any
other key is rejected by `load_settings()` and again by `pipeline.run_cycle()`:

`basketball_nba`, `basketball_euroleague`, `soccer_spain_la_liga`,
`soccer_germany_bundesliga`, `soccer_france_ligue_one`, `soccer_italy_serie_a`,
`soccer_netherlands_eredivisie`, `soccer_portugal_primeira_liga`, `soccer_epl`

### Spec-compliance notes (documented deviations)

- **Sync stdlib transport, not async httpx/aiohttp** — the worker polls on a
  6 h/15 min cadence; async saves seconds on a minutes-to-hours envelope and is
  a non-factor next to the credit budget. The system stays stdlib-only (zero
  runtime deps = small supply-chain surface for a money-adjacent worker). If a
  real-time delivery layer arrives, the client exposes a thin transport seam to
  swap in an async client without touching the pipeline.
- **Dataclass validation, not Pydantic v2** — the ingestion boundary has one
  internal consumer, and `parsing.py` already strictly filters to match
  metadata + bookmaker h2h decimal prices and drops malformed records. Pydantic
  earns its keep at a public API surface (the future delivery layer), not here.
- **Base-URL default corrected** — the spec proposed `https://the-odds-api.com`
  (the marketing site); the API host is `https://api.the-odds-api.com` with a
  `/v4/` path and an explicit `markets=h2h` param, per the upstream contract.
  The env override `THE_ODDS_API_BASE_URL` is honored as specified.

## 9. Repository layout

```
engine/            Python package `lisa` (stdlib core)
  lisa/odds.py     domain types (Match, Book, Score, Score.winner)
  lisa/parsing.py  Odds API JSON → domain types (defensive)
  lisa/client.py   transport (retry/backoff, credit tracking) + FixtureClient
  lisa/shin.py     Shin's method + proportional fallback
  lisa/consensus.py  per-book de-vig → weighted consensus → agreement
  lisa/gate.py     quality gate + EV overlay + Pick/Execution
  lisa/storage.py  Storage interface: in-memory / redis / postgres
  lisa/pipeline.py Stage 1+3+4 orchestration (run_cycle)
  lisa/settle.py   settlement cron (WIN/LOSS/VOID)
  lisa/cadence.py  pure schedule-state logic (live/spike/prematch/idle)
  lisa/scheduler.py tick loop + credit-budget guard (run_forever / tick)
  lisa/tracker.py  JSONL validation trail + weekly summary
  lisa/notify.py   LogNotifier / TelegramNotifier + alert text
  lisa/cli.py      `python -m lisa demo|run-cycle|settle|run|report`
  lisa/fixtures.py deterministic bundled Odds API payloads
  tests/           60 tests: math, consensus, gate, pipeline, settle,
                   storage, cadence, scheduler, tracker
docs/DESIGN.md     this document
architecture/      original product docs (unchanged)
```

## 10. Quickstart

```bash
cd engine
python3 -m venv .venv && .venv/bin/pip install pytest   # dev only; runtime is stdlib
.venv/bin/python -m pytest -q                            # 72 tests
.venv/bin/python -m lisa demo                            # full cycle + settlement on fixtures
export THE_ODDS_API_KEY=...   # LISA_ODDS_API_KEY also accepted
.venv/bin/python -m lisa run-cycle                       # live API, one pass
.venv/bin/python -m lisa settle                          # grade pending rows
.venv/bin/python -m lisa run                             # scheduler loop (adapted cadence)
.venv/bin/python -m lisa run --once                      # single tick (cron-friendly)
.venv/bin/python -m lisa report --metrics data/metrics.jsonl  # weekly validation summary
# Redis ledger: LISA_STORAGE=redis LISA_REDIS_URL=redis://...  (pip install redis)
# Postgres ledger: LISA_STORAGE=postgres LISA_DATABASE_URL=postgresql://... (pip install 'psycopg[binary]')
```

## 11. Roadmap

Status as of the scheduler/tracker iteration:

* ✅ **Live mode + scheduler** — `run_cycle(live=True)` with freshness handling, wrapped by
  `lisa/scheduler.py`: cadence adapts to the schedule seen in the last fetch (live →
  spike → prematch → idle), settlement grades on its own cadence, and the credit budget
  degrades polling below `credit_warn` and gates everything but settlement below
  `credit_stop`.
* 🔬 **Live-fire validation (active)** — run `lisa run --metrics data/metrics.jsonl` for a
  week against the real API, then `lisa report` for pick volume at the gate, the real EV
  distribution (+EV share), settlement accuracy, and credit burn. Answers the product's
  open questions with data instead of fixtures.
* ✅ **Leave-one-out EV** — every book's execution EV is measured against a reference
  consensus that excludes that book, so a lagging soft book can no longer dilute the
  consensus it is judged on.
* **Score settlement robustness**: retry bookkeeping, match rename handling, and multiple
  settlement attempts per row.
* **Delivery layer**: web dashboard (Next.js), Telegram channel fan-out with per-chat
  throttling, Stripe tiers — separated by design so the engine stays infra-light.
* **Advisory lock** for multi-worker safety; structured log sink; metrics endpoint.
* **Legal/compliance**: responsible-gambling notice, terms, jurisdiction review — product
  concern, tracked here for completeness.