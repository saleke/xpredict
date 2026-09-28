# LISA — Completion Plan (48h Match Prediction Engine)

**Goal:** predict matches available within a **48-hour window**, with the **best win
opportunity** and **best earning opportunity**, at production accuracy and precision,
using a **severely limited** The Odds API budget, and populate **10+ matches/day with
varied micro-bets**.

---

## Current-state audit (findings, evidence-based)

| # | Finding | Evidence | Impact |
|---|---|---|---|
| 1 | **No credit planner.** `Pipeline.run_cycle` polls *every* configured league every cycle. `regions=eu,us` doubles cost. | `pipeline.py:82` iterates `settings.sports`; `config.py:92` `regions="eu,us"` | Budget 50/key/day is arithmetically impossible → self-throttle → starved board |
| 2 | **90-min payload TTL vs 6h poll cadence.** | `pipeline.py:29` `PAYLOAD_TTL_SEC=90*60`; `.env` `LISA_CADENCE_PREMATCH_SEC=21600` | `/api/forecast` returns `NO_LIVE_DATA` ~75% of the day |
| 3 | **No 48h cap on picks.** Ledger holds fixtures 3 weeks out. | live `data/lisa.db` picks `commence_time` up to `2026-10-17` | Wrong product; ungradable for weeks; no CLV |
| 4 | **Micro markets structurally unpriceable.** `markets=h2h` only, `enable_extra_markets=false` → `micro_markets` stripped. | `config.py:93`, `config.py:169`, `bulletin.py:555-559` | "Micro bets of different kinds" is 0% implemented |
| 5 | **Soccer Poisson applied to every sport.** NBA/MLB rows get `expected_goals≈1.5`, `p_over_2_5`, BTTS. | `bulletin.py:347-354` → `model.predict_score_matrix` unguarded | User-visible nonsense; credibility killer |
| 6 | **Elo model trained on 5 soccer leagues only** → `ready:false` for all other leagues, prior-only probs still displayed. | `bulletin.py:177-190` trains on `build_matches()` | False precision |
| 7 | **Settlement queries `/scores` for every sport with *any* pending pick**, incl. 3-weeks-out rows. | `settle.py:57-69` | Wastes credits, never grades |
| 8 | **CLV defaults to `0.0` not `None`.** | `storage.py:70-72` | Fake zeros dilute mean CLV and `positive_clv_share` |
| 9 | **No SQLite migration path** — `CREATE TABLE IF NOT EXISTS` only. | `storage.py:630-633`; Postgres has `ALTER`s, SQLite does not | Upgrading against an existing DB silently fails *all* pick writes |
| 10 | **Redis/Postgres drivers are stubs**; Postgres `list_settled_picks` matches everything. | `storage.py:1566`, `1356-1439`, `1477-1518` | 2 of 4 documented drivers broken |
| 11 | **Two divergent runtimes** — `Scheduler` (used by `start`) and `LiveIngestionDaemon` (used by `live-ingest`). | `cli.py:760-783` vs `cli.py:1120-1136` | Trap scan/settlement drift; two owners |
| 12 | **4 failing test groups + suite >15 min.** | see audit | No trustworthy safety net |
| 13 | Runtime state duplicated in `web/data/` **and** repo `data/`. | `web/data/verified_users.json`, `data/verified_users.json` | Tracked artifact, test failure |
| 14 | Bot `/traps` reads a *metrics summary*, not the recorded trap history ingestion writes. | `telegram_bot.py:2313-2343` vs `live_ingest.py:237-252` | Real integration bug |

### Failure triage (4 groups — 3 obsolete expectations, 1 real bug)
- `test_payment_webhook_rejects_wrong_and_unsigned` — fixture uses an 11-char secret; server now correctly requires high entropy. **Obsolete test.**
- `test_auth_api_flow` — asserts `telegram_verified is False` after a proven handshake; uncommitted `server.py` now correctly sets `True`. **Obsolete test.**
- `test_web_data_directory_is_not_tracked` — runtime artifact committed in `web/data/`. **Real hygiene bug.**
- `test_command_traps_*` / `test_stats_page_*` — bot no longer reads recorded history / old empty-state strings. **Obsolete tests, except `/traps` which is a real source bug (finding 14).**

---

## Feasibility of the 10+ matches/day + micro-bets requirement

**RESOLVED — measured, not assumed. No blocker.**

### Measured cost model (authoritative, verified against vendor docs + live probes)

| Endpoint | Credit cost |
|---|---|
| `GET /v4/sports` | **0 (free)** |
| `GET /v4/sports/{sport}/events` | **0 (free)** — supports `commenceTimeFrom/To` |
| `GET /v4/sports/{sport}/odds` | `markets × regions` |
| `GET /v4/sports/upcoming/odds` | `markets × regions` |
| `GET /v4/sports/{sport}/scores` | 1 (2 if `daysFrom` set) |
| historical odds | `10 × markets × regions` |

