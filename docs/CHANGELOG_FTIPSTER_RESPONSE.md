# Change Log — Response to the Ftipster Teardown

> **Date:** 2026-09-29
> **Input:** `docs/COMPETITIVE_TEARDOWN_FTIPSTER.md`
> **Scope:** Every item in that document's "Verdict" register that was actionable in code.
> **Test status:** 7 pre-existing failures remain, unchanged and unrelated (verified by
> `git stash` against the original tree). No test was weakened to make a change pass.

---

## 1. What the audit actually found, and the one result that changed the strategy

The teardown's central claim was that the engine was **discarding its only edge**. That was
correct, and the fix produced a measurable but *not significant* improvement. Stating that
plainly is the most important thing in this document.

### The gate change

`require_positive_ev` defaulted to `False`. With it off, the gate emitted a pick on
`p_top >= 0.75` alone, then discovered there was no edge: `best_execution` was `None`,
`exec_odds` fell back to `1/p_top`, EV computed to *exactly* 0.0, and Kelly returned
`"Pass: Non-positive expected value"` with a zero stake. **The headline product was a pick with
no price and no stake.** It now defaults to `True`.

Measured on the real 7,155-match archive, flat 1 unit per bet:

| | `require_positive_ev=False` | `require_positive_ev=True` |
|---|---|---|
| Executed bets | 380 | **30** |
| Win rate | 79.74% (95% CI 75.41–83.47) | 86.67% (95% CI 70.32–94.69) |
| Avg price | 1.237 | 1.220 |
| Breakeven win rate | 81.06% | 82.19% |
| **Flat ROI** | **−1.74%** | **+5.20%** |
| Mean per bet | −0.0174u (t = −0.68) | +0.0520u (t = 0.67) |
| Max drawdown (2% flat) | −19.3% | −3.3% |

**Read this honestly.** The old configuration won 79.7% of its bets and *lost money*, because
its average price (1.237) required an 81.1% hit rate. The new configuration is directionally
better on every axis — but **n = 30 and t = 0.67**. The Wilson lower bound (70.3%) sits *below*
the 82.2% breakeven this price band requires. **The archive does not prove the edge exists.**
It proves only that the old configuration was demonstrably not producing one.

This is now asserted in the tests, so a future change cannot quietly promote 30 bets into a
profitability claim:

- `tests/test_backtest.py::test_backtest_is_honest_about_money` — asserts
  `wilson_ci_lower < 0.82`, and documents that the win rate is not a finding.
- `tests/test_backtest.py::test_backtest_runs_on_real_archive` — pins the CI and asserts the
  lower bound straddles breakeven.

Reproduce with `engine/edge_gate_study.py`.

### Benchmark comparison, restated

| | Ftipster (measured) | LISA, old gate | LISA, new gate |
|---|---|---|---|
| Sample | 841 bets, 27 months | 380 bets, 4 seasons | 30 bets, 4 seasons |
| Win rate | 75.9% | 79.7% | 86.7% |
| Flat ROI | **+13.1%** | −1.7% | **+5.2%** |
| Significance | t = 7.50 | t = −0.68 | t = 0.67 |
| Max drawdown | −5.96u | −19.3% | −3.3% |

The gap is now smaller and points the right way, but Ftipster's is still the only one backed by
a statistically significant result. The remaining difference is **league selection**, not
engineering — see §4.

---

## 2. Removed — customer-facing fabrications

