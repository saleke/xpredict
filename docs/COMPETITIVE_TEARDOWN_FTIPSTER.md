# Competitive & Architectural Teardown: Ftipster vs LISA

> **Date:** 2026-09-29
> **Subject:** `https://ftipster.com/paid-betting-tips/`
> **Purpose:** Reverse-engineer how Ftipster sources match data and odds, audit their published
> record, and determine what LISA should keep, fix or remove.
> **Method:** Full HTML/Markdown capture of the paid-tips index, the Bet-Of-The-Day service page,
> the complete Bet-Of-The-Day history (27 months, 849 published picks), the free-tips page and the
> order page. Every number below is computed from Ftipster's own published tables by
> `docs/analysis/grade_ftipster.py`, `forensics.py` and `continuity.py` — not estimated.

---

## 0. Executive Summary

**Ftipster is not a prediction engine. It is a price-band screener with a WordPress storefront.**

The evidence is in their own archive. Across 849 published picks spanning 2024-05-01 → 2026-08-31,
they used **20 distinct prices**, with **89.2% of all picks priced between 1.53 and 1.62**. That is
not a market price. That is a *target*. They scan the day's global fixture list, find a match where
the "safe" market is currently quoted inside a narrow band, and publish it. The price is an
**input constraint**, not an output.

**They are profitable, and I will not pretend otherwise.** I graded all 841 gradable picks with full
Asian-line handling (half-win, half-loss, push). Result: **75.9% win rate, +0.113 units per bet,
+13.1% ROI, t = 7.50.** That is not noise and not fabrication. They have a real edge.

**But the edge is not theirs, and it is not yours either — it is the same edge: thin markets.** 850
distinct clubs appear in 849 rows. 61.5% of clubs appear exactly once. Only 21.9% of picks involve a
Big-5 / Champions-League club. They are systematically harvesting mispriced Over-2.5 and BTTS
lines in low-liquidity leagues where sharp books are absent and soft books leave 6-10% on the table.

**LISA already has the better version of this trade, unimplemented.** The engine already does
multi-book Shin de-vigging with leave-one-out EV. That is *strictly more powerful* than a
single-book price-band screen. The problem is not the architecture — it is that the gate is
configured to throw the edge away (`require_positive_ev=False`, `markets="h2h"` only, sharp keys
tuned for efficient leagues), and the product surface ships fake data.

**The single most important finding: Ftipster's own archive proves their product works. Your README
proves yours does not — on your archive, in your leagues, at your settings.** The gap is not
engineering. It is *target selection*. They bet where the market is thin. You bet where it is sharp.

---

## 1. How Ftipster Sources Match Data and Odds

### 1.1 What is observable

| Layer | Finding |
|---|---|
| Platform | WordPress (custom theme, `wp-content/uploads/` asset tree, block editor markup) |
| Payments | **PayPal only** — hosted-buttons and Smart Buttons (`paypal.com/ncp/payment/…`). No Stripe, no own processor. |
| Fulfilment | **Manual email.** "you will receive your paid prediction via **Gmail**" (`ftipster369@gmail.com`) |
| Contact channel | Gmail + Skype. No live chat, no phone. |
| Public data | Results and picks are **manually keyed into a WordPress table** — the archive is static HTML, not rendered from a feed |
| Machine-readability | **Zero.** No JSON-LD `SportsEvent`, no `schema.org` markup, no `wp-json` REST exposure, no OG `sports` tags, no `/api`, no RSS. The site is invisible to structured-data consumers. |

**Consequence:** the entire public track record is a hand-maintained WordPress table. It is
**not machine-verifiable by a third party**. It cannot be scraped cleanly, and it cannot be
independently audited without a human reading 849 rows — which is precisely what I just did.

### 1.2 The forensic fingerprints

Five signals in the data reveal the mechanism without needing their source code.

**Fingerprint 1 — the price ladder is a filter, not a quote.**

```
1.57  314 picks  37.3%   ####################
1.53  253 picks  30.1%   ####################
1.60   67 picks   8.0%   ######
1.55   55 picks   6.5%   #####
1.61   33 picks   3.9%   ###
1.67   29 picks   3.4%   ##
1.62   28 picks   3.3%   ##
...
20 distinct prices. 89.2% inside 1.53–1.62.
```

