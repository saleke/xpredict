# Selection, price-readiness and persistence release

Implemented on 2026-10-06 following the
[improvement review](IMPROVEMENT_REVIEW.md). This delivers the first two roadmap
steps: selection/price correctness and batched persistence, with supporting
diagnostics. Expanded league catalogues, new enrichment feeds and new market
models are subsequent work.

The first-release measurements below are preserved for audit. The later
[pick-feed policy](../docs/PICK_FEED_POLICY.md) supersedes their user-facing
selection/access behavior: every visible opportunity now needs a qualifying
current offer, with one headline per match and bounded Tier 3 alternatives.

## Selection behavior

The 1.18 minimum applies to the actual offered price. Fair odds remain model
output and no longer exclude high-probability selections. The legacy
`LISA_BOARD_MIN_FAIR_ODDS` environment input and constructor argument remain
accepted for compatibility, but do not filter opportunities. The obsolete
setting is no longer editable in the operator console.

Probability, EV, historical sample and reviewed-evidence requirements still
control selection and staking. The validation configuration fingerprint now
includes the selection-policy version and offered-price/risk thresholds. Old
validation artifacts cannot authorize the new policy until reviewed with its
current fingerprint. Paper publication keeps all stakes zero.

## Published price lifecycle

Each priced opportunity carries `price_valid_until`, `price_book_key`,
`price_source_event_id` and `price_quote_identity`, alongside its separate
price-change and publisher-confirmation clocks. The deadline is bounded by
kickoff, the board's age allowance and any stricter source deadline. Native
Scalper offers use its configured age allowance; the default is 300 seconds.
Other board prices retain the existing 1,800-second allowance.

Publication and each subsequent read check eligibility. Native Scalper records
are also compared with local quote/event state. Removed or suspended offers,
changed prices, moved fixtures and expired confirmations lose financial status.
Browser source failure retains only the existing 30-second confirmation grace.
Unavailable local state fails closed. A fresh capture of an unchanged price
does not extend a previously published deadline; the generation worker must
publish a new observation.

Unavailable earning rows are removed. Winning/micro forecasts remain, with
`priced:false`, null offered price/EV and zero stake. Accumulator legs likewise
lose unavailable prices; affected combinations retain only their model forecast,
with no price, EV or stake claim. Saved publications and original ledger
observations are not rewritten by reads. Expiry discovered during publication
also prevents a stale price from entering the ledger as a recommendation.

Reads use bounded local SELECTs. They neither instantiate collectors nor run
staging migrations. An older browser publication without sufficient local
identity metadata becomes unpriced until regenerated. Free views mask the new
execution identity fields alongside existing financial fields.

## Readiness and diagnostics

`price_readiness` is returned by the publication API and service status.
`service.ready` remains worker/publication readiness; `service.price_ready`
separately describes usable prices for the published selections. A healthy
worker does not imply that current prices exist.

The projection includes fresh/expired/suspended/removed/changed price counts,
currently priced fixtures, earning-candidate counts, collection state and failed
source names. `coverage.fixtures_priced` reflects currently usable prices for
published selections; `fixtures_priced_at_generation` preserves the original
generation count. The explicit scope is `published_selections`, rather than a
claim about a bookmaker's entire inventory.

The interface distinguishes expired prices, failed collection, unmatched events
and current prices without qualifying earning candidates. Multi-source quote
diagnostics now sum source counts and count matched fixtures once, while
preserving each source's diagnostics. Successful core/browser collection records
include measured `merge_ms` for the staging merge, excluding later history writes.

## Batched merges

The production repository now pre-encodes quotes, fetches existing state in
bounded groups and uses grouped deletes and multi-row inserts. Writes use at
most 700 bound parameters per statement. Source locking, final-result protection,
identity validation, newer-observation precedence and empty-set revocations
remain within the same transaction.

Read-only replay of the saved public trial captures produced:

| Capture | Fixtures / quotes | Previous statements | Production statements | Production refresh median |
| --- | ---: | ---: | ---: | ---: |
| SportyBet | 50 / 1,612 | 1,865 | 25 | 122.02 ms |
| Pinnacle EPL | 20 / 1,482 | 1,585 | 23 | 126.45 ms |

Statement reductions are approximately 98.7% and 98.5%. Timings are local SQLite
measurements, with three refresh repetitions; they establish neither remote
PostgreSQL performance nor a browser collection SLA. The measured operation is
the repository merge, excluding schema initialization and history bookkeeping.
Baseline observations are preserved in
[the research evidence](research/2026-10-05-findings.json).

## Verification

- Complete engine suite: **924 passed, 32 subtests passed**, with an isolated
  PostgreSQL 18.6 server enabled; no PostgreSQL checks were skipped.
- Real PostgreSQL checks cover concurrent first inserts, source revocation,
  publication reads, shared worker leases and rollback. A late unique-constraint
  failure after multiple chunks of a 1,040-quote/260-fixture merge leaves the
  prior snapshot intact and the connection reusable.
- Controlled-clock checks cover the expiry boundary, source-failure grace,
  changed/removed/suspended prices, missing/future/naive timestamps, read
  immutability, accumulator downgrade and prevention of stale ledger stakes.
- Four frontend test scripts passed: **47 checks**. The public asset build passed.
- Local HTTP smoke checks returned 200 for health, public board, owner sign-in
  and paid board. The sample contained **78 displayed rows, 49 priced rows and
  25 earning rows**, with minimum offered odds **1.18**. Every priced row had an
  expiry deadline; paper stakes were zero and public prices were masked. These
  are observations from one local snapshot, not promised selection counts.
- Local Chrome verified owner sign-in, model-board rendering and the readiness
  message with **zero JavaScript errors**.