| Item | Location | Action |
|---|---|---|
| Simulated checkout | `web/index.html:2126`, `web/js/app.js` | Deleted the card form (labelled *"Card Number (Simulated Stripe Gateway)"*, pre-filled `4242…4242`) and the handler that slept 600 ms, called `switchUserTier()`, and toasted *"Privilege elevated to Tier 2 Pro Trader!"*. Replaced with a purchase-intent handoff to the Telegram desk that takes no card details. |
| Fake daily board | `web/js/app.js:2542` | Deleted five invented fixtures at invented odds (Arsenal/Chelsea @1.85, Real/Barca @2.10, …) rendered on the highest-traffic nav item, with the real `/api/daily-board` call commented out one line above. Now calls the real endpoint; on failure it says so rather than showing unverified rows. |
| Fake leaderboard | `web/index.html:414` | Deleted five hardcoded users (PlayMaker 4,532 picks / 92%, DropShot 4,215 / 89%, …) painted on first render before JS replaced them. Now ships a single empty-state row. |
| `"MODEL ACCURACY 92.7%"` | `web/index.html:462` | → `n/a`. |
| `"1.2M+ PREDICTIONS MADE"` | `web/index.html:475` | → `n/a`. |
| Hardcoded `"84.0%"` | `engine/lisa/telegram_bot.py:3024, 3045` | Removed from the health reply and the greeting. Both now derive from `_load_dashboard_summary()` and explicitly refuse to state a rate below `MIN_SETTLED_FOR_STATS` — the same contract `/stats` already enforced. This was a direct self-contradiction: the bot told a prospect 84% on "hi" while `/stats` refused to publish anything. |
| Invented session token | `web/js/app.js:3684` | Rendered `lsp_live_…` (or the literal `8849201948ae`) as if it were a working API credential. Now shows the real token or nothing. |
| `web/data/verified_users.json` | deleted | 10 fake emails plus a real-looking Telegram ID, committed inside a gitignored path. |
| `lisa/fixtures_generator.py` | deleted | A module whose stated purpose was to emit 12 fabricated picks with invented `p_true` values, a fake "142 Institutional Seats" claim, and a third conflicting price ladder. It was unreachable — which is exactly why it was a liability. |

---

## 3. Fixed — security and money

**The payment exploit.** `_handle_payment_confirmation` minted a real, redeemable activation key
the instant a user tapped *"I've Sent Payment"* — no chain check, no amount check, no
confirmation — and printed *"🎉 PAYMENT CONFIRMED!"* first. Anyone could self-issue a free
Tier 3. Now:

- `payments.record_payment_claim()` records a self-report and returns a reference. It issues
  **no key**.
- `payments.verify_claim()` is the only path that mints a key from a claim, and it is
  operator-initiated and idempotent (re-verifying returns the same key).
- `list_unverified_claims()` exposes the queue for reconciliation.
- The bot says *"PAYMENT REPORTED — PENDING VERIFICATION"*, and states plainly that activation
  is not automatic.

**The paywall.** `localStorage.setItem('lisa_tier','tier3')` unlocked the entire product, because
`/api/dashboard` — the route the frontend actually calls — returned every pick unmasked and the
tier was read from client storage. `web/DESIGN.md:174` and `web/FRONTEND_DEV_GUIDE.md:190` both
claimed the server enforced this.

- Extracted the entitlement ladder into `_mask_picks_for_tier()` and the tier resolution into
  `_resolve_viewer_tier()`, and applied both to **`/api/dashboard`**, which previously had no
  masking at all.
- Locked rows have `outcome_name`, `best_odds`, `fair_odds` and `best_ev` set to `None` —
  stripped, not merely flagged.
- `/api/picks` now routes through the same helpers, so the two cannot drift.
- Every pick now carries `rank`. The frontend keyed its lock state off `p.rank`, which no
  payload ever set, so **every pick fell through to the final branch and a default visitor saw
  100% of picks locked** — including the "Match #1 completely free daily" promised on the
  landing page.
- `state.currentTier` is now sourced from `viewer_tier` in the response and never from
  localStorage. `switchUserTier` is display-only and no longer grants anything.
- Tests: `test_dashboard_masks_picks_for_anonymous_callers`,
  `test_dashboard_ranks_are_always_present`.

**Latent crash.** `TelegramBot` read `self.auth` at line ~2592 but never assigned it. It is
currently unreachable because `/picks` returns earlier — which is what made it dangerous: a
one-line refactor turns it into an `AttributeError`. Now accepts an `auth_store` constructor
argument.

---

## 4. Fixed — ledger integrity

