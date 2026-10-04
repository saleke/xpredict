# Daily prediction lifecycle

Architecture follow-up: PostgreSQL is now the selected pre-production backend.
Shared repositories, authentication, pooling, migrations, independent settlement,
and separate-process deployment are implemented. See
[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md) for current startup and
validation status; older migration recommendations below describe the prior state.


The SQLite ledger remains the record of published selections. Extend it additively
with provenance, recommendation/forecast distinction, model version, and final-score
evidence. Do not create a second ledger or backfill simulated predictions.

One worker owns provider I/O. HTTP requests read persisted boards. Provider sets and
their response caches survive cycles; rebuild only when provider settings change.
Use a SQLite lease to avoid duplicate workers on the same ledger, and short SQL
transactions: never hold a write transaction while fetching or fitting.

Publication inserts immutable, pre-kickoff selection rows and the corresponding
board/forecast snapshot in one transaction. A key includes market line; repeated
cycles cannot rewrite probability, entry price, stake, or terminal result. Record
unpriced model forecasts with zero stake and unknown consensus statistics.

Settlement uses final official results fetched once per league, including all
overdue pending matches after outages. It continues while publication is paused.
Postponed or missing results stay pending; only explicit cancellations are voided.
Store final score and source alongside the grade. Never invent a closing price.

Boot runs a cycle without waiting for a visitor. The worker uses an interruptible
event wait and persists attempts, successes, failures, shortfalls and lease state.
Readiness reports stale/missing publication separately from HTTP liveness. Quiet
days still produce dated reports; data coverage is not a quota for invented picks.

The configured forecast window is an initial horizon. Before fetching prices,
generation extends it to the nearest verified, forecastable fixtures needed for
the volume target, using only the calendar already collected. It has no fixed
48-hour fallback ceiling. Unknown teams, provisional dates, canceled matches
and started fixtures cannot justify extending the forecast horizon. The report
records the decision. Listings and their size limits prioritize kickoff, then
winning probability; positive-EV gates continue to govern earning eligibility.
Daily Board defaults to Next Matches; explicit day/week filters remain bounded
by their dates. Cold start uses this policy without persisted manual overrides.

All web surfaces and Telegram read the same ledger and board snapshot. Archived
publications remain available after restart. Open tabs refresh from the read APIs.
Paid price/staking information is masked at the server, including nested parlay
legs. Unsupported catalog features carry an explicit availability status.

Payment confirmation is a request for verification, never evidence of payment.
Disable self-confirmed activation issuance; privileged verified webhook/admin flows
remain the provisioning authority. Do not claim blockchain/card integration until
that integration has been configured and tested.

Accumulator probabilities are theoretical. Multiplying quotes from different books
cannot produce an executable parlay. Even common-book straight prices are an
estimate until that book confirms its combined offer; label them accordingly and
do not size a wager from a theoretical parlay price.

Verification covers publication idempotency/rollback, different market lines,
restart durability, overdue recovery, aliases/correct scores, quiet days, outages,
pause/resume, lease contention, setting propagation, public read-only behavior,
payment denial, and worker shutdown. Fixtures are isolated from production data.
Full provider canary testing requires target-host credentials and network access.

References: [SQLite transactions](https://www.sqlite.org/lang_transaction.html),
[Python HTTP server shutdown](https://docs.python.org/3.12/library/socketserver.html).