**Correction to an earlier assumption in this repo's `.env` and in my own first
reading:** cost is **per region *per market***, not per region.
`h2h,spreads,totals` × `us` = **3 credits**. The existing `.env` comment was correct.

### The economic unlock

`/sports` and `/sports/{sport}/events` are **free and windowed by `commenceTimeTo`**.
That is a **complete, zero-cost 48-hour fixture calendar for every league**, refreshable
at any cadence. Only `/odds` is metered — and it only needs to be called for leagues
that actually have a fixture in the window.

**Measured live (0 credits spent):** 18 in-scope leagues → **39 fixtures in the next
48h across 8 active leagues** (12 within 12h). So the daily 10+ match floor is
**satisfied by a wide margin on day one**, and the marginal cost of a *priced* board
is one credit per active league.

### Quota reality (measured, free probe via invalid-market 422)

| Key | used | remaining |
|---|---|---|
| #1 | 496 | **4** |
| #2 | 485 | **15** |
| **total** | 981 | **19** |

Two 500-credit allowances are ~98% consumed (monthly reset). The board is dark
because the budget is exhausted, not because the feed is empty. The planner must
therefore (a) spend nothing on discovery, (b) cap itself against a hard quota floor,
and (c) degrade to a free-only mode that still renders a fixture calendar.

---

## Phases

### Phase 0 — Safety net & foundation
- [ ] 0.1 Idempotent additive SQLite migration system (`PRAGMA user_version` + `ALTER TABLE ADD COLUMN` parity with Postgres).
- [ ] 0.2 Fix `picks` CLV seed: `None` instead of `0.0`.
- [ ] 0.3 Fix bot `/traps` to read recorded history (`live:traps`); keep the honest empty state.
- [ ] 0.4 Consolidate runtime state to a single dir; remove `web/data/verified_users.json`.
- [ ] 0.5 Update the 4 obsolete test groups; keep them as canaries.
- [ ] 0.6 Test perf: session-scoped archive/Elo cache; target full suite < 3 min.

### Phase 1 — Economic core: the 48h fixture planner
- [ ] 1.0 **Probe the live API once**; confirm per-region pricing + `/sports/upcoming` discovery.
- [ ] 1.1 `engine/lisa/planner.py` — fixture index, 48h window, per-league region routing, T-minus-tiered cadence, credit budget allocation.
- [ ] 1.2 Discovery via `/v4/sports/upcoming` (1 credit) → which leagues have fixtures in 48h.
- [ ] 1.3 Wire `Scheduler` to the planner; pipeline polls only planned leagues.
- [ ] 1.4 Fix settlement: only query scores for picks past their settle window; per-sport settle delays.
- [ ] 1.5 Extend payload TTL to cover the 48h board.

### Phase 2 — Markets & micro-bets
- [ ] 2.1 `markets` → `h2h,spreads,totals,btts`; per-league regions.
- [ ] 2.2 Sport-aware scoring model: soccer Poisson; basketball; baseball; generic fallback. No soccer metrics on non-soccer rows.
- [ ] 2.3 Micro-bet engine: priced (real book) vs derived (model-only, honestly flagged) vs unpriced.
- [ ] 2.4 Elo model: seed real ratings per league from the API feed as results settle; otherwise mark `ready:false` and withhold probs.
- [ ] 2.5 Keep the market-consensus layer (never model-only) as the *priced* source of truth.

### Phase 3 — 48h window & pick correctness
- [ ] 3.1 Hard 48h horizon cap in the gate.
- [ ] 3.2 Correct `spreads` line sign / `totals` grading edge cases (pushes, quarter lines).
- [ ] 3.3 Real closing-line capture at T-0 for honest CLV.

### Phase 4 — Product: dual ranking + volume guarantee
- [ ] 4.1 **Best win opportunity** ranking: certainty × agreement × EV.
- [ ] 4.2 **Best earning opportunity** ranking: EV × stake × confidence.
- [ ] 4.3 10+ matches/day guarantee with honest shortfall reporting + coverage stats.
- [ ] 4.4 Server endpoints for micro-bets + dual rankings; web UI rendering.

### Phase 5 — Consolidation & audit
- [ ] 5.1 One runtime path (remove the duplicate daemon path).
- [ ] 5.2 Complete or remove Redis/Postgres stubs; fix Postgres `list_settled_picks`.
- [ ] 5.3 Full test suite green and fast.
- [ ] 5.4 Final audit: edge cases, credit spend report, honest accuracy reporting.

---

## Non-negotiables
1. **No invented data.** Anything not backed by the real feed or a real model is `null` + flagged.
2. **No look-ahead.** Model state updated strictly after the fixture it predicts.
3. **Bounded spend.** Hard per-key daily cap; the planner degrades gracefully.
4. **Honest reporting.** Shortfalls, sample sizes and negative ROI reported as-is.
5. **48h window is a hard cap**, never a padding target.
