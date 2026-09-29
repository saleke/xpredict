# CLAUDE.md

Guidance for coding agents working in this repository.

## Project: LISA / xpredict

Multi-tier sports prediction engine. The Python engine lives in `engine/`
(package `lisa`); the static dashboard is in `web/`. See `README.md` and
`architecture/` for the domain model.

- Run the engine: `cd engine && PYTHONPATH=. python -m lisa.cli start --port 8080`
- Tests: `cd engine && PYTHONPATH=. python -m pytest tests/ -q`
  (the suite exceeds 120s — pass a larger timeout)

## Settlement & CLV backfill (BetExplorer)

`engine/lisa/betexplorer.py` + `engine/lisa/backfill.py` re-walk **pending**
picks against archived BetExplorer day pages, which carry a final score *and* the
closing 1X2 line. This closes two gaps: `settle.py` can only grade fixtures
inside a live scores window (so nothing can be backfilled), and pipeline CLV
comes from the same snapshot it priced from (so it is not an independent close).

```bash
cd engine && PARSE_API_KEY=pmx_... PYTHONPATH=. python -m lisa.cli backfill --verbose
```

**Dry run unless `--write`.** Even a read-only pass spends Parse credits and
reaches a third party. Re-running is safe — a settled row leaves the pending set.

Config: `LISA_ENABLE_BETEXPLORER_BACKFILL` (default off), `PARSE_API_KEY`,
`LISA_BACKFILL_LOOKBACK_DAYS` (7), `LISA_BACKFILL_DRY_RUN` (true),
`LISA_BACKFILL_MIN_INTERVAL_SEC` (1.0).

Live verification (spends credits, not part of the suite):
`cd engine && PYTHONPATH=. python tests/live_backfill_check.py`

### Four rules worth knowing before changing this code

1. **1X2 only.** The feed publishes no totals, spreads or handicaps, so any
   market other than `h2h` is counted as unsupported and left pending. Do not
   "improve" this by grading a market that is not in the payload.
2. **Fixture join is the dangerous part.** A wrong join writes a wrong result
   into the ledger and silently corrupts every downstream metric. Ordered
   home/away only (a swapped pairing is a different fixture), and the *weaker*
   leg governs — both sides must independently reach `MIN_JOIN_SCORE`.
3. **Never drop identity words when normalising club names.** Only legal-form
   noise (`FC`, `CF`, `SC`) may go. Dropping `united` collapses "Manchester
   United" to "manchester", which then joins "Manchester City" and settles a pick
   against the wrong team. `_SIDE_MARKERS` refuses senior-vs-women's/reserve
   joins outright. See the `test_distinct_clubs_never_match` cases — every one is
   a real collision.
4. **Abbreviations deliberately do not match.** "Man United", "Wolves",
   "Nott'm Forest" score 0.0 and surface as `skipped_no_match` so the real match
   rate stays visible. Do not add fuzzy matching to "fix" this; add an alias
   table instead.

CLV reuses the engine's own convention from `pipeline._update_closing`:
`taken / closing - 1`, positive when the price beat the close.

## How this project calls Parse