| Change | Why |
|---|---|
| Added `actual_score` + `score_source` to the `picks` table, with `ALTER TABLE ... IF NOT EXISTS` migration. Populated by `settle.py` (from the observed score) and by the admin/Telegram settlement paths. | The ledger's "Actual Score" column previously always rendered `-`. A customer could not verify a single settlement against an independent scoreboard. |
| `clv` now initialises to `NULL`, not `0.0` | A pick never re-priced before kickoff scored **exactly zero CLV**, which reads as "matched the close perfectly" in every mean that folds it in. It silently dragged mean CLV toward zero. |
| `closing_odds` / `closing_p_true` now initialise to `NULL`, not copies of the emit price | The "close" was the price the engine already had. |
| `admin_pick_stats` now publishes `clv_sample_size` beside `avg_clv` | So a reader can see what fraction of the graded book the CLV figure actually covers. |
| `manual_settle_match` guarded with `state IN (PENDING_STATES)` | The operator path could rewrite an already-`SETTLED` row, making "write-once immutable" true of the automated path and false of the operator path. Now returns 0 rows and the API distinguishes "no match" from "already settled". |
| `/api/ledger` gained `offset` paging with `has_more` / `next_offset` | `limit` was capped at 1000 with no offset, so the record past pick 1,000 was **unreachable**. The ledger is the main trust asset. |

---

## 5. Fixed — correctness

**Asian grading was wrong.** `markets.grade_asian_handicap` reads its line as the *home* team's
point. `odds.Score.grade_pick` was called with the *outcome's* line, so routing it through
`markets` naively (`side="away"` with the same line) graded Knicks at +8.0 as a LOSS when it is
a PUSH. Resolved by mirroring (`-line`) and inverting the result, with `_invert_handicap()`
handling the quarter-ball swap. Both markets now route through one implementation.

**`grade_total` could not handle quarter lines.** "Over 2.5,3.0" is half the stake on each of
2.5 and 3.0 — 3 goals is a HALF_WIN, 2 a HALF_LOSS. The old implementation graded it as a plain
WIN or LOSS, overstating both the win count and the money. This combination is **22% of the
competitor's published picks**, so it is not a corner case.

**`walkforward.py` reported meaningless calibration.** It stored `p_true` = the probability of
the outcome that *actually happened*, alongside a hardcoded `"result": "WIN"`. Brier became
`mean((1 − p_actual)²)` and ECE was measured against a 100% observed win rate — printed under
the heading "INDEPENDENT MODEL CALIBRATION". Now stores the real result, so it grades
confident-correct against confident-incorrect.

**`study.py` had cross-league look-ahead.** `HISTORICAL_ODDS` is season-major / league-minor, so
iterating it in construction order let a club be rated on results up to nine months later in its
own calendar — then evaluated as if out-of-sample. Now sorted by `commence_time`, with undated
fixtures sinking to the end.

**`tuning.py` tuned a strategy the product does not run.** It hardcoded
`require_positive_ev=False` while the engine shipped `True`, so every cell in the grid described
a certainty-only book production would never emit. Now parameterised and defaulting to match
production. Verified: tuning and backtest both produce 30 bets.

**`derived.py` hid its own failures.** `PoissonFit.converged` was hardcoded `True`; both solvers
returned the last midpoint tried on exhaustion. Now return `(value, converged)` and the flag
propagates. `_home_away_diff` also reports captured grid mass.

**Deduplicated the Poisson maths.** `bulletin.py` carried private copies of
`_poisson_tail` / `_btts_probability`. The canonical versions now live in `derived.py`.

**`odds.py` substring bug.** `"1x" in name_lower` / `"12" in name_lower` matched any label
containing those sequences.

**Gate sized on the wrong probability.** EV was measured against the leave-one-out `p_ref`, then
Kelly was handed the self-inclusive `p_top` — two different probabilities for one bet, one of
which had already been ruled out. Now sizes on `best_p_ref`.

**`sortino = sharpe * 1.5`** (invented, no statistical basis) and **`profit_factor = 99.0`** on
zero losses (a sentinel that reads as a strong ratio) are gone. Both metrics are now `None` when
undefined.

**`net_counterfactual` hardcoded `80.0`** — described as "net profit at avg ~1.80 odds", never
derived. Now computed from the archived prices of the avoided winners. The honest result is
**+$5,448 net against $310,500 gross "capital preserved"**, i.e. most avoided traps would have
*won*. The gross figure is now visibly smaller than the net, which is the point.

**Conviction score labelled `/10.0`.** The formula
`((p_true − 0.75) / cv) × (1 + ev)` is unbounded and reaches ~27 on real picks. Label corrected
and the formula shown.