Genuine best-available prices are continuous — you would see 1.541, 1.567, 1.583. Ftipster shows
20 values, and two of them (1.57, 1.53) account for **67.4% of all picks ever published**. This is
the shape of a *screen* ("find me a pick priced 1.53–1.62"), not of an *observation* ("here is what
the best book offered").

Confirmed in their own copy: *"we keep an eye out for **dropped or increased odds**"* and *"The
reason we don't post them from the evening is that we **monitor the movement of the odds**."*
They run an odds-movement screen, daily, manually.

**Fingerprint 2 — a rolling global scan, not a coverage of major leagues.**

- 850 distinct clubs across 849 picks
- 61.5% of clubs appear **exactly once** in 27 months
- 44.7% Over 2.5 · 22.4% Over 2.5/3.0 · 12.5% BTTS Yes · 9.9% Asian Handicap · 3.1% Under 2.5
- **Zero corners** in 849 BOTD rows, despite running a paid "Corner Tips" product

Their FAQ claims: *"We **only focus on the major football leagues**."* Measured: **21.9%** of picks
contain a Big-5 / UCL club name. The real sample is Norwegian Eliteserien, Finnish Kakkonen,
Icelandic Besta deild, Estonian Esiliiga, Chinese League One, Brazilian Série C/D, Welsh Cymru
Premier, Northern Irish Championship, and Finnish/Norwegian third tiers.

This is the opposite of a major-league service. It is a **thin-market harvester**.

**Fingerprint 3 — archive continuity is near-perfect, so the winners are not cherry-picked.**

```
Span            2024-05-01 → 2026-08-31  (853 calendar days)
Published rows  849            → 0.995 picks/day
Duplicate dates 0
Missing dates   4 — all Christmas/NYE fixtures (24-25 Dec 24, 31 Dec 24, 25 Dec 25)
```

One pick per day, every day, no gaps, no duplicates. **They publish losses.** This is
meaningfully better than most tipster sites and it is why the t-statistic holds up.

**Fingerprint 4 — one product per market = a market-by-market screen.**
Nine paid products (BOTD, High Odds, O2.5, U2.5, AH, Double Chance, ACCA, BTTS, Corners), each with
its own archive. Each is a **filter on the same underlying feed**:

| Product | Screen |
|---|---|
| Bet Of The Day | best `p_true` pick in the 1.53–2.10 band |
| High Odds | same screen, `odds > 2.00` |
| Over/Under 2.5 | same screen, restricted to one market |
| BTTS | same screen, restricted to one market |
| Asian Handicap | same screen, restricted to one market |
| Corners | same screen, restricted to one market |

The "expert tipster Victor" is brand copy. The *unit of work* is a filter criterion.

**Fingerprint 5 — the "chance %" and "stake" fields are not calibrated.**

From the free-tips page:

| Match | Type | Odds | Implied | Claimed "chance" | Gap |
|---|---|---|---|---|---|
| Czech Rep v England | 2 | 1.34 | 74.6% | 85% | +10.4pp |
| Scotland v Switzerland | 0.0 Away | 1.37 | 73.0% | 90% | +17.0pp |
| Slovakia v Kazakhstan | 1 | 1.28 | 78.1% | 85% | +6.9pp |

Their FAQ defines stake 10/10 = ">90% chance", 9/10 = "85-90%". So `chance` → `stake` is a
**lookup table, not a probability estimate**. And the MEGA Weekend table advertises
`Over 1.5 → 97%`, `Over 1.5 → 95%`, `Over 1.5 → 94%`. Over 1.5 goals occurs in ~85% of all
football matches. 97% is a marketing number.

**Critically: the public archive publishes NO confidence figure at all — only the price.** So the
one number a customer would need in order to evaluate whether their tipster is well-calibrated is
exactly the number they withhold.

### 1.3 Most likely actual mechanism

Reconstructed, with confidence levels:

1. **A purchased automated tip feed** (high confidence). The `chance %` + `stake` + market taxonomy
   is the signature output of commercial tip-feed APIs — the product category is real and
   documented: STATSCORE's *TipsterAPI* ("100+ markets… automated hints… powered by top mathematical
   models"), Forebet's Kelly-criterion value list, and white-label Tipster Script WordPress plugins
   whose entire pitch is *"we integrate directly with premium providers."* Ftipster's WordPress
   stack + Gmail fulfilment + market taxonomy matches this shape exactly.

2. **A daily manual price-band screen** (high confidence). The 20-value ladder, the odds-movement
   language, and the daily cadence are a human running a filtered list, not an automated pipeline.

3. **Manual result entry** (high confidence). The archive is static HTML. Someone types in each
   day's scoreline. The `Postp.` / `Abnd.` / `*1 : 1` / `2 : 1*` messiness is human data entry,
   not an API.

4. **A proprietary statistical model** (low confidence, and I doubt it). Nothing in the output
   requires one. Over 2.5 + BTTS + low-liquidity-league line shopping fully explains the returns.

---

## 2. The Audit: Their Published Record, Graded

I graded every gradable pick with correct Asian semantics — quarter balls resolve to half-win /
half-loss, whole balls push, `Over 2.5,3.0` is two half-staked lines.

```
Rows parsed      849
Graded           841   (8 ungradable: 2 cup-winner/qualify markets, corners, "Postp.")
```

### 2.1 Headline

| Metric | Value |
|---|---|
| Bets | 841 |
| Win rate (incl. pushes) | **75.9%** |
| Win rate (excl. pushes) | 76.5% |
| Average price | 1.576 |
| Breakeven win rate at that price | 63.5% |
| Mean P&L | **+0.113 units/bet** |
| Std dev | 0.437 |
| t-statistic | **7.50** |
| 95% CI on edge | [+0.083, +0.143] units/bet |
| Flat-stake ROI | **+13.1%** |
| Return on turnover | **+19.0%** |
| Max drawdown (flat 1u) | **−5.96u** |
| Max losing streak | 4 |

### 2.2 By market

| Market | n | Avg odds | Win % | ROI |
|---|---|---|---|---|
| Over 2.5 | 564 | 1.571 | 73.9% | +8.6% |
| BTTS Yes | 105 | 1.590 | 73.3% | +17.2% |
| Asian Handicap (all) | 83 | 1.567 | 93.1% | +30.4% |
| Under 2.5 | 26 | 1.586 | 92.3% | +23.3% |
| 1 (home win) | 14 | 1.601 | 78.6% | +27.4% |
| 2 (away win) | 11 | 1.674 | 81.8% | +38.5% |
| Over 2.0 | 12 | 1.577 | 85.7%¹ | +10.4% |
| BTTS No | 10 | 1.582 | 70.0% | +11.9% |

¹ excl. 5 pushes

**The edge is concentrated in Over 2.5 (67% of all picks) and in Asian Handicap (small n, so treat
the +30% with suspicion — 83 bets).**

### 2.3 By month — is it stable?

| Month | n | Avg | Win % | ROI |
|---|---|---|---|---|
| 2024-05 | 29 | 1.608 | 82.8% | +23.4% |
| 2024-06 | 30 | 1.565 | 83.3% | +21.7% |
| 2024-07 | 30 | 1.549 | 73.3% | +3.1% |
| 2024-08 | 31 | 1.568 | 67.7% | +4.5% |
| 2024-09 | 30 | 1.605 | 83.3% | +25.8% |
| 2024-10 | 31 | 1.604 | 80.6% | +15.5% |
| 2024-11 | 30 | 1.586 | 60.0% | **−3.7%** |
| 2024-12 | 28 | 1.602 | 78.6% | +17.4% |
| 2025-01 | 31 | 1.632 | 87.1% | +30.2% |
| 2025-02 | 28 | 1.564 | 71.4% | +3.3% |
| 2025-03 | 30 | 1.577 | 60.0% | **−1.8%** |
| 2025-04 | 30 | 1.577 | 80.0% | +16.6% |
| 2025-05 | 31 | 1.562 | 64.5% | **−0.3%** |
| 2025-06 | 30 | 1.599 | 63.3% | **−1.7%** |
| 2025-07 | 31 | 1.599 | 79.3%¹ | +19.2% |
| 2025-08 | 31 | 1.603 | 77.4% | +14.9% |
| 2025-09 | 30 | 1.563 | 80.0% | +16.9% |
| 2025-10 | 31 | 1.556 | 67.7% | +3.3% |
| 2025-11 | 30 | 1.548 | 60.0% | **−8.8%** |
| 2025-12 | 30 | 1.590 | 90.0% | +27.0% |
| 2026-01 | 30 | 1.573 | 86.7% | +29.9% |
| 2026-02 | 26 | 1.545 | 73.1% | +7.1% |
| 2026-03 | 31 | 1.593 | 83.9% | +22.9% |
| 2026-04 | 30 | 1.567 | 83.3% | +22.5% |
| 2026-05 | 30 | 1.553 | 83.3% | +23.9% |
| 2026-06 | 30 | 1.543 | 73.3% | +11.2% |
| 2026-07 | 31 | 1.542 | 77.4% | +11.9% |
| 2026-08 | 31 | 1.548 | 77.4% | +10.6% |

¹ excl. pushes

**Four losing months out of 28** (Nov-24, Mar-25, May-25, Jun-25, Nov-25 — five actually). The
recent 12 months are the *strongest* stretch in the sample. Drawdown is tiny: 5.96 units over 2.2
years. Bankroll survival at flat staking: 1% → 2.56×, 2% → 6.46×.

### 2.4 Goal-total selection bias

Distribution of total goals in the 841 fixtures they actually bet:

```
0 goals  3.4%     3 goals 22.4%     6 goals  8.2%
1 goal  10.9%     4 goals 22.5%     7 goals  3.3%
2 goals 15.6%     5 goals 12.2%     8+ goals 1.4%
```

Under 2.5 fires only **29.9%** of the time versus a real-world base rate of ~50%. They are not
guessing; they are **selecting fixtures from the low-scoring tail and backing the majority side**.
This is exactly what a price-band screen on a thin market achieves.

### 2.5 Verdict on their record

**The archive appears honest.** Daily continuity, no duplicate dates, no gaps outside Christmas,
and an edge that survives a proper t-test. I found no evidence of deletion.

**But three caveats matter commercially:**

1. **The archive is the *free/public* BOTD product.** The paid tips arrive by email and are
   unverifiable. The "Success Rate of Over 85%", "Daily Success Rate of Over 93%", "Over 15% ROI"
   and "AVG Odds 1.57 / 24 winning / 7 unrealized" claims on the BOTD page are **marketing claims
   with no published denominator and no graded record behind them**. My +13.1% is the *free*
   product. You cannot assume the €59 email tip behaves the same.

2. **No confidence data is published**, so their calibration — the thing that determines whether a
   bettor sizes stakes correctly — is invisible.

3. **The edge is not the customers'.** This is the decisive point. At +0.113 units per bet and €59
   per single tip, a customer needs **522 consecutive bets** to clear one €59 purchase. Volume
   pricing confirms they know: 30 tips for €549 = €18.30/tip, and 30 tips at 0.113u ≈ **3.4 units
   ≈ €60 of profit against €549 paid.** **The tipster captures essentially 100% of the edge. The
   customer is the liquidity.**

---

## 3. Critique of Ftipster's Logic

### What they get right (steal these)

| # | Practice | Why it works |
|---|---|---|
| 1 | **Target thin markets, not famous ones.** | Their edge lives in leagues with no sharp coverage. This is the entire strategy. |
| 2 | **Publish one pick per day, unconditionally.** | Kills cherry-picking suspicion. 0.995 picks/day, 0 duplicates, 4 holiday gaps. |
| 3 | **Publish losses, immediately.** | Full graded archive going back to May 2024. |
| 4 | **Low price band (1.53–1.62).** | Caps variance. −6u max drawdown over 841 bets. Sells "safety", not "edge". |
| 5 | **Market-per-product.** | Nine products from one feed = nine price points = nine SKUs. No marginal cost. |
| 6 | **Archive is the sales page.** | Their FAQ tells you to *"Calculate what results you would have if you bet with us"* before buying. Transparency as conversion. |
| 7 | **No free trial, defended on cost grounds.** | "We are still paying tipsters." Honest reasoning. |
| 8 | **Loss-replacement policy.** | Free next-day tip on a loss, 50% off on two. Genuine retention device. |

### What is weak, exploitable, or dishonest

| # | Problem | Detail |
|---|---|---|
| 1 | **"Chance %" is fabricated confidence.** | 97% for Over 1.5. Not a probability. Remove this from your product entirely. |
| 2 | **"Expert tipster Viktor"** | A human brand on a mechanical screen. Legal/reputational risk to you if you copy this. |
| 3 | **"Only focus on the major football leagues"** | Measured at 21.9%. A checkable factual misstatement. |
| 4 | **Email-only paid fulfilment.** | No account, no API, no machine-readable record, no dispute resolution. |
| 5 | **PayPal-only, Gmail-based ops.** | High friction, high chargeback risk, no recurring billing. |
| 6 | **Zero structured data.** | Invisible to AI assistants, aggregators, SEO. A structural dead end. |
| 7 | **No model, no method disclosure.** | Cannot audit. Cannot reproduce. No calibration data. |
| 8 | **Customer captures ~0% of the edge.** | The product is economically hostile to the buyer. |
| 9 | **Q4 2025 price drift to 1.54–1.55.** | Suggestive of margin pressure / feed degradation. Their edge may be decaying. |

### Structural weaknesses you can exploit

- **No API, no structured data, no machine-readability** → you win every AI-search and
  integration surface by default.
- **No calibration disclosure** → your Brier/ECE/CLV layer is a product they cannot copy without
  rebuilding from zero.
- **Gmail fulfilment** → a Telegram/API product with instant delivery is a strict upgrade.
- **No live/closing-line data** → they cannot report CLV. You already compute it.
- **No corner data in the archive** despite selling corner tips → their "wide coverage" claim is
  unverified.

---

## 4. Your Engine: What It Actually Is

This is the part you asked me to be blunt about.

### 4.1 The honest characterisation

**LISA is a market-implied odds refinery. It is not a predictor.**

```
pipeline.py:253  → consensus.refine()   [de-vigged, sharp-weighted blend of book prices]
                → gate.evaluate()       [P_top ≥ 0.75, CV ≤ 0.10]
                → staking.compute_kelly_stake()
```

`model.py` (Elo + Poisson) **is never imported by `pipeline.py`, `gate.py`, `consensus.py` or
`staking.py`.** It appears only in the display bulletin and in offline backtests. So when the
system says `p_true = 0.83`, that number is a **consensus of bookmakers' prices with the margin
removed** — not a belief about the world. You are reading back the market's own consensus and
calling it your product.

**This is not a criticism of the math. It is a statement of where the edge must come from.** A
de-vigged consensus cannot be beaten by a better de-vig — the best possible de-vig converges to the
market's true belief. Your only sources of edge are:

1. **Leave-one-out line shopping** — best retail price vs. the de-vigged mid of *all other books*.
   Real, and currently the only live alpha in the codebase.
2. **Disagreement between your model and the market** — computed in `bulletin.py:131-143`, shown
   as a warning label, and then **completely ignored** by the gate.
3. **Thin-market selection** — the only mechanism that actually works, and Ftipster's proof.

### 4.2 Configuration is throwing the edge away

| Setting | Current | Effect |
|---|---|---|
| `require_positive_ev` | **`False`** | The gate emits a pick on `p ≥ 0.75` alone, with no edge requirement. If no book beats fair price, `best_execution` is `None`, `exec_odds` falls back to `1/p_top`, EV computes to **exactly 0.0**, and Kelly returns `"Pass: Non-positive expected value"` → `stake_pct = 0.0`. **Your headline picks frequently have no edge and no stake.** |
| `markets` | `"h2h"` | You ingest **only moneyline**. Ftipster's entire edge is Over 2.5 / BTTS / handicap — markets you do not price at all. |
| `sharp_keys` | `("pinnacle","circa")` ×2.0 | Correct for sharp leagues, wrong for thin ones. In a league Pinnacle doesn't cover, "sharp" is a soft book with a thin book. |
| `min_margin_floor = 0.001`, `margin_weighted = True` | on | `w_i = sharp_mult / margin_i`. A 0.1% overround gets weight **1000**. A near-arbitrage market or a stale single-book snapshot silently dominates the consensus. |
| `credit_budget_daily` | 50/key | ~2 cycles across 22 leagues. You cannot scan a global fixture list at Ftipster's cadence. |
| Backtest override | `max(50.0, kelly) if kelly > 0 else 100.0` | When Kelly says **"Pass, no edge"**, the backtest bets a flat $100 anyway. **Your "Fractional Kelly" backtest is not Kelly-gated.** |
| `tuning.py` selection | best-of-35 in-sample | No held-out set. The per-season split is displayed but never gates selection. |
| `walkforward.py` | `"result": "WIN"` hardcoded | The INDEPENDENT MODEL CALIBRATION block measures `mean((1-p_actual)²)`. It is meaningless. |

### 4.3 Dead code that is 40% of the "AI"

| Module | Status | Note |
|---|---|---|
| `derived.py` | **never imported** | Full Poisson micro-pack (BTTS, clean sheets, team totals) with proper bisection λ-solve. Imported by nothing but its own test. |
| `statistical_features.py` | **never imported** | 407 lines of form/H2H/BTTS-rate features. Docstring promises "input features for the hybrid model in paid tiers" — no such model exists. |
| `exact_score.py` | **never imported** | Global constructed, then never read. |
| `longshot_ticket.py` | **never imported**, `storage=None` | A 60-leg parlay, 2 picks/day × 30 days. Its VOID-removal logic is documented and **not implemented**. |
| `flashscore.py` | probably non-functional | Scrapes client-side-rendered pages behind Cloudflare via `urllib`. 200+ "leagues" with a fabricated `LEAGUE_IDS` map (100 = Djibouti). |
| `live_settler.py` | written, never wired | `lisa start` doesn't start it. |

**So the honest count: your "AI/statistical prediction" surface is `model.py` (Elo+Poisson, display-only)
plus `bulletin.py`. The other ~1,500 lines of statistical machinery are unreachable.**

### 4.4 Dead and broken that a customer can see

| # | Issue | Impact |
|---|---|---|
| 1 | `web/js/app.js:2554` DAILY BOARD renders **hardcoded fake fixtures + odds** (Arsenal/Chelsea @1.85, Real/Barca @2.10…) with the real API call **commented out** on line 2549. `/api/daily-board` works. | 🔴 A customer cross-checks one price and you're finished. |
| 2 | Checkout is labelled **"Card Number (Simulated Stripe Gateway)"**, pre-filled `4242…4242`, then toasts **"Privilege elevated to Tier 2 Pro Trader!"** after `await sleep(600)`. | 🔴 Worst possible artifact in the repo. |
| 3 | `LISA_WEBHOOK_SECRET=` is **empty** → payment webhook returns 503; `POST /api/auth/update-tier` returns 403. | 🔴 **There is no working payment path.** Revenue does not exist today. |
| 4 | Telegram `/buy` mints a real activation key the instant a user taps **"I've Sent Payment"** — no chain check, no amount check, no confirmation. Bot prints "🎉 PAYMENT CONFIRMED!" | 🔴 Anyone can self-issue a free Tier 3. |
| 5 | Tier paywall is `localStorage.getItem('lisa_tier')`. `localStorage.setItem('lisa_tier','tier3')` unlocks everything. `/api/dashboard` returns all picks **unmasked**, and the frontend never calls `/api/picks` (where masking exists). `web/DESIGN.md:174` and `FRONTEND_DEV_GUIDE.md:190` both claim the server enforces it. | 🔴 Your paid product is free. |
| 6 | Telegram bot hardcodes **"Audited 84.0% win rate"** at `telegram_bot.py:3024` and `:3045`, in the same bot whose `/stats` correctly refuses to publish below 20 graded samples. | 🔴 |
| 7 | `index.html:504` **"MODEL ACCURACY 92.7%"**, `:517` **"1.2M+ PREDICTIONS MADE"**, `:414-468` a **fake leaderboard** (PlayMaker 4,532 picks 92%…). Overwritten by JS, but painted first. | 🔴 |
| 8 | **Three price lists**: $19/$49/$149 (landing + bot) · ₦14,999/29,999/49,888 (payments.py + subscription tab) · $19/$49/**$249** (`fixtures_generator.py`). At their own FX, ₦14,999 ≈ $10, not $19. | 🔴 |
| 9 | `picks` table has **no `actual_score` column** → the ledger's "Actual Score" column always renders `-`. A customer cannot verify a single settlement. | 🟠 |
| 10 | `clv` initialised to `0.0` not `NULL` → never-repriced picks count as "exactly at the close", dragging mean CLV toward zero. `closing_odds` initialised to `best_odds` — the "close" is the emit price. | 🟠 |
| 11 | `GET /api/ledger` caps at `limit ≤ 1000` with **no pagination** → past 1,000 settled picks the record is unreachable. | 🟠 |
| 12 | `manual_settle_match` (`storage.py:1239`) has **no state guard** — reachable from admin API and `/settle` Telegram command. "Write-once immutable" is true of the automated path, false of the operator path. | 🟠 |
| 13 | Tier 1 sells **"6-Bookmaker Booking Codes"** (`tiers.py:73`) which the bot explicitly refuses: *"LISA does not generate bookmaker booking codes."* | 🟠 |
| 14 | `tiers.py` advertises `steam_radar`, `portfolio`, `arbitrage_stream`, `early_bird` — **none implemented**. `bulletin.py:389` hardcodes `movement: None`. | 🟠 |
| 15 | `sortino = sharpe * 1.5` (`backtest.py:171`) — an invented fallback with no statistical basis. | 🟠 |
| 16 | `net_counterfactual` hardcodes `80.0` (`backtest.py:683`) — a fabricated dollar figure in an exported field. | 🟡 |
| 17 | `walkforward.py` docstring claims to test "GATED MARKET VS INDEPENDENT MODEL" but **never calls `gate.evaluate`**. It compares `market_follower` vs `model_value` only. | 🟡 |
| 18 | `study.py:205` iterates the archive season-major, league-minor → cross-league look-ahead up to ~9 months. | 🟡 |
| 19 | `tuning.py` and `staking.py` implement **two different "fractional Kelly"** strategies (λ=0.5 no shrinkage vs λ=0.25 with dispersion+drawdown shrinkage). | 🟡 |
| 20 | `markets.py:grade_asian_handicap` (quarter-ball correct) is imported by `study.py` only. The pipeline uses `odds.Score.grade_pick` which has **no quarter-ball logic**. | 🟡 |
| 21 | `markets="h2h"` means `markets.py:grade_asian_handicap` and `grade_total` can never fire in production. | 🟡 |
| 22 | `p.rank` is never set by any backend payload → **every pick falls through to the `else` branch and locks as TIER_2.** A free visitor sees 100% of picks locked, including the "Match #1 completely free" that `index.html:954` promises. | 🟠 |
| 23 | No prediction game exists. Hero CTA "MAKE YOUR PREDICTION" and "TOP PLAYERS" leaderboard both just `switchTab('picks')`. `lisa_predictions` is a view over the engine's own picks. | 🟡 |
| 24 | `CONVICTION SCORE: 27.3/10.0` — `compute_conviction_score` returns ~27 and the frontend gates at `>= 20.0`. The "/10.0" label is wrong. | 🟡 |

### 4.5 The thing your README already knows

Your own README, line 370:

> *"a quality-gated **79.7%** win-rate book still produced **negative ROI** at closing prices, and
> neither baseline nor an independent model beat the market"*

Line 311:

> *"**no config is robustly profitable on this archive**"*

**Ftipster: 75.9% win rate, +13.1% ROI, t=7.50, over 841 bets in thin leagues.**
**LISA: 79.7% win rate, negative ROI, over 7,155 matches in efficient leagues.**

**You have a higher win rate and worse ROI. That is the entire lesson.** Win rate is not skill;
it is a function of price band. Ftipster wins because they bet where the price is wrong. You lose
because you bet where the price is right.

---

## 5. Edge Comparison

| Dimension | Ftipster | LISA |
|---|---|---|
| **Data source** | Purchased tip feed + manual odds screen | The Odds API (real, live) + football-data.co.uk archive |
| **Books used** | 1 (single price quoted) | 5+ (`bet365, betandwin, pinnacle, williamhill, vcbet`) |
| **De-vigging** | None (published price is raw) | Shin (1993) z-solved, sharp ×2 weighted, margin-weighted |
| **Fair price computed?** | **No** — only a marketing "chance %" | Yes, `1/p_top` from de-vigged consensus |
| **Edge mechanism** | Thin-market line shopping | Leave-one-out EV (implemented), model disagreement (ignored) |
| **Markets priced** | O2.5, U2.5, O2.5/3, BTTS, AH, DC, 1X2, corners | **h2h only** |
| **Confidence published?** | Fabricated ("97%") | De-vigged probability (honest, uncalibrated) |
| **Calibration measured?** | No | Brier, log-loss, ECE, MCE, Murphy decomposition |
| **CLV measured?** | No | Yes (but `0.0` default, and `closing_odds` seeded to `best_odds`) |
| **Machine-readable** | **No** — static HTML tables, no schema | Full REST + Telegram |
| **Track record** | 841 graded picks, public, daily, 0.995/day | Real but **empty in production**, `None`/`n/a` everywhere |
| **Max drawdown** | −5.96u over 2.2 years | (Grade A only; 380 bets over 4 seasons) |
| **Measured edge** | **+0.113u/bet, +13.1% ROI, t=7.50** | **Negative at closing prices** |
| **Payment** | PayPal, working | **None — webhook secret unset** |
| **Deliverable** | Email, manual | Telegram + REST, automatic |
| **Cost structure** | Human-in-the-loop daily | Fully automated (but under-funded: 50 credits/day) |
| **Legal exposure** | Low — "for entertainment", BeGambleAware | Higher — must publish real numbers or be liable |

### 5.1 Where you genuinely win

1. **Method transparency.** You can publish Shin parameters, CV, book count, leave-one-out EV and
   calibration. They can publish nothing but a price.
2. **Machine readability.** REST + JSON-LD + Telegram. You can be the answer in AI assistants;
   they are invisible.
3. **Auditability.** Your archive is a CSV with source attribution. Their archive is HTML someone
   typed.
4. **The dead code is the product.** `derived.py`, `statistical_features.py` and `model.py` are
   1,500 lines of unused statistical machinery that already computes the exact markets
   (O2.5, BTTS, clean sheets, correct score) where Ftipster's edge lives. **Wiring `derived.py` into
   the gate is the single highest-leverage change in this document.**
5. **Multi-book de-vig is strictly better than a single-price screen.** Where Ftipster needs to
   guess at a fair price, you compute it.

### 5.2 Where they genuinely win

1. **They have a measured, positive, statistically significant edge. You do not.**
2. **They chose the right leagues.** This is 80% of the strategy.
3. **They have a working payment path and you do not.**
4. **They publish a track record. Yours renders `n/a`.**
5. **They ship. Their product is ugly but it takes money.**

---

## 6. Verdict: What Stays, What Goes, What Changes

### 6.1 REMOVE — immediately, before any customer sees it

| Target | Action |
|---|---|
| `web/js/app.js:2554-2566` fake daily board | Delete. Point `loadDailyBoard` at `/api/daily-board` (2 lines). |
| `index.html:2165` "Simulated Stripe Gateway" + `app.js:3609-3631` checkout | Delete the whole modal. Until a real provider is wired, **hide every Buy/Upgrade button** rather than show one that lies. |
| `telegram_bot.py:3024, :3045` hardcoded "84.0%" | Delete both strings. |
| `index.html:414-468, 504, 517` fake leaderboard / 92.7% / 1.2M+ | Delete. Render `n/a` until real numbers exist. |
| `generate_payment_key` call in `_handle_payment_confirmation` | Delete until chain/signed-webhook verification exists. **This is free Tier 3 for anyone.** |
| `fixtures_generator.py` | Delete the file. Its purpose is to fabricate a flattering track record, and it is one intern's mistake from being shipped. |
| `tiers.py` features `steam_radar`, `portfolio`, `arbitrage_stream`, `early_bird`, `booking_codes` | Either implement or delist. A sold feature that doesn't exist is what survives a chargeback conversation. |
| `$249` price list (`fixtures_generator.py:700-727`) | Dies with the file. |
| `sortino = sharpe * 1.5` (`backtest.py:171, 696`) | Replace with the real formula or omit Sortino. |
| `net_counterfactual` hardcoded `80.0` (`backtest.py:683`) | Compute from data or delete the field. |
| The two "84.0%"/"92.7%" claims in any marketing copy | Replace with the real measured numbers. |

### 6.2 FIX — correctness and honesty defects

| Target | Fix |
|---|---|
| `storage.py` `picks` table | Add `actual_score` (and a `score_source` URL). Set `clv = NULL` not `0.0`. Set `closing_odds = NULL` not `best_odds`. |
| `server.py:876 _handle_dashboard` | Accept the session, apply the existing masking at `server.py:1108-1154`, and **emit `rank`**. This alone closes the entire `localStorage` paywall hole. |
| `web/js/app.js:57, 1158-1167` | Remove `localStorage` as a tier authority. `state.currentTier` must come from `/api/auth/me`. |
| `server.py:1181-1195 /api/ledger` | Add `offset` pagination. Cap 1,000 hides your best asset. |
| `storage.py:1239 manual_settle_match` | Add `WHERE state IN (PENDING_STATES)` like `settle_pick:755`. "Immutable" must be true. |
| `LISA_WEBHOOK_SECRET` | Set it. **This is the difference between a business and a demo.** |
| Payment provider | Paystack is already configured in `config.py:173-174` and commented out at `payments.py:213`. Wire it. |
| `payments.py` price list | One source of truth (`TIER_PRICES_NGN`), one currency, rendered from `/api/tiers`. |
| `telegram_bot.py:540` | `Conviction Score: 27.3` is not "/10.0". Fix the label or normalise the score. |
| `walkforward.py:178-183` | Remove `"result": "WIN"` hardcode. It makes the calibration block meaningless. |
| `walkforward.py` docstring | Either add the gate as a third strategy, or stop claiming to test it. |
| `backtest.py:517-519` | Stop betting when Kelly says "Pass". Or rename the strategy to "Gated Flat" and say so. |
| `tuning.py:145-151` | Best-of-35 in-sample. Add a held-out season or stop calling it a result. |
| `study.py:205` | Sort chronologically before iterating. |
| `odds.py:80-200 grade_pick` | Port quarter-ball logic from `markets.py:55-105`. Duplicate the correct grader, or make `markets.py` canonical. |
| `tuning.py:282` vs `staking.py:90` | Two different Kelly. Pick one. |
| `telegram_bot.py:2575` `self.auth` | Uninitialised attribute. Guaranteed `AttributeError` when reached. |
| `CONVICTION SCORE` gate `>= 20.0` | Align with the actual score range. |
| `flashscore.py` | Delete, or fix. A 200-league map with fabricated IDs (`100 = Djibouti`) is worse than no module. |
| `live_settler.py` | Wire into `lisa start` or delete. |
| `accuracy_tracker = AccuracyTracker(storage=None)` | Dead global; every method `AttributeError`s. |
| `web/data/verified_users.json` | Delete (10 fake emails + a real-looking Telegram ID in a gitignored path). |

### 6.3 KEEP — this is the genuine advantage

| Asset | Why |
|---|---|
| **`shin.py`** | Correct Shin (1993) implementation, closed form for n=2, fixed-point for n≥3, tol 1e-12, and **ten distinct failure modes that all degrade to proportional de-vig rather than crash**. This is production-grade. It is also the thing Ftipster cannot do at all. |
| **`consensus.py` leave-one-out** | `loo_probability(exclude_key)` is the correct fix for the self-inclusion confound. Textbooks get this wrong. Keep it. |
| **The quality gate's ordering** | "Plausibility before certainty" — books-must-agree checked *before* probability. Right instinct. |
| **`markets.py:grade_asian_handicap`** | The most rigorous code in the package. Make it canonical, not the one `study.py` imports. |
| **`calibration.py`** | Brier, log-loss, ECE, MCE, CLV-by-market. Uniquely valuable. Publish it. |
| **`backtest.py` baselines** | `market_favorite` and `always_home` are computed honestly, not hardcoded. Most competitors fake these. |
| **`admin_api.py`** | 33 routes, CSRF HMAC, origin checks, rate limits, owner/role separation, audit log. Genuinely good. |
| **`auth.py`** | PBKDF2-HMAC-SHA256, 600k iterations, per-user salt, `hmac.compare_digest`. Correct and current. |
| **The honesty contract** | `dashboard.py:1-6` — *"Nothing is invented: when a value cannot be computed it is reported as None."* `storage.py:911` — *"Including pending rows in the denominator is the single easiest way to make a model look better than it is."* `MIN_SETTLED_FOR_STATS = 20`. `telegram_bot.py:3348` refusing the old dashboard block because it contradicted the backtest. **This is your brand.** Ftipster cannot say any of it. |
| **Write-once ledger** | `INSERT OR IGNORE` + `WHERE state IN (PENDING_STATES)`. Right design. |
| **`betexplorer.py` / `backfill.py`** | The team-name normalisation reasoning at `betexplorer.py:53-56` (never drop `united` or Man Utd → Man City) and the deliberate *refusal* to fuzzy-match abbreviations, so miss rate stays visible, is exemplary. Ship that discipline. |
| **`runtime.py`** | Validate-everything-before-swapping, boot-time re-validation of persisted overrides, readonly credential fields. Clean. |

### 6.4 BUILD — the two changes that decide this business

**A. Ingest the markets where the edge actually is. `markets="h2h"` → `markets="h2h,spreads,totals"`.**

This is the whole thesis. Ftipster's edge is Over 2.5, BTTS and handicap. You currently price only
the moneyline, which is the most efficiently arbitraged market in existence. You have
`derived.py` — a complete, tested, bisection-solved Poisson layer that turns a de-vigged 1X2 +
totals pair into BTTS, clean sheets, team totals and scorelines. **It is imported by nothing but
its own test.** Wiring it in is the highest-leverage change available.

Also: move `markets.py` into the pipeline so `grade_asian_handicap` is live, and port the
quarter-ball logic into `odds.grade_pick`.

**B. Set `require_positive_ev=True` and go shopping in thin markets.**

Right now your gate fires on certainty and then discovers there is no edge, and stakes €0. Flip it
and the pick only exists when a book beats the leave-one-out fair price. Then widen `SCOPE_LEAGUES`
past the 22 sharp competitions toward the 100+ leagues where two or three books quote and Pinnacle
doesn't. That is Ftipster's entire edge, available to you in code.

**C. Then: publish the honest numbers, loudly.**

You are selling to people who are losing money at the book. "79.7% win rate, −1.8% ROI, and we
show you the calibration curve so you can see exactly how much you should trust us" is a stronger,
more defensible sales asset than a fabricated 92.7% — and it is *already in your README*. Ftipster
cannot match it, because their archive is a hand-typed HTML table with no confidence data.

---

## 7. Appendix: Reproduction

All figures in this document are reproducible from Ftipster's own public pages:

| Script | Produces |
|---|---|
| `docs/analysis/grade_ftipster.py` | Per-market and per-month win rate, ROI, P&L with full Asian-line grading |
| `docs/analysis/forensics.py` | t-statistic, CI, price-ladder histogram, market mix, goal-total bias, drawdown, bankroll survival |
| `docs/analysis/continuity.py` | Date continuity, gap detection, club-repetition profile, Big-5 coverage |

Sources: `/paid-betting-tips/`, `/paid-betting-tips/bet-of-the-day/`,
`/tips-history/bet-of-the-day-history/`, `/free-betting-tips/`, `/order-page/`.
Captured 2026-09-29. Note that ftipster.com returns **HTTP 403** to plain programmatic fetches
(Cloudflare); a browser-equivalent user agent is required.
