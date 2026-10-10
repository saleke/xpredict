# Provider validation

Run probes on a host with outbound HTTPS/DNS and configured private credentials.
A network failure is not proof of a bad key; a saved key is not authenticated
entitlement. Reports are machine-local observations, not permanent access claims.
Commands below can spend provider quota and must be run deliberately.

From the repository root with the engine installed:

```bash
PYTHONPATH=engine python3 scripts/validate_providers.py --history --output data/reports/provider-validation.json
python3 scripts/probe_oddspapi.py --output data/reports/oddspapi-validation-local.json
python3 scripts/probe_the_odds_api.py --output data/reports/the-odds-api-validation-local.json
```

The general probe samples one mapped competition and bounded history/price
requests. Use `--league` for another mapped scope and `--allsports-odds` only
when you intend that extra request. Direct OddsPapi `--account-only` avoids
billable catalogue/odds calls. The Odds API probe makes at most one paid league
request unless cached. None publishes predictions or sends notifications.
Individual price probes use shared storage to preserve quota/cache accounting.

RapidAPI transport is still a separate diagnostic:

```bash
python3 scripts/probe_rapidapi.py --output data/reports/rapidapi-validation-local.json
```

Its report includes a bounded field/type sketch, not raw leaf values. Follow
[the adapter boundary](ODDS.md) before testing optional provider authentication.
A successful response still needs schema and period/market validation before
worker integration. Do not treat a marketplace key as a direct-provider key.

All probes create a requested output's parent directory and withhold credentials,
raw error bodies and exception text. Exit status and report state distinguish
missing credentials, network blocking, failed requests and usable samples.
Samples do not establish complete league/history/bookmaker coverage or timely
settlement. Retain dates, quota metadata, quote counts and available bookmaker
timestamps when assessing a source.

Next validate competition/team joins, period and corner-field semantics,
subscription limits, restarts, quota exhaustion and confirmed final results.
Then collect dated prematch observations for chronological paper evaluation.
Do not approve staking from a provider connectivity check.
