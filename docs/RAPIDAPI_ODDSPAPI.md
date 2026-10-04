# OddsPapi via RapidAPI: preparation and activation boundary

The user-confirmed Basic plan has 250 requests/month, a hard request limit, a rate limit of 1,000/hour, and a separate 10,240 MB monthly bandwidth allowance with paid overage. This is a marketplace subscription, not a direct OddsPapi account. The direct adapter's unmetered-account/history rules and its account payload cannot be assumed here.

## Implemented preparation

`ODDSPAPI_RAPIDAPI_KEY` is separate from `ODDSPAPI_KEY`. Admin → Settings → Data Providers includes `oddspapi_rapidapi` for owner-only replacement, disabling and restoring the environment credential. Private-file storage and runtime reload follow the existing credential mechanism. Generic settings and audit events do not expose the key.

`RapidApiTransport` supports explicitly verified GET paths on a literal `*.p.rapidapi.com` host. It sends `X-RapidAPI-Key` and `X-RapidAPI-Host` in headers, blocks redirects, bounds response reads and timeouts, sanitizes failures, and rejects mock responses. There are no guessed hostnames or endpoint defaults. An unverified route fails before a request is made.

Each attempted request, including account/history calls, reserves durable capacity before network access. The conservative free-plan maximum is 250 with a default reserve of 25; reported limits can only reduce capacity. Usage is independent of the key and persists through restarts. Reservations are atomic across transport instances. Reported usage raises the local usage floor and never erases concurrent reservations. Exhaustion remains blocked; throttling/authentication failures impose persisted cooldowns. Bandwidth overage is separate: no claim is made that the request ledger measures or prevents shared-account bandwidth charges.

RapidAPI documents quota reset as a seconds countdown, rather than an absolute Unix timestamp. The transport records its estimate, but does not reset usage from a guessed calendar boundary or the mere passage of that estimate. A stable subscription and confirmed billing-period identity are required. Automated renewal is not implemented. This conservative preparation intentionally cannot run indefinitely unattended until the real billing contract has been validated.

## Still needed

The subscriber has now supplied the actual host `odds-api1.p.rapidapi.com` and GET route `/fixtures/odds/main`, with `since=0` and bookmakers `pinnacle,stake,draftkings`. Those are the probe defaults; no language prefix or direct-v4 route is added. This verifies the request route, not its response schema, football filtering or whether `since` is a timestamp or another kind of cursor. These semantics must be established before scheduling incremental collection. The earlier tournament-image endpoint is not used.

This transport is **not connected to prediction workers yet**. No RapidAPI request has been made, and no marketplace offer has become a prediction or recommendation. A saved key alone does not activate it. The direct OddsPapi integration remains independent.

`scripts/probe_rapidapi.py` performs one bounded GET using the private runtime key and shared storage. It records sanitized quota metadata and a bounded field/type sketch, withholding all leaf values and credential subtrees. The default provisional subscription scope never resets automatically. Replace it only after confirming subscription/billing-period identity, not when changing a key. A successful JSON response still needs schema validation before connecting the provider to the feed.

Run from the project root with the installed engine and a private `ODDSPAPI_RAPIDAPI_KEY` credential:

```sh
python scripts/probe_rapidapi.py --output docs/rapidapi-validation-local.json
```

Tests use mocked responses; the actual subscriber-provided URL is checked without network access. They cover authentication placement, route validation, concurrent reservations, external usage reconciliation, replacement credentials, cooldowns, mock responses, bounded schema reporting and private admin credential separation. The sandbox attempt on 2026-10-03 still failed DNS before authentication or quota reservation; see `rapidapi-validation.json`. PostgreSQL execution and authenticated endpoint compatibility remain unverified.

Official contracts: [RapidAPI authentication](https://docs.rapidapi.com/docs/configuring-api-security), [response and quota headers](https://docs.rapidapi.com/docs/response-headers), [subscribed listing](https://rapidapi.com/odds-papi-odds-papi-default/api/odds-api1/pricing).

## Upstream authentication diagnostic

The direct OddsPapi documentation requires an `apiKey` query parameter, and RapidAPI explicitly permits additional provider authentication. Neither establishes that the private RapidAPI key is valid as the native key, or that this marketplace route needs it. The probe now has an explicit `--provider-auth rapidapi` diagnostic option to test that hypothesis at the supplied route. `--provider-auth direct` instead uses the separately supplied private `ODDSPAPI_KEY` and fails before requesting if it is missing. The default remains headers-only. Both query-auth options preserve URL/error redaction and quota reservations; neither bypasses cooldowns. No speculative language or version prefix is inserted.

```sh
python scripts/probe_rapidapi.py --provider-auth rapidapi --output docs/rapidapi-query-auth-local.json
```

Do not supply a credential in `--params`; the script inserts it privately. A successful response still requires schema validation. No authenticated query-auth test has succeeded yet in this environment.