---

## 6. Kept — verified as the real advantage

| Asset | Verdict |
|---|---|
| `shin.py` | Correct Shin (1993): closed form for n=2, fixed point for n≥3, tol 1e-12, and ten distinct failure modes that degrade to proportional de-vig rather than crash. Production-grade, and the competitor cannot do it at all. |
| `consensus.loo_probability` | The correct fix for the self-inclusion confound. Textbooks get this wrong. |
| Gate ordering | "Plausibility before certainty" — books-must-agree checked before probability. Right instinct. |
| `calibration.py` | Brier, log-loss, ECE, MCE, Murphy decomposition. Uniquely valuable. Publish it. |
| `backtest.py` baselines | `market_favorite` and `always_home` computed honestly, not hardcoded. |
| `admin_api.py` | 33 routes, CSRF, origin checks, rate limits, owner/role separation, audit log. |
| `auth.py` | PBKDF2-HMAC-SHA256, 600k iterations, per-user salt, `compare_digest`. Correct. |
| `betexplorer.py` normalisation | Never dropping `united` (Man Utd → Man City), and the deliberate *refusal* to fuzzy-match abbreviations so the miss rate stays visible. Exemplary. |
| `dashboard.py` honesty contract, `storage.py:911` comment, `MIN_SETTLED_FOR_STATS`, the refusal to read the contradicting `dashboard.json` | **This is the brand.** The competitor cannot say any of it, because its archive is a hand-typed HTML table with no confidence data. |

---

## 7. Not done — and why

**`markets="h2h"` → `markets="h2h,spreads,totals"` remains off.** This is the single
highest-leverage change available: the competitor's entire edge is in Over/Under 2.5, BTTS and
handicap, and `h2h` excludes exactly those markets.

It is not free, and the numbers are now documented at `lisa/config.py:enable_extra_markets`:
The Odds API bills per (sport, region, market-group) — 1 credit for h2h, 3 for
h2h+spreads+totals. At `credit_budget_daily=50` across 22 sports that is ~2 cycles/day on h2h, or
**well under 1 cycle with all three markets**. Enabling it without raising the budget does not
add markets — it silently cuts league coverage to a third.

To do it properly: raise `credit_budget_daily` and the key count, or narrow `SCOPE_LEAGUES` and
poll fewer competitions more often. A narrower, well-covered set beats a broad,
one-cycle-per-day set. **This is a budget decision, not a code change, and it should be made
deliberately with the ingestion cadence measured.**

Still outstanding, and listed so it is not lost: `tiers.py` still advertises `steam_radar`,
`portfolio`, `arbitrage_stream`, `early_bird` and `booking_codes`, none of which are implemented
(`/codes` correctly refuses: *"LISA does not generate bookmaker booking codes"*). A sold feature
that does not exist is what survives a chargeback conversation. Either implement or delist.

---

## 8. Test status

```
29 slow-suite tests (backtest / walkforward / study / tuning) ....... all pass
Rest of the suite ................................................. 7 failures, all pre-existing
```

The 7 were verified pre-existing by stashing all changes and re-running against the original
tree, where they fail identically:

| Test | Cause | Pre-existing |
|---|---|---|
| `test_key_pool::test_operators_real_env_yields_both_keys` | Only one Odds API key in `.env`; an environment fact, not a defect | ✓ |
| `test_security_regressions::test_payment_webhook_rejects_wrong_and_unsigned` | `LISA_WEBHOOK_SECRET` is empty, so the webhook returns 503 before it can reject a bad signature | ✓ |
| `test_telegram_bot` × 5 | Bot copy and keyboard labels drifted from the tests | ✓ |

The webhook one is worth noting: it fails *because* the payment webhook is disabled. Fixing it
means setting `LISA_WEBHOOK_SECRET` and wiring a real provider — which is the difference between
a business and a demo, and is the next thing to do.

**No test was weakened to make a change pass.** Where behaviour genuinely changed (the gate
default, the ledger's `clv` initial value, the removed trap-page fixture rows), the tests were
updated to assert the *new* contract, with the reason recorded in the docstring — and the gate
change additionally gained new tests that assert the edge requirement holds and that the result
is not presented as profitable.
