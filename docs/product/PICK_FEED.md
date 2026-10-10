# Pick feed and subscription access

The feed prioritizes the strongest qualifying pick from each match, with one
shared quality floor across the owner-confirmed subscription ladder. Published
picks are distinct from all background calculations and immutable journal rows.

## What is selected

The worker evaluates actual observed offers. Basic 1X2 probabilities remain
available as match analysis; it does not turn an unoffered derivative line into
a market candidate. Internally, the durable publication's `board.research`
retains the current candidate pool before display caps. This private worker
snapshot differs from the qualified `board.research` returned to members. The headline
feed uses the latest publication and rechecks its offers against expiry and
local native quote state on every read. It does not reuse old journal prices.

The default gates are:

- Future kickoff and a currently verifiable offered price.
- Offered odds of at least **1.18**.
- Estimated positive-payout probability of at least **55%**.
- Model expected value of at least **3%**.
- Positive expected log growth under the ranking comparison below.
- A supported regulation-time market contract and valid numeric goal lines.

Default goal-line safeguards are 5.5 for match totals and 3.5 for team totals,
configurable with `LISA_BOARD_MAX_TOTAL_LINE` and
`LISA_BOARD_MAX_TEAM_TOTAL_LINE`. These limits do not replace the offer check.
An unoffered team-total line, such as the previously reported **Away team goals
Under 7.5**, cannot become a pick merely because a model estimates high winning
probability. Unpriced or below-threshold candidates remain private calculations.

Select the strongest qualifying candidate within each match, then assign those
match winners a global quality rank. The default feed has **no numerical count
cap**. There is **one headline per match**, including on short slates. No worse
picks or extra lines are added to fill a count. Any supported market can be a
headline when it clears the same gates.

All upcoming match displays follow **earliest kickoff first**: today's games,
then tomorrow's, then later dates. When the next games are on the 9th, those
appear before games on the 10th and beyond. Compare timestamp instants, including
timezone offsets. Quality breaks equal-kickoff ties. The card's `rank` retains
its quality meaning for subscription access and any optional count cap; it is
not its position in the chronological display. Market alternatives follow the
same schedule. Accumulators follow their earliest leg's kickoff, then adjusted
joint probability; their legs are also chronological.

The comparison score is estimated positive-payout probability multiplied by
expected log growth at a fixed 2% comparison fraction. For refund and split
contracts, each possible grade uses its exact payout. This fraction is a ranking
device, not a proposed stake. Reviewed recommendations rank before unapproved
research candidates. High odds alone do not determine the rank.

These probabilities and edges are model estimates. Ranking is a heuristic, not
empirical proof of profitability. Paper mode and reviewed out-of-sample evidence
continue to control executable stakes independently. Changing the selection
policy or its settings invalidates old validation fingerprints.

`LISA_PICK_FEED_MIN_PROBABILITY` (default `.55`) and `LISA_PICK_FEED_LIMIT`
(default `0`, meaning unlimited) are configurable. A positive limit is an
optional operational cap; zero removes it across the publication and dashboard.
API metadata reports `limit:null` for an uncapped feed. Existing offered-odds/EV
thresholds and the confirmed subscription ladder still apply. Tier 2/3 receive
every qualifying headline; Free/Tier 1 retain their access allowances.
The discovery volume target is a fallback minimum for expanding the time
window, not a ceiling on fixtures within it. Removing the feed cap does not
invent additional matches or extend a source's calendar coverage.
Accumulator analysis requires
every leg to clear the same gates and an adjusted joint probability of at least
35%, configurable with `LISA_PICK_FEED_MIN_ACCUMULATOR_PROBABILITY`. Straight-leg
quotes do not prove a combined bookmaker offer; combinations remain unpriced,
zero-stake analysis, displayed by the earliest leg's kickoff with adjusted
joint probability breaking ties.

## Who sees what

| Access | Curated picks | Accumulators | Broader market research |
| --- | --- | --- | --- |
| Free | One featured pick | No | No |
| Free, Telegram verified | Two highest quality ranks, when available | No | No |
| Tier 1 | Up to the five highest quality ranks, all different matches | No | No |
| Tier 2 | Full qualifying curated feed | Available model combinations | No |
| Tier 3 | Same full qualifying feed | Available model combinations | Up to two qualifying alternatives per match, from distinct market families |

Higher tiers inherit the same quality ranking and chronological display. They buy coverage and analysis,
without a different quality floor or guaranteed results. Basic match forecasts
remain public as probability analysis, separate from picks. Tier 3 alternatives
must meet the same price, probability and value gates as headlines. Failed
candidates are excluded at every tier, and no alternative repeats its headline
market family. Alternatives share match risk; they are not independent stakes.
Unsupported markets are not invented to make a premium plan look fuller.

Access is enforced on `/api/dashboard`, `/api/picks`, `/api/opportunity-board`
and `POST /api/curated-picks`. Locked teasers expose fixture names and kickoff,
but no market, selection, line, probability, fair price, payout distribution,
execution identity or price. Anonymous/member tier query parameters cannot
upgrade access. Proven operators can preview a tier without changing their
account. Telegram commands use the same publication and access ladder; long
messages link to the full web feed rather than creating a smaller paid quota.

## What happens to remaining predictions

Current evaluated candidates stay in the private saved publication. Rejected
candidates are excluded from every user-facing opportunity array. Tier 3 sees
only the bounded qualifying alternatives, with suppressed totals visible as
diagnostics rather than selections. Predictions are not padded to justify prices.
Existing original journal entries remain untouched for settlement and audit.
A newly featured candidate outside the legacy ladder caps is also journalled.
The current research snapshot is replaced by subsequent publications; this
release does not create an immutable archive of every intermediate candidate
from every refresh.

`pick_feed` reports `display_order: "kickoff_asc"`, candidates, qualifying candidates/matches, displayed picks,
thresholds and rejection counts. Pipeline counts distinguish curated matches
from background forecasts and pending journal entries. Price readiness now
checks the complete retained candidate population when present, with scope
`published_model_candidates`; older publications use `published_selections`.
These counts describe modelled coverage, not a bookmaker's entire inventory.

## Temporary all-tier paper verification

For manual verification, start the local paper launcher with
`python3 scripts/local_paper.py --unlock-tiers`. This sets
`LISA_PAPER_TIERS_UNLOCKED=1` for that server. When both paper mode and this
explicit flag are enabled, guests and all account tiers receive the full Tier 3
prediction view. The UI labels this temporary access and removes upgrade prompts
from the pick feed. Accounts and subscription tiers are not rewritten.

Normal launches default to locked tiers; a non-paper server ignores the flag.
The flag is not editable through the runtime console. Restart without
`--unlock-tiers` to restore the normal ladder. Quality gates, offer freshness,
zero paper stakes and administrative authorization remain in force.

Keep manual verification snapshots under ignored `data/manual-grading/`
as timestamped CSVs and matching frozen JSON publications. Each CSV row needs
the exact market/selection/line, quote and probability at observation, publication
time, original journal observation and blank manual-result fields. Record a
confirmed regulation-time final score, source and individual contract grade
(`WIN`, `HALF_WIN`, `VOID`, `HALF_LOSS`, or `LOSS`). Corner markets also need final
corner counts. Alternatives share match risk.

Keep manual grades in the sheet and compare them with the automated Ledger.
The legacy match-wide manual-settlement action applies one grade to every
market on that fixture and must not be used to grade different contracts in
this trial. Saving manual notes does not overwrite automated grades or the
original prediction. The frozen sheet distinguishes the current model/price
from the journal's first observation; neither is silently substituted for the other.
