# Supporting odds integration

OddsPapi supplements the existing fixture, result and statistics providers. It does not replace them. Its adapter batches competitions in one odds request, caches metadata for seven days and polls prematch offers every six hours by default. Tournament slugs and bookmaker entitlements must match the authenticated catalogs; unknown competitions are withheld.

## Activation and credentials

Supply an OddsPapi key through `ODDSPAPI_KEY` or, as an owner, through **Admin → Settings → Data Providers**. Replace, disable and restore environment credentials without editing source code. Worker instances read updated credentials on their next cycle and rebuild their supporting providers. Replacement does not reset the usage ledger of the same subscription.

Secrets are held in an atomic, permission-600 file configured by `LISA_PROVIDER_CREDENTIALS_PATH`, separate from ordinary settings and telemetry. The file is plaintext, not application-encrypted. Docker services share the private application-data volume; include that volume in protected backups. Deployments on separate hosts need a shared private credential store before using this mechanism. Keys are never returned by the admin API or recorded in its audit events. The original The Odds API now has a separate daily-worker supporting adapter; its legacy pipeline remains disabled. See THE_ODDS_API_INTEGRATION.md for the active credit-limited path.

## Quota and freshness

Defaults are a 250-request ceiling with a 25-request reserve. Actual account limits and usage constrain these settings. Reservations are atomic and durable across worker restarts and key replacement. Failed billable requests consume reserved capacity. Metadata cache hits do not. Historical requests are unmetered under the documented contract, but become unavailable when the billable allowance is exhausted. Scheduled collection attempts at most two finished fixtures per cycle and retries failures after 24 hours.

Subscription identity and its documented `valid_from` identify the budget scope. The ledger is deliberately conservative: if a provider renews its allowance without changing that identity/date, it does not automatically discard accumulated usage. Verify authenticated renewal behavior before relying on unattended monthly resets. Other account activity is reconciled when account metadata refreshes, so retain a reserve. A six-hour collection cadence is economical but cannot support minute-by-minute executable prices. Replacing a key does not grant a fresh allowance when it belongs to the same subscription.

## Markets and historical evidence

The initial verified-contract whitelist covers full-time result, BTTS and full-time goal totals, including quarter lines. Other offers are archived with their provider market, outcome and player identifiers, but are not passed into an unsuitable model. This does not deliver validated player props, cards, first-half predictions, live micro bets or executable same-game accumulators.

Immutable price observations retain receipt time, provider time and bookmaker update time separately. Cached data keeps its original receipt time. Provider `changedAt` alone does not establish bookmaker freshness. Unpriced or stale offers cannot become executable recommendations. Historical provider timestamps are retained without inventing bookmaker timestamps. API-Football and football-data half-time results are retained when valid; API-Football match statistics are retained from existing enrichment requests. Player-statistics collection is opt-in, not an extra automatic drain on free quotas.

PostgreSQL schema migration 4 adds indexed price observations. The history ledger supports auditing and subsequent evaluation work; it does not itself establish a profitable model. The existing evidence gate still withholds stakes without approved evaluation evidence.

## Validation status

The direct key is now supplied in private runtime configuration; it remains distinct from the RapidAPI key. To test only direct OddsPapi locally, run `python scripts/probe_oddspapi.py --output docs/oddspapi-validation-local.json`. This checks the unmetered account and samples one league through bounded metadata/odds calls. `--account-only` avoids billable catalogs and odds. The report excludes raw account credentials, identity and server text; it does not publish predictions. The initial sandbox attempt is recorded in `oddspapi-validation.json` and remains DNS-blocked.

Offline tests exercise quota concurrency, account/key replacement, safe credential storage, fixture ambiguity, price timestamps and bounded historical retries. Live validation on 2026-10-03 remains incomplete: the direct key is now supplied, but this environment cannot resolve the provider hosts. The successful local requests to some supporting providers do not establish direct OddsPapi access. PostgreSQL integration also requires a reachable test database. See [provider-validation.json](provider-validation.json) for the sanitized report and [FREE_SOURCE_RESEARCH.md](FREE_SOURCE_RESEARCH.md) for source coverage and licensing constraints.

Provider contracts: [OddsPapi documentation](https://oddspapi.io/us/docs), [request accounting](https://oddspapi.io/us/docs/requests-and-quota), [account metadata](https://oddspapi.io/us/docs/get-account).
