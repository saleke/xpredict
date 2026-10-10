# Models and evaluation

## Current football models

`league_model.py` fits league-specific Dixon–Coles goal distributions from
historical observations available before the fit cutoff. The score matrix
supplies match outcomes, goal/team-goal totals, BTTS, double chance, draw no bet,
exact scores and supported Asian-line payout distributions.

`corners.py` models observed corner counts separately. Goal probabilities cannot
stand in for corner probabilities. Missing/insufficient team history prevents a
supported forecast; raw cards or player fields do not activate additional models.

Market matching must preserve fixture, bookmaker, selection, line, regulation
period and payout contract. Whole and quarter lines require their actual push or
split-payout outcomes. For a binary no-refund contract, estimated EV is
`p * decimal_odds - 1`; use settlement-aware payouts for other contracts.

The [curated-feed policy](../product/PICK_FEED.md) applies the 1.18 floor to the
**offered price**. `LISA_BOARD_MIN_FAIR_ODDS` remains a compatibility input and
no longer excludes high-probability outcomes. Fair odds are a model result,
not proof that a bookmaker offers a bet. Model probability, estimated edge,
quote freshness and reviewed evidence are independent checks.

## Legacy bookmaker consensus

`shin.py`, `consensus.py`, `gate.py` and `pipeline.py` retain the original
multi-book consensus pipeline. Shin de-vig removes margin per bookmaker, using
a two-outcome closed form or an iterative multi-outcome solve, with a recorded
proportional fallback for unstable inputs. Consensus weights account for margin
and configured sharp books. Leave-one-out probabilities keep a book's own price
out of its execution-edge reference.

The legacy certainty/book-count gate and legacy historical strategy differ from
the active league-model feed. Their thresholds and backtest returns must not be
presented as validation of the current pick selector. The old legacy transport
can remain disabled while the supporting The Odds API adapter is enabled.

## Evaluation

The retained [historical baseline](../research/model-evaluation.json) describes
`league-dixon-coles-v3` on 7,156 archive observations. It records source hashes,
monthly chronological fit cutoffs, market baselines, calibration metrics and
limitations. It explicitly has `approved_for_staking: false`. Archive odds lack
executable timestamps; those metrics do not establish available returns.

Regenerate analysis into ignored local storage:

```bash
PYTHONPATH=engine python3 scripts/evaluate_models.py
```

The default output is `data/reports/current-model-evaluation.json`. Use
`--test-from`, `--league`, `--output` and optional CSV paths for an explicit
research scope. Keep fit, tuning/calibration and final evaluation periods
separate. Fields observed after kickoff cannot enter an earlier prediction.

The evidence gate validates model version, scope, configuration and approval
artifacts before authorizing staking. A new data source, faster collector or
larger market catalogue is not an approval artifact. Paper mode keeps stakes
zero regardless of research rankings. Closing-price/return research needs dated
bookmaker observations, exact settlement rules and documented coverage gaps.
