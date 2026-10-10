# Odds adapters and credential handling

The active daily feed can combine supporting price adapters with Scalper's
stored prices. An API catalogue is broader than any one adapter's accepted
contracts. A matching fresh quote is necessary for an earning opportunity.

## Credentials

Keep credentials in private `.env` or owner-controlled provider overrides.
Admin → Settings → Data Providers supports replace, disable and environment
inheritance. Workers reload effective credentials on a subsequent cycle;
replacement does not reset shared account usage.

Vercel uses encrypted PostgreSQL-backed overrides and requires
`LISA_CREDENTIAL_ENCRYPTION_KEY`. Preserve that Fernet key across deployments.
Native file-backed overrides use a private permission-600 file and require
protected backups; they are not encrypted merely because the file is hidden.
Admin reads and audit events never return credential values. Runtime settings
and provider credentials are separate stores.

## Direct OddsPapi

`ODDSPAPI_KEY` configures the direct adapter. It batches competitions, caches
metadata and retains durable account-scoped quota reservations. Actual account
catalogues determine competition/bookmaker access; unknown slugs are withheld.
The accepted bridge covers full-match result, BTTS and supported goal-total
contracts. Other provider fields may be archived without entering a model.

Default configured ceilings are 250 requests with 25 reserved and a six-hour
price cadence. Account metadata can reduce usable capacity. Historical requests
and subscription renewal semantics require authenticated verification; key
replacement does not create a fresh subscription. A six-hour cache is not a
minute-level executable price feed.

## The Odds API

Enable the supporting v4 adapter with `LISA_THE_ODDS_ENABLED=1` and private
`THE_ODDS_API_KEY`. This is separate from the disabled legacy poller controlled
by `LISA_ENABLE_ODDS_API`.

Default controls are 500 monthly credits, 50 reserved, 14 daily credits, region
`eu`, markets `h2h,totals`, and a 24-hour cache. Costs depend on requested markets
and regions. Least-recently-collected priority rotates available league coverage.
Daily and subscription reservations persist across restarts and reconcile
reported usage conservatively; calendar rollover is not proof of renewal.

The adapter accepts full-match three-way results and supported goal totals.
Two-way results are not silently interpreted as draw no bet. Team identity,
competition, kickoff tolerance, line/period and bookmaker timestamps must match.
Its current implementation does not price corners, cards, player or first-half
markets merely because the external catalogue lists them.

## OddsPapi via RapidAPI

`ODDSPAPI_RAPIDAPI_KEY` and the `oddspapi_rapidapi` override are separate from
the direct credential. `RapidApiTransport` implements bounded, verified GET
routes, header authentication, redacted failures, durable reservations and
cooldowns. **It is not connected to prediction workers.**

The configured probe route is `odds-api1.p.rapidapi.com/fixtures/odds/main`, with
`since=0` and selected bookmakers. Response schema, cursor semantics and actual
account entitlement still require validation. Direct OddsPapi account/history
rules do not transfer to a marketplace subscription. Request limits and
bandwidth accounting are distinct; automatic subscription renewal is unverified.

The probe defaults to RapidAPI headers. Optional `--provider-auth rapidapi` or
`--provider-auth direct` tests explicit upstream query authentication using
private credentials; neither proves the route needs it. Never put keys in
`--params` or invent a language/version prefix.

## Freshness and product boundaries

Preserve receipt, provider-update and bookmaker-change/confirmation times
separately. Retrieving a cached price does not refresh its bookmaker timestamp.
Publication and subsequent reads recheck deadlines and local Scalper state.
Expired/suspended/changed offers cannot remain executable; immutable original
ledger observations remain available for audit and settlement.

Stored straight prices do not prove a combined accumulator quote. Unpriced
forecasts have unknown EV. Model evidence and paper mode govern stakes
independently of adapter success. See [selection policy](../product/PICK_FEED.md)
and [validation commands](VALIDATION.md).

Provider references: [OddsPapi](https://oddspapi.io/us/docs),
[The Odds API v4](https://the-odds-api.com/liveapi/guides/v4/),
[RapidAPI authentication](https://docs.rapidapi.com/docs/configuring-api-security).
