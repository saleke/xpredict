# Quota-aware coverage

The prediction worker now derives API-Football calendar and odds refresh intervals from its configured daily allowance and supported requested leagues. The worker may still run every five minutes; this does not mean a provider request every five minutes. Cached results remain available to each scan.

With 100 requests per day and nine leagues, the planner reserves 20 requests for settlement, budgets nine cold catalog lookups and eight backfill/enrichment requests, and divides the remaining ordinary capacity between calendars and odds. This yields three full scheduled refresh rounds per day: an eight-hour cache for each league's current season and first odds page. A cold-day envelope is 9 catalog + 27 calendar + 27 odds + 8 history requests, below the ordinary ceiling of 80. Cache expiry, failures, manual probes, season changes and other workers can alter realized consumption; the atomic shared reservation ledger remains the hard limit.

Odds league priority rotates daily. Only one odds page per league is requested under this plan; provider pagination is reported as partial coverage. This preserves breadth but cannot promise prices for every fixture. Unsupported or unavailable seasons are still withheld. A small allowance that cannot fund a full daily round is explicitly reported as insufficient.

API-Football historical collection now has an additional shared daily cap: eight requests at the default limit. Those requests also count against the ordinary/shared allowance. Restarts and key replacement do not restore this cap. A large initial training backlog will therefore take time to collect. Existing enrichment requests are part of this same cap.

Settlement uses a separate fixture cache so it cannot inherit the forecast worker's long cache lifetime. Its refresh cadence divides the reserved allowance across due leagues. Nine due leagues and 20 reserved requests yield a 12-hour API-Football cache. Other result providers retain their independent cadences. The reserve also serves result enrichment; broad corner settlement can consume it before all updates arrive. Prompt automatic settlement across every league is not guaranteed by this small free allocation.

Admin → Settings → Data Providers displays the coverage plan, locally reserved requests, supporting-provider estimates and settlement cadence. AllSports retains its existing hourly limit and 15-minute fixture cache; OddsPapi retains batched odds collection and its account-constrained quota ledger. Their displayed request estimates do not establish authenticated league coverage or account entitlements. Football-data.org, OpenLigaDB, TheSportsDB and SharpAPI retain existing limits/caching; this is not yet a universal account-aware optimizer.

Price freshness and model evidence requirements remain independent of the collection plan. Eight-hour odds collection cannot establish current executable odds. More league fixtures do not necessarily mean more eligible recommendations. No live-event product or profitable model is implied by this scheduler.

The planning example uses the configured free-tier allowance, not a verified
account entitlement. Check [API-Football's plans](https://www.api-football.com/pricing)
and [OddsPapi accounting](https://oddspapi.io/us/docs/requests-and-quota) against
the actual subscription before changing budgets. See [odds adapters](ODDS.md)
for credential replacement and [provider validation](VALIDATION.md) for probes.