Parse (`https://parse.bot`, docs at https://docs.parse.bot) exposes third-party
bookmaker sites as callable JSON APIs. The integration is a **self-contained uv
project** at `engine/integrations/parse/` so it cannot disturb the engine's
setuptools build or the root `.venv` that runs the app.

### Layout

```
engine/integrations/parse/
  pyproject.toml        # uv project; engine/ is its workspace root
  parse_apis/           # generated typed clients (scaffold committed, payload ignored)
  .env                  # PARSE_API_KEY (gitignored) — never commit a key
```

### Auth

The key is read from `PARSE_API_KEY`; the CLI also stores it at
`~/.config/parse/credentials`. Keys start with `pmx_`. Never hard-code a key or
commit one. Verify with `uv run parse whoami --json`.

### Invocation

Always via `uv run parse …` from `engine/integrations/parse/`. The
`VIRTUAL_ENV does not match` warning is expected and harmless — the shell's
root `.venv` is active while uv correctly uses the workspace env at
`engine/.venv`.

### Current status

**Working: `betexplorer-com-api`** (canonical `5daf70d1-eaad-46ba-afbb-c57c0d5ebc3e`).
`access_requirements: []` — no phone or card needed. Verified live:

```
GET /scraper/5daf70d1-eaad-46ba-afbb-c57c0d5ebc3e/search_matches?date=YYYY-MM-DD
→ 200 {"status":"success","data":{"date":…,"matches":[…]}}
```

Each match: `event_id`, `country`, `league`, `home_team`, `away_team`, `time`,
`status` (`FIN` when complete), `score` (`"2:1"`), `odds_home`/`odds_draw`/
`odds_away`.

Measured behaviour (2026-09-29, per-day counts):

| Date | Matches | Leagues | Countries | with odds | with score | with status |
|---|---|---|---|---|---|---|
| today | 144 | 35 | 28 | 3 | 3 | 3 |
| yesterday | 98 | 35 | 27 | 98 | 98 | 98 |
| −3 days | 117 | 35 | 18 | 117 | 117 | 117 |

**Read this table before designing against it.** Odds attach when a match
*completes*, not when it is scheduled — today only 3 of 144 upcoming fixtures
had odds. These are **closing/averaged odds**, so this source is a settlement,
backfill and CLV-benchmark feed. It is *not* a pre-match odds feed, and at ~35
leagues/day it does not by itself satisfy the 140+ league target. Anything
requiring a price *before* kickoff must still come from The Odds API.

**Blocked: `sportybet-com-ng-api`** — `403 verification_required`,
`requirements: ["card"]`. Phone cleared; card outstanding. Unblock at
<https://parse.bot/settings?tab=account#verification>. Until then it cannot be
subscribed and no endpoint is callable.

Note that `betexplorer-com-api` covers, and SportyBet does not, the **historical
results** the engine needs for `accuracy_tracker.py` — SportyBet's own spec
confirms it can only grade codes it created via `book_bet`.

### The intended use: a data source, not a second opinion

Decision on record: SportyBet is wired in for **new product surfaces**, *not* as
an additional voice in the consensus. The engine's edge is multi-book
de-vigging — `Match.bookmakers: tuple[Book, ...]` in `engine/lisa/odds.py` is
what `consensus.refine()` and its leave-one-out weighting consume. A single
book adds no independent signal, so treating SportyBet as a consensus input
would be a downgrade over The Odds API. The value is in markets The Odds API
carries poorly or not at all.

The same reasoning applies to `betexplorer-com-api`, and more sharply: it is a
single book's closing line, so it is a **settlement/CLV reference**, not a
consensus input.

Relevant endpoint facts, from the marketplace spec (last verified 2026-09-28):

| Endpoint | Method | Credits | Notes |
|---|---|---|---|
| `get_prematch_football_events` | GET | 1 | 1X2 odds; filter by `tournament`, `team`, `senior_men_only` |
| `get_prematch_football_markets` | GET | 1 | O/U, AH, GG/NG, Correct Score |
| `get_football_event_markets` | GET | 3 | every market for one `sr:match:<id>` |
| `get_prematch_basketball_events` | GET | 1 | Winner/Handicap/1st-half/1st-quarter |
| `get_prematch_ice_hockey_events` | GET | 1 | Puck Line, totals, correct score |
| `get_prematch_football_all_markets` | GET | 10 | corners/bookings/shots/players; ~2 MB per fixture, page_size capped at 3 |
| `get_booking` | GET | 1 | settlement: WON/LOST/VOID per selection |
| `book_bet` | POST | 2 | **reserves a real bet** and returns a share code |

Three limits to respect when designing against it:

1. **Pre-match only.** Every listing endpoint returns un-started events. There
   is no in-play markets endpoint; adding one requires forking the API on Parse
   and revising it. So this cannot feed the in-play / `LiveSettler` path.
2. **No historical results.** There is no results/settlement lookup endpoint.
   `get_booking` only grades codes that `book_bet` created, so it cannot
   backfill `accuracy_tracker.py` over past fixtures. The engine's own Odds API
   settlement polling remains the source of truth for that.
3. **Pinned snapshot.** `parse add --marketplace` pins a version; upstream
   changes do not alter the contract until updates are explicitly merged. Treat
   a field rename as a breaking change requiring a code change here.

Cost is metered in Parse credits, separate from the engine's
`credit_budget_daily` (currently 50 Odds API requests/day/key). Reconcile the
two before pointing the pipeline at Parse.
