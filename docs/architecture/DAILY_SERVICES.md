# Generation, publication and settlement

The same jobs run as finite authenticated functions on Vercel and as independent
workers on a native host. All use the configured shared database. See
[hosting boundaries](SYSTEM.md) and [database architecture](DATABASE.md).

## Generation and publication

Generation requests verified calendars, reuses durable historical/provider
caches, fits or loads league models, fetches eligible prices and evaluates
supported contracts. Provider I/O and fitting never hold a write transaction.
Configuration changes invalidate the relevant provider/model caches.

The configured forecast window is an initial horizon. When it contains fewer
forecastable fixtures than the discovery target, generation extends it to the
nearest verified, unstarted fixtures with known modelled teams. The target is a
fallback minimum, not a maximum number of matches in the window. Unknown dates,
provisional fixtures and unknown teams cannot justify an expansion.

One transaction checks the generation lease, inserts original selection records
and saves the publication/forecast snapshot. Selection identity includes its
market and line. Repeated cycles do not rewrite entry probability, price, stake
or terminal grade. Unpriced analysis has zero stake and no measured EV.

[The pick-feed policy](../product/PICK_FEED.md) determines visible headlines and
qualifying alternatives. Read-time price checks exclude expired, suspended,
changed or removed offers without rewriting the saved publication or original
ledger. A new capture cannot extend an older publication's price deadline.

## Settlement

Settlement has its own lease and provider caches. It processes overdue pending
selections even when generation is paused, model fitting fails or prices are
unavailable. It uses confirmed regulation-time results and preserves the result
source and final score alongside each exact contract grade.

Postponed or missing results remain pending. Explicit cancellations can void
eligible contracts; a timeout alone is not a final result. Quarter/whole-line
contracts retain push and split-payout semantics. Terminal grades are immutable.
Missing closing odds remain missing.

## Reads, jobs and readiness

HTTP and Telegram views use saved projections and entitlement rules. Date filters
read stored calendar observations; no visitor fetches providers. Listings follow
kickoff order while quality ranks govern access and optional feed limits.

Workers persist starts, completions, failures, coverage shortfalls and lease
state. `/api/health` is liveness; readiness, publication age and price availability
are separate. Quiet days can publish an honest empty report. An outage must not
be hidden by invented fixtures or quota-padding selections.

Vercel invocations use bounded work and resumable history checkpoints; no daemon
is expected to survive a request. Native workers use interruptible waits and
supervised shutdown. Cadence guards and shared quota reservations prevent a
frequent trigger from implying equally frequent provider requests.

See [Vercel scheduling](../operations/VERCEL.md),
[local operation](../operations/LOCAL_RUNNING.md), and
[checks](../operations/CHECKS.md) for execution and verification.
