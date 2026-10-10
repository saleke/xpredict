# Provider roles and routing

Adapters share the existing generation, history and settlement jobs. Each source
contributes only the fields and leagues its implementation actually supports.
Provider access, historical model coverage and usable prices are separate.

| Input | Main role | Boundary |
| --- | --- | --- |
| Openfootball CC0 JSON | Cached goal history and available half-time scores | Eight mapped leagues; file dates do not verify kickoff or settle predictions |
| OpenLigaDB | Timed German league calendars and results | Explicit supported mappings; no bookmaker prices |
| football-data.org | Verified timed fixtures/results and fallback history | Requires an entitled token and competition mapping |
| AllSports | Supporting calendars, results and available team/player observations | Missing fields and unavailable account scopes remain missing |
| API-Football | Supporting fixtures, history and bounded statistics/corners | Account/season limits and settlement reserves constrain collection |
| The Odds API / direct OddsPapi | Exact bookmaker offers and lines | Quotas, matching and bookmaker timestamps govern eligibility |
| SharpAPI | Optional supported bookmaker prices | Constructed when configured; does not block other adapters |
| TheSportsDB | Optional supplementary calendar discovery | Opt-in; clipped responses do not prove completeness |
| Scalper | Stored calendars, results, statistics and verified browser offers | Independent collectors, source-specific league/book/contract scope |

Openfootball maps Premier League, Championship, Bundesliga, La Liga, Serie A,
Ligue 1, Eredivisie and Primeira Liga. These are input mappings, not a promise of
complete current calendars or prices.

## Collection and identity

A league's verified calendar uses a primary provider, with another queried on
failure or no fixtures. Independent hosts can run concurrently; provider pacing
and durable request reservations remain authoritative. Bulk history does not
satisfy the verified-calendar requirement. Supporting Scalper mode checks fresh
stored records first and retains existing fallbacks.

Bulk history validates league/season, size, teams, scores, duplicate identity and
available periods before replacing a cached resource. Current-season files have
daily caching; completed seasons refresh less often. Conditional validators,
payload hashes and retry cooldowns survive restarts in the shared database.

Unknown file times are storage anchors, not invented midnight kickoffs.
Date-only results use conservative availability rules for training. A history
row joins a timed observation only for an unambiguous competition/team/date
pairing. Conflicting finals are withheld; discovery records cannot overrule a
confirmed result. Missing corner/player/period fields stay missing.

## Configuration and inspection

Use `.env.example` and `engine/lisa/config.py` for settings. Typical controls:

```dotenv
LISA_ENABLE_OPENFOOTBALL=true
LISA_ENABLE_SPORTSDB=false
LISA_SCALPER_MODE=supporting
```

Scalper defaults to `off`; collect into the same database/schema before enabling
it. `only` delegates network collection to Scalper and needs sufficient stored
history. It cannot create absent models or offers.

Inspect provider failures, collection/cache ages, verified fixtures, model-ready
teams, current price matches and qualifying picks separately in the operator
console. A worker's successful cycle does not mean full bookmaker inventory.

See [quota coverage](COVERAGE.md), [odds adapters](ODDS.md),
[provider probes](VALIDATION.md), [Scalper coverage](../scalper/COVERAGE.md),
and the dated [free-source](../research/FREE_SOURCES.md) and
[file-source](../research/FILE_SOURCES.md) assessments for research context.
