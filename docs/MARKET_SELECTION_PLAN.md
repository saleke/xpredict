# Market selection and feasibility

The goal is to rank fixture/market/selection/line combinations for probability and
price value, rather than select a favourite and attach extra markets to it.

## What is now implemented

The goal-score distribution supplies 1X2, double chance (1X/X2/12), match totals,
home/away team totals, BTTS, and correct-score forecasts. Winning selection chooses
the highest eligible probability across markets, with the minimum fair-odds filter
excluding negligible-return near-certainties. Mixed-market accumulator candidates
use one leg per fixture and remain theoretical, unpriced, and zero-stake until an
actual combined bookmaker offer is available. The Micro Bets board exposes the
new market families. Entry probabilities and official-result grades are persisted.

Consistency tests found and fixed an existing over/under suffix-sum error that
excluded one goal count from every Over probability. Totals and team totals are
now checked directly against the joint score matrix. The published ladder also
includes its intended upper 7.5-goal line.

Quote matching and model support are distinct. The current SharpAPI integration
requests moneyline and match goal totals; it does not yet fetch usable double-chance,
team-total, correct-score or corner prices. The new forecast families do not acquire
prices by multiplying/inverting other markets. They can enter the priced earning
ranking only if a compatible adapter supplies a real matching quote.

## What remains necessary

1. Persist multiple historical seasons by canonical team/league IDs. The current
   football-data adapter fetches the current season, which gives inadequate team
   samples early in a season. Subscription access to older history is insufficient
   until the ingestion code actually uses it.
2. Make goal modelling league-aware. The current fit pools leagues under shared
   scoring/home-advantage parameters. Different scoring environments and disconnected
   team-strength scales need league baselines and hierarchical shrinkage, particularly
   for cross-league competitions. Global fit sufficiency does not establish local
   team or market reliability.
3. Build an independently trained corner-count model. A hierarchical negative-binomial
   model is a candidate where observed corner counts show overdispersion; compare it
   with Poisson and simple league/team baselines. It needs dated match-level corner
   counts for both teams, opponent adjustment, trustworthy final counts, and real
   corner prices. Goal probabilities are not corner probabilities. Provider access
   and coverage must be confirmed before implementation claims.
4. Implement market-specific payout rules. Half-line totals are binary. Whole-line
   totals and draw-no-bet include pushes. Asian quarter lines have split stakes and
   half-win/half-loss outcomes; fair pricing, EV, stake sizing, ledger grades and
   analytics must all support those payouts before exposing them. Current new team
   totals publish half lines and reject quarter-line grading.
5. Add quote age, available bookmaker/region, match period, and execution validation.
   A statistically attractive price from the wrong book, an expired quote, or a
   first-half market compared against a full-match model is not an earning opportunity.
6. Validate each market and league in rolling time-based tests with only information
   available before kickoff. Training, calibration, tuning and evaluation must be
   temporally separated. Report calibration, Brier/log loss, net returns, drawdown,
   and uncertainty; benchmark against simple predictors and available market prices.
   More searched markets increase false discoveries, so reserve an untouched test
   period and penalize uncertain estimates instead of trusting the largest raw EV.
7. Add selection/exposure constraints. Several goal bets on the same match depend
   on the same underlying outcome; size aggregate exposure rather than each independently.
   Same-game combinations need joint probabilities from the score distribution and
   a confirmed bookmaker quote. Cross-match accumulators need correlation validation;
   the current fixed haircut is a heuristic, not a calibrated joint probability.
8. Separate immutable forecasts from later executable recommendations. Preserve the
   first forecast while recording the precise quote/time/model version for each
   recommendation. Track closing prices and settlements so probability accuracy and
   return on quoted recommendations are distinguishable. Current first-publication
   records do not substitute for a complete execution/portfolio ledger.

## API keys

Existing adapters: FOOTBALL_DATA_TOKEN and SHARPAPI_KEY; optional
LISA_SPORTSDB_KEY. OpenLigaDB requires no key. The retired Odds API is not needed
for the present stack.

API-Football is an additional candidate for richer statistics/corners and market
coverage, not an implemented adapter. It lists statistics and odds endpoints, but
historical corner availability and target-bookmaker markets need authenticated
coverage checks. Start by checking samples before purchasing or assuming coverage.

Verified provider pages: [football-data](https://www.football-data.org/pricing),
[SharpAPI](https://sharpapi.io/pricing),
[TheSportsDB](https://www.thesportsdb.com/api.php),
[API-Football](https://www.api-football.com/pricing).

## Feasibility judgement

Autonomous daily scanning, multi-market forecasts, saved results, and transparent
rankings are technically achievable. Consistently profitable overlooked opportunities
are an empirical research question, not a consequence of more APIs or more markets.
The shipped legacy backtest achieved 79.74% wins with -1.81% ROI; it is a different
strategy and does not validate the current model, but it demonstrates why hit rate
alone is insufficient. Prefer an empty value list to forced daily bets.

A launchable forecast/research product is feasible with the current architecture
once real database/provider integration passes. A defensible earning product needs
market coverage, richer history, corrected market pricing, model validation, and
shadow tracking before its profitability claims can be assessed.