The [sanitized release results](research/2026-10-06-release-results.json) preserve
these checks and measurements without credentials, cookies or raw account data.

The local app uses the dedicated `data/localhost-scalper.db` trial database and
is available at `http://localhost:8080`. Production ledgers and private provider
credentials were not modified. The PostgreSQL checks used a separately named
temporary test database. No commit or push was made for this release.

## Pick-feed follow-up verification

The report of **Away team goals Under 7.5** exposed a separate publication
problem: synthetic derivative lines were generated without bookmaker offers,
and nearly certain model outcomes could win the winning ladder. Earlier
chronological journal caps also concentrated 50 cards on just three fixtures.

The worker now constructs derivative candidates from observed offers. HTTP and
Telegram opportunity views share current-price, probability, odds and value
acceptance gates. The feed chooses one headline per fixture before applying
its cap. Tier 3 receives at most two qualifying alternatives per match, with
different market families; rejected calculations remain private. Premium
browser views also consume these accepted alternatives. Subscription prices
are unchanged. See the linked policy for thresholds and access rules.

- Full engine rerun with PostgreSQL 18.6: **964 passed, 32 subtests passed**.
- Four frontend scripts: **52 checks passed**; asset build and whitespace checks passed.
- Local HTTP and Chrome snapshot: **10 curated picks across 10 matches**, with
  Free/Tier 1/Tier 2/Tier 3 unlocking **1/5/10/10** respectively. Tier 3 had
  **four** accepted alternatives. No member/anonymous query could upgrade access;
  proven-operator preview exit restored real server access.
- The snapshot evaluated **362** candidates across **15** modelled fixtures.
  **11** fixtures had current prices, **27** candidates qualified, and the lowest
  headline odds/probability were **1.28 / 55.74%**. These counts vary with the slate
  and source freshness; an earlier expired-price read correctly returned no picks.
- Available accumulator analysis had qualifying distinct-fixture legs and at
  least 35% adjusted probability. No combined bookmaker price was invented.
- Every published opportunity/combination remained at zero paper stake. Chrome
  reported **zero JavaScript errors** across all four previews.

[Sanitized follow-up results](research/2026-10-06-pick-feed-results.json) record
the audit and checks. The dedicated localhost app and collectors remain running;
the isolated PostgreSQL verification server was stopped afterward.

## Uncapped feed follow-up

The owner subsequently removed the 50-headline maximum. The default
`LISA_PICK_FEED_LIMIT=0` now means every qualifying match; a positive value is
an optional cap. The dashboard no longer adds a separate 50-row ceiling, and
the operator console accepts zero and limits above 50. Shared quality gates,
one headline per fixture and the agreed subscription allowances still apply.
The fixture-discovery target is a fallback minimum, not a maximum within the
selected time window.

Verification: **137** focused selector/dashboard/configuration/lifecycle checks
and **21** settings/HTTP API checks passed. A 150-match regression verifies that
the selector, dashboard, picks, opportunity board and curated-picks route retain
all qualifying matches for Tier 2/3. The asset build, admin JavaScript syntax and
whitespace checks passed. The restarted localhost snapshot reported `limit:null`
and **11 picks across 11 matches**; no extra selections were fabricated.

[Sanitized uncapped-feed results](research/2026-10-06-unlimited-feed-results.json)
preserve the verification without credentials or account data.

## Temporary all-tier verification

The dedicated localhost paper launcher now enables `--unlock-tiers`, as
requested by the owner for manual grading. The server requires both paper mode
and `LISA_PAPER_TIERS_UNLOCKED` before granting Tier 3 read access to guests and
all accounts. The normal default remains disabled. The flag is not editable
through the runtime console; no account tier or prediction is rewritten.

Verification: **109** focused backend checks and **21** local settings/API checks
passed. **53** frontend checks passed. An anonymous Chrome session saw the full
pick feed, the explicit testing banner, no locked cards or upgrade prompts in
the feed, and the qualifying research view, with zero JavaScript errors. The
asset build and whitespace checks passed.

The frozen manual sheet contains **nine headlines and eight alternatives**,
all with original ledger records, exact market contracts and zero paper stakes.
The timestamped CSV and matching JSON snapshot are in ignored
`data/manual-grading/`. Manual notes remain separate from automatic grades.
Use individual contract grades; the legacy match-wide grade action would apply
one result to different markets. Restart the launcher without `--unlock-tiers`
to restore normal subscription access.

[Sanitized verification results](research/2026-10-06-paper-unlock-results.json)
record the access checks and frozen-sheet checksum.

## Chronological match displays

Upcoming matches now display by earliest kickoff across the pick feed, curated
board, market alternatives, forecast cards, ticker and calendar. Accumulators
follow the earliest leg's kickoff, then adjusted joint probability; legs also
follow kickoff. Quality still selects the best offered market per match and
determines subscription access or any optional operational cap. Card `rank`
retains that quality meaning. Timestamp offsets are compared as instants;
missing display timestamps come last. The picks heading now says "Upcoming"
because the nearest available fixtures can be days away.

Verification: **158** focused backend checks and **57** frontend checks passed.
The public asset build and whitespace checks passed. Live public API and Chrome
checks confirmed **12 headlines** and **14 qualifying alternatives** in date
order, with October 9 fixtures before October 10. The calendar contained **53**
upcoming matches ordered October 9–12. These are observations of the current
snapshot, not guaranteed daily counts. Chrome reported zero JavaScript errors.
All-tier paper access remains enabled on localhost. The original frozen manual
grading sheet's checksum is unchanged.

[Sanitized chronological-display results](research/2026-10-06-chronological-results.json)
record the verification without account data or credentials.
