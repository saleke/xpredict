# Authenticated provider validation

Credentials are loaded from the private, git-ignored `.env`, never the example
template. The current machine has credentials configured for AllSportsAPI,
API-Football, football-data.org and SharpAPI. Their validity and account
entitlements remain unverified: all four provider hostnames fail DNS resolution
in this execution environment. See `provider-validation.json` for the dated
result. A network failure does not mean a key is invalid.

On a machine with outbound HTTPS and DNS, run from the project directory:

```sh
PYTHONPATH=engine python3 scripts/validate_providers.py --history --output docs/provider-validation.json
```

This checks one competition, one historical season (or a two-week historical
AllSports sample), and a single odds page per supported price provider. It never
publishes forecasts or writes the application database. Exit code 1 means a
requested check did not pass; JSON distinguishes missing credentials, unavailable
DNS and failed requests. Error bodies, exception text and credentials are omitted.
An empty sample does not prove lack of subscription entitlement. A successful
sample does not verify all leagues or historical coverage.

To test AllSports odds entitlement, explicitly add `--allsports-odds`; automatic
prediction jobs retain the existing configured odds setting. Use `--league` to
sample other mapped competitions, allowing for subscription quotas. Do not set
higher request budgets until actual account limits have been verified.

After connectivity is available, confirm competition mappings and corner-field
semantics with authenticated payloads. Then perform resumable historical backfill
and capture prematch offers for daily shadow evaluation. Keep stake approval
disabled until the model beats the relevant chronological market benchmarks.
AllSports remains supplementary; no existing provider is replaced.
