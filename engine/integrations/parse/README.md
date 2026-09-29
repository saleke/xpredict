# Parse integration (SportyBet Nigeria)

Self-contained uv project wrapping [Parse](https://parse.bot) typed/REST access
to the `sportybet-com-ng-api` marketplace listing.

Isolated from the LISA engine on purpose: it has its own `pyproject.toml` and
uses the uv workspace env at `engine/.venv`, so it cannot disturb the engine's
setuptools build or the root `.venv` that runs
`python -m lisa.cli start`.

## Setup

```bash
cd engine/integrations/parse
uv add parse-sdk
uv run parse login --no-web     # or: export PARSE_API_KEY=pmx_...
uv run parse init
uv run parse add --marketplace sportybet-com-ng-api
```

Always invoke as `uv run parse …`. The
`VIRTUAL_ENV=/…/xpredict/.venv does not match …/engine/.venv` warning is
expected and harmless.

## Status: partially working

### Working — `betexplorer-com-api` (no verification required)

Canonical `5daf70d1-eaad-46ba-afbb-c57c0d5ebc3e`, listing
`e19325ba-0ad4-47b4-826f-0a1b68aefa28`, `access_requirements: []`.
Callable directly on the canonical — no subscribe step needed.

```bash
uv run python check_access.py --betexplorer   # prints a live sample
```

```python
import json, os, urllib.request

CID = "5daf70d1-eaad-46ba-afbb-c57c0d5ebc3e"
req = urllib.request.Request(
    f"https://api.parse.bot/scraper/{CID}/search_matches?date=2026-09-28",
    headers={"X-API-Key": os.environ["PARSE_API_KEY"]},
)
matches = json.loads(urllib.request.urlopen(req, timeout=90).read())["data"]["matches"]
```

Per match: `event_id`, `country`, `league`, `home_team`, `away_team`, `time`,
`status` (`"FIN"` when complete), `score` (`"2:1"`), `odds_home`/`odds_draw`/
`odds_away`.

Measured 2026-09-29:

| Date | Matches | Leagues | Countries | with odds | with score |
|---|---|---|---|---|---|
| today | 144 | 35 | 28 | 3 | 3 |
| yesterday | 98 | 35 | 27 | 98 | 98 |
| −3 days | 117 | 35 | 18 | 117 | 117 |

**Key limitation — odds are closing, not pre-match.** On the current day only
3 of 144 upcoming fixtures carried odds; on completed days 100% did. These are
average closing odds, so the endpoint is a settlement / backfill / CLV-benchmark
source and **not** a pre-match price feed. At ~35 leagues/day it also does not
reach the 140+ league target on its own.

### Blocked — `sportybet-com-ng-api` (`requirements: ["card"]`)

Phone verification registered; **card verification did not**. Subscribe returns:

```json
{"detail": {"error": "verification_required",
            "requirements": ["card"],
            "verification_url": "https://parse.bot/settings?tab=account#verification"}}
```

Add a payment method at that URL, then re-run `uv run python check_access.py`.
Until then no SportyBet endpoint is callable. Two distinct 403s to expect, in
order:

1. Calling the marketplace canonical directly
   (`/scraper/8e652912-d760-4522-85ce-071e539a9c12/<endpoint>`)
   → `403 subscription_required` — *"Add it to your account from its
   marketplace listing, then call your own scraper id."*
2. `POST /marketplace/apis/b0c4782f-…/subscribe` → `403` (see above).

## Discovery shortcut

`access_requirements` on a marketplace listing is the single most useful field
for triage: `[]` means callable immediately, while `["phone","card"]` gates
every bookmaker listing. Checking it first avoids burning time on a source that
cannot be called. Verified 2026-09-29 — of six zero-requirement candidates
checked, only `betexplorer-com-api` resolved; the others returned
`404 Scraper with ID … not found` despite being listed with `access_requirements: []`:

| Listing | `access_requirements` | Result |
|---|---|---|
| `betexplorer-com-api` | `[]` | **200, works** |
| `flashscore-com-tr-api` | `[]` | 404 not found |
| `oddsportal-com-api` | `[]` | 404 not found |
| `zq-titan007-com-api` | `[]` | 404 not found |
| `oddschecker-com-api` | `[]` | 404 not found |
| `football-data-org-api` | `[]` | 404 not found |

So an empty requirement list is necessary but **not** sufficient — probe the
canonical with a real endpoint name before planning around a listing.

## Gotchas hit during setup

**1. `parse add --marketplace` skips this API and subscribes to nothing.**

```
uv run parse add --marketplace sportybet-com-ng-api
→ { "written": [], "skipped": [{ "slug": "sportybet_com_ng_api",
                                 "reason": "un-modeled (no `resources` in spec)" }] }
```

The listing's spec is a flat `apis` list with no `resources` grouping, so the
code generator has nothing to model and quarantines it. `parse list --json`
stays `[]` and `parse_apis/src/parse_apis/_manifest.json` stays `{}`. The
`sportybet_com_ng_api` client module is **not** generated and never will be via
this path. Consequence: there is no typed client, and `parse_apis/` contains
only its committed scaffold.

Fallback, and what to use: call the REST surface directly
(`X-API-Key` header, base `https://api.parse.bot`) rather than waiting on a
generated client. Docs: *"Prefer no SDK? Every API is also a plain REST
endpoint."*

**2. `parse init` did not create `parse_apis/.gitignore`.**

The SDK docs say `init` "appends a deny-by-default `.gitignore` block"; it did
not here. The block has been added by hand — without it, the generated payload
(which reveals your account's API inventory) would be committed. Commit the
scaffold, ignore the payload, and re-create it with `uv run parse sync`.

## Method notes

- Endpoints whose spec method is `GET` take **query-string** params; `POST`
  endpoints take a JSON body. The intro page's blanket "POST it" wording
  applies to POST endpoints only.
- `event_id` format is `sr:match:<digits>`.
- The `odds` field arrives as a **string** (`"2.36"`), not a number. Convert
  before arithmetic.
- Kickoff times are Unix **milliseconds**.
