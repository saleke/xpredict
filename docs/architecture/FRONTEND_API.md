# Xpredict backend integration contract for a replacement UI

Reviewed against the repository on **2026-10-06**. This describes the current
Python backend implementation, including the automatic nearest-fixture policy.
It is a source-based contract, not a claim that every provider, production
deployment or legacy feature has passed live validation. A running Python worker
must restart after a code update before it publishes the new response fields.

**Audience:** the coding agent building the new public UI and, if needed, its
operator console. Preserve the wire names below; normalize them in one client
adapter instead of guessing field names in individual components.

All JSON examples in this file are **illustrative**, with invented teams and
identifiers. They contain no production credentials, sessions or personal data.

## Contents

1. [Required UI-to-data connections](#1-required-ui-to-data-connections)
2. [Transport, sessions and data conventions](#2-transport-sessions-and-data-conventions)
3. [Public and account endpoint inventory](#3-public-and-account-endpoint-inventory)
4. [Match forecasts](#4-match-forecasts-get-apiforecast)
5. [Winning picks, micro markets and accumulators](#5-opportunity-board-get-apiopportunity-board)
6. [Dashboard and prediction history](#6-dashboard-get-apidashboard)
7. [Calendar and observed scores](#7-calendar-get-apidaily-board)
8. [Raw picks and settled ledger](#8-raw-picks-and-settled-ledger)
9. [Health and readiness](#9-health-and-readiness)
10. [Authentication, Telegram and activation](#10-authentication-telegram-and-activation)
11. [Tier catalog and historical backtest](#11-tier-catalog-and-historical-backtest)
12. [Operator console API](#12-operator-console-api)
13. [Server-only endpoints and legacy curation](#13-server-only-endpoints-and-legacy-curation)
14. [Client loading, polling and state handling](#14-client-loading-polling-and-state-handling)
15. [Missing capabilities and integration traps](#15-missing-capabilities-and-integration-traps)
16. [Completion checklist and source references](#16-completion-checklist-and-source-references)

## 1. Required UI-to-data connections

| UI area | Endpoint and exact response path | Required behavior |
|---|---|---|
| Upcoming match forecasts | `GET /api/forecast` → `matches` | Render fixture-level probabilities and uncertainty; earliest kickoff first. |
| Forecast count and horizon | Same → `count`, `window_hours`, `window_selection` | Explain the actual adaptive horizon; future fixtures are not necessarily today's fixtures. |
| Winning picks | `GET /api/opportunity-board` → `board.winning` | Same entitled curated selections as `board.earning`; best qualifying pick per match, earliest kickoff first. |
| Micro bets | Same → `board.micro_bets` | Render actual returned markets, lines, sides, probability and fair odds. Do not manufacture markets. |
| Value / earning opportunities | Same → `board.earning` | Respect `board.execution_locked`; distinguish locked, unpriced and no qualifying edge. |
| Accumulators | Same → `board.accumulators` | Render server-supplied legs, adjusted joint probability and warnings; currently research combinations. |
| Coverage / model evidence | Same → `board.coverage`, `model`, `providers`, `prices`, `price_match` | Show available, modelled and priced coverage separately. |
| Publication freshness | Same → `generated_at`, `published_at`, `cached`, `service`, `stale_reason` | A saved board can exist while service readiness is false. |
| Overview statistics | `GET /api/dashboard` → `summary`, `pipeline`, `live` | Honor nulls, sample limits and the difference between HTTP health and publication health. |
| Curated upcoming selection cards | Same → `active_picks`, `pick_feed` | One qualifying selection per match from the latest price-checked publication, earliest kickoff first. Respect server locks. |
| Predictions awaiting results | Same → `awaiting_results` | Keep past-kickoff predictions visible while confirmed results are missing. |
| Previous prediction results | `GET /api/ledger?limit=200` → `settled_ledger` | Show original probability/selection alongside result, actual score, settlement time and source. |
| Overview result preview | `GET /api/dashboard` → `settled_ledger` | Bounded recent sample; use the ledger endpoint for its explicit truncation metadata. |
| Reliability chart | Same → `calibration.bins` | Sample counts and missing values must remain visible. |
| Closing line value | Same → `clv` and `summary.mean_clv` | Missing closing data is not a zero CLV result. |
| Trap warnings | Same → `traps` | Render only observed advisories; this collection may legitimately be empty. |
| Default calendar | `GET /api/daily-board` → `board.all_upcoming` | Default to **Next Matches**, including recorded future dates beyond this week. |
| Today / Tomorrow / This Week | Same → `board.today`, `board.tomorrow`, `board.this_week` | Use the supplied product timezone and explicit date groups. |
| Observed scores and status | Calendar fixture → `score`, `status`, `completed`, `worker_observed_at` | Scores are observations, not minute-by-minute streaming guarantees. |
| Profile and account tier | `GET /api/auth/me` → `user` | Use server identity and tier; re-fetch after login, logout or activation. |
| Tier feature matrix | `GET /api/tiers` → `features`, `ladder`, `billing_available` | Availability flags override promotional catalog text. |
| Historical research | `GET /api/backtest` → `summary`, `strategies`, `records`, `meta` | Label as archive simulation, separate from live results and current-model validation. |
| Public status indicator | `GET /api/status` and `GET /api/ready` | A 503 readiness response does not invalidate a saved forecast. |
| Operator diagnostics | `GET /api/admin/operations`, `/providers`, `/overview`, `/health` | Separate operator authentication and role requirements apply. |

The browser does not request forecasts from football/odds providers directly.
It reads saved backend publications and observations. Provider credentials stay
on the server; public page loads do not trigger generation.

## 2. Transport, sessions and data conventions

### 2.1 Base URL and HTTP behavior

- Local launcher: usually `http://localhost:8080`; all routes below are relative
  to that origin. Make the API base configurable for a replacement development UI.
- Vercel: request the same `/api/...` paths. `/api/index` and its `__lisa_path`
  rewrite parameter are implementation details, not client-facing routes.
- JSON requests use `Content-Type: application/json`; JSON responses use
  `application/json; charset=utf-8`.
- Public JSON responses normally carry `Cache-Control: no-cache, no-store,
  must-revalidate`. Use `cache: 'no-store'` for client polling.
- JSON request bodies must be objects; the current size ceiling is **64 KiB**.
- There is **no uniform success envelope**. For example, dashboard has `summary`,
  forecast has `kind`, opportunity board has `success`, auth/me has
  `authenticated`, and ledger has `settled_ledger`.
- Inspect HTTP status, content type and payload. Unknown local GET routes can
  return an HTML 404 through the static handler; do not always assume JSON.
- Typical errors have `error`, optionally `success:false`, `detail`, `state` or
  `error_type`. Vercel can return `{error:'Service unavailable', error_type:...}`
  before the endpoint-specific handler runs.
- Expected refusal/status codes include 400, 401, 403, 404, 409, 410, 413, 429,
  500, 502 and 503. Keep a meaningful error state, not an empty success state.

### 2.2 Authentication and access rules

Public/account requests recognize the **`lisa_session`** HTTP-only cookie, or
`Authorization: Bearer <session_id>`. Cookies take precedence. Use browser
`credentials: 'include'`; do not publish or log session IDs.

Admin requests recognize the separate **`lisa_admin`** cookie, scoped to
`/api/admin`, or a Bearer session. An admin login alone does **not** establish the
public site's `lisa_session`. Conversely, a public login cookie alone is not the
admin cookie. See section 12 for CSRF and operator roles.

| Public resource | Guest / free | `tier1`, `tier2`, `tier3`, `admin` account tier |
|---|---|---|
| Match forecasts and derived probability detail | Public | Public |
| Winning opportunity rows | Entitled featured pick(s) only | Same entitled picks as the curated feed |
| Curated earning / pick feed | First pick; second after Telegram verification | Tier 1: up to five; Tier 2/3: full qualifying feed |
| Research accumulators | Empty list | Tier 1: empty; Tier 2/3: available combinations |
| Micro tables and `board.research` | Empty lists | Tier 1/2: empty; Tier 3: up to two qualifying alternatives per match |
| Settled ledger | Public; current handler has no tier mask | Same endpoint |

Proven operators receive the full view by default and may preview a tier using
the controls described below. For the temporary paper-verification server,
`pick_feed.paper_tiers_unlocked:true` means every caller receives the full Tier 3
read view, including guests. Display the explicit testing banner, suppress
upgrade prompts and preserve zero paper stakes. This requires both paper mode
and the server-side unlock flag; it does not update account subscriptions.
Normal operator preview uses
`?tier=free|tier1|tier2|tier3` without changing their account. Ordinary accounts
cannot upgrade through that query. Public
`?refresh=1` specifically requires `user.tier === 'admin'`.

### 2.3 Cross-origin integration

Prefer serving/proxying the new UI and API under the same origin. The current
CORS allowlist is server-configured through `LISA_ALLOWED_ORIGINS`; it defaults
to no additional browser origins. Allowing an origin does not change cookie
SameSite behavior.

**Existing limitation:** preflight advertises `GET, POST, OPTIONS` and does not
include `X-CSRF-Token` in allowed headers. Consequently a separately hosted admin
UI using PATCH/PUT/DELETE or CSRF headers needs same-origin proxying or an explicit
backend CORS change. Do not assume the existing CORS response supports it.

### 2.4 Numeric, timestamp and identity conventions

| Value | Wire format / meaning |
|---|---|
| Probability | Fraction in `[0,1]`: `.72` displays as `72%`. |
| Decimal odds | Total return multiplier: `2.50`. Model `fair_odds` is not an available bookmaker offer. |
| EV | Fraction of unit stake: `.06` displays as `+6%`; may be negative or null. |
| `stake_fraction` | Bankroll fraction: `.01` = `1%`. Current paper mode emits zero. |
| `recommended_stake_pct` | Already percentage points: `1.0` = `1%`; do not multiply by 100 again. |
| `recommended_units` | Recommendation units; current paper forecasts emit zero. |
| Match / publication timestamps | Timezone-aware ISO strings, generally UTC with `+00:00` or `Z`. Parse as instants. |
| Auth user times, session expiry, audit timestamp | Unix **seconds**, not JavaScript milliseconds. Convert with `new Date(value * 1000)`. |
| `timings_ms` | Milliseconds. |
| `age_sec`, `uptime_seconds`, job duration | Seconds. |
| `null` / absent numeric field | Unknown, unavailable or masked; display `n/a` or an explicit state, never zero by coercion. |
| Raw ledger boolean columns | May serialize as `0`/`1`; projected account/card fields may be booleans. Normalize explicitly. |
| `match_id` | Opaque provider-scoped identifier. Do not split or recreate it. |
| `dedupe_key` | Opaque identity of one immutable selection, including market, side and line. Use as ledger/card key. |
| Opportunity row key | No `dedupe_key` is supplied; key by `(match_id, market, selection, line)`. |
| Accumulator key | No ID is supplied; key by its ordered leg identities, not `description`. |

Join objects by exact `match_id` when possible. Alternate providers can have
different IDs for the same fixture; avoid speculative frontend name matching.
The backend performs bounded fixture reconciliation itself.

Naming differs across projections:

| Meaning | Forecast | Opportunity | Dashboard / raw ledger / calendar |
|---|---|---|---|
| Kickoff | `commence_at` | `kickoff` | `commence_time` |
| Teams | `home`, `away` | `home`, `away` | `home_team`, `away_team` |
| League | `sport_key` / `league` | `sport_key` | `sport_key` |
| Selection | Fixture probabilities | `selection` | `outcome_name` |
| Probability | `model.p_home/p_draw/p_away` | `p_model` | `p_true` |
| Expected value | Not normally supplied | `ev` | `best_ev` |

## 3. Public and account endpoint inventory

| Method | Route | Purpose / parameters |
|---|---|---|
| GET | `/api/dashboard` | Overview, saved cards, recent results, calibration, CLV, pipeline. Alias: `/api/data`. |
| GET | `/api/forecast` | Match-level bulletin; modern publication or legacy/empty fallback. |
| GET | `/api/opportunity-board` | Saved market selections, diagnostics, earning and accumulators. Optional `refresh=1|true|yes` requires admin-tier public session. |
| GET, POST | `/api/daily-board` | Saved calendar; no supported period/filter body or query. Select its returned arrays client-side. |
| GET | `/api/picks` | Same curated feed and lock projection as dashboard. Tier query is an operator preview only. |
| GET | `/api/ledger` | Recent settled raw rows. `limit`: default 200, clamped 1–1000; invalid values use 200. |
| GET | `/api/status` | Public health payload. Aliases: `/api/health`, `/api/telemetry`. |
| GET | `/api/ready` | Readiness object, HTTP 200 when ready and 503 otherwise. |
| GET | `/api/tiers` | Feature catalog and billing availability. |
| GET | `/api/backtest` | Packaged archive research, computed on demand and cached for 24 hours. |
| GET | `/api/auth/me` | Current public identity or `authenticated:false`. |
| POST | `/api/auth/signup` | `{email,password,display_name?}`; always creates free-tier account. |
| POST | `/api/auth/signin` | `{email,password}`; public session cookie. |
| POST | `/api/auth/signout` | Revoke public session; body optional. |
| POST | `/api/auth/link-telegram` | `{telegram_id,telegram_username?,token?}`; authenticated. |
| GET | `/api/verify-status` | `user_id` query → Telegram verification status. |
| POST | `/api/verify-token` | `{user_id,token}` → verification, not a paid-tier purchase. |
| POST | `/api/activate-tier` | `{key}` → redeem an existing one-time payment key; requires account session. |
| POST | `/api/auth/update-tier` | Privileged tier mutation; **not self-service checkout**. Alias: `/api/auth/tier`. |
| POST | `/api/curated-picks` | Current unlocked curated picks for the caller's tier; read-only. |

Public routes currently do not provide server-side league/date/market filtering,
search, arbitrary sorting or pagination beyond ledger `limit`. Filter the returned
snapshot locally. Do not invent query parameters and assume they work.

## 4. Match forecasts: `GET /api/forecast`

### 4.1 Current generation publication

This is the preferred source for match cards, 1X2 probability bars, expected goals
and per-match model detail. It is separate from actionable market selections.

| Top-level field | Type | Meaning |
|---|---|---|
| `kind` | string | `match_forecast_bulletin`. |
| `mode` | string | `live_model` for the current independent goal model. |
| `generated_at` | ISO string | Generation start time. |
| `day` | string | Publication date in product timezone after worker publication. |
| `window_hours` | number, optional on older publications | Effective look-ahead. |
| `window_selection` | object, optional on older publications | Adaptive horizon decision; see below. |
| `count` | integer | Number of returned upcoming match forecasts after past kickoffs are filtered. |
| `matches` | `ForecastMatch[]` | Chronological fixture forecasts. |
| `disclaimer` | string | Research / unpriced forecast explanation. |
| `service` | readiness object, optional on fallback | Current producer readiness, independent of the saved forecast content. |

`window_selection` fields: `mode:'nearest_upcoming'`, `configured_hours`,
`effective_hours`, `expanded` (boolean), `available_verified`,
`available_modelled`, `next_kickoff` (ISO or null), `next_forecast_kickoff`
(ISO or null), `volume_target`.

The configured horizon defaults to 24 hours. If forecastable fixtures fall short
of the volume target (default 12), the worker extends to the nearest eligible
fixture needed for that volume, rounded up to an hour. Equal kickoffs remain
together. When fewer fixtures exist it uses what is available. It only uses
verified future fixtures with trained teams; it does not fabricate dates or
forecast unknown teams. The horizon may exceed 48 hours or seven days.

### 4.2 Forecast match fields

| Field | Type | UI interpretation |
|---|---|---|
| `match_id` | string | Fixture identity. |
| `home`, `away` | string | Team labels; render as text. |
| `sport_key`, `league` | string | Canonical league, e.g. `soccer_epl`. |
| `commence_at` | ISO string | Kickoff, used for primary sorting/countdown. |
| `market` | null in current model publication | No bookmaker consensus attached to this projection. |
| `model.ready` | boolean | Fit sufficiency indicator at generation; not approval to stake. |
| `model.p_home`, `p_draw`, `p_away` | numbers | Independent 1X2 probabilities. |
| `micro.double_chance` | object | Probability map, usually `1X`, `X2`, `12`. |
| `micro.home_team_over`, `away_team_over` | object | Goal-line string → over probability, e.g. `"1.5":0.46`; under is its complement for these binary lines. |
| `micro.p_btts` | number | Probability of BTTS Yes. |
| `micro.p_over_2_5` | number or null | Over 2.5 probability. |
| `micro.expected_goals_home`, `expected_goals_away` | numbers | Predicted mean goals; not externally supplied observed xG. |
| `micro.most_likely_scores` | array | Current publication uses `{score:'2-1',p:0.12}`, up to five scorelines. |
| `uncertainty.level` | string | Current model path uses `medium` or `high`; preserve future values. |
| `uncertainty.reasons` | array | Current path: strings. Legacy fallback: objects containing `label`/`key`. Accept both. |

Forecasts sort by kickoff, then highest outright probability
`max(p_home,p_draw,p_away)`, then match ID. Winning market picks have their own
market-aware ranking; the forecast card's outright lean is not necessarily the
winning ladder's strongest derived selection.

```json
{
  "kind": "match_forecast_bulletin",
  "mode": "live_model",
  "generated_at": "2026-10-04T08:00:00+00:00",
  "day": "2026-10-04",
  "window_hours": 96,
  "window_selection": {
    "mode": "nearest_upcoming", "configured_hours": 24,
    "effective_hours": 96, "expanded": true,
    "available_verified": 1, "available_modelled": 1,
    "next_kickoff": "2026-10-08T08:00:00+00:00",
    "next_forecast_kickoff": "2026-10-08T08:00:00+00:00", "volume_target": 12
  },
  "count": 1,
  "matches": [{
    "match_id": "example:fixture-1", "home": "Example Home", "away": "Example Away",
    "sport_key": "soccer_epl", "league": "soccer_epl",
    "commence_at": "2026-10-08T08:00:00+00:00", "market": null,
    "model": {"ready": true, "p_home": 0.55, "p_draw": 0.25, "p_away": 0.2},
    "micro": {
      "double_chance": {"1X": 0.8, "X2": 0.45, "12": 0.75},
      "home_team_over": {"1.5": 0.46}, "away_team_over": {"0.5": 0.58},
      "p_btts": 0.52, "p_over_2_5": 0.48,
      "expected_goals_home": 1.55, "expected_goals_away": 0.9,
      "most_likely_scores": [{"score": "1-0", "p": 0.14}]
    },
    "uncertainty": {"level": "medium", "reasons": ["Independent model forecast; not a bookmaker consensus"]}
  }],
  "disclaimer": "Illustrative research forecast; unpriced selections have no measured EV."
}
```

### 4.3 Empty and legacy fallback

Without a saved model publication the handler can read an existing odds cache
and produce a legacy bulletin, or return HTTP 200 with `mode:'no_live_data'`,
`count:0`, `matches:[]`, `day:null`, null `top_pick/best_win/best_earning`,
zero `marquee_count/high_uncertainty_count/in_play_count`, `in_play:[]` and a
`disclaimer`. Do not require a `success` property on forecast responses.

Legacy bulletins may include `top_pick`, `best_win`, `best_earning`, `marquee`,
`is_top_pick`, `market`, `markets`, `micro_markets`, `movement`, `locks`, `in_play`
and `in_play_count`. These are **optional compatibility fields**, not promises
from the modern independent model. Legacy `market` rows can contain
`market,line,top_outcome,p_top,fair_odds,cv,n_books,best_odds,best_book,ev`.
Legacy scoreline rows can use `home_goals,away_goals,p` instead of `score,p`.
Keep the new UI chronological even if a legacy marquee flag exists.

## 5. Opportunity board: `GET /api/opportunity-board`

This is the authoritative endpoint for the **winning ladder, micro picks,
earning opportunities and research accumulators**. Do not rebuild it by
multiplying probabilities or prices in the browser.

### 5.1 Envelope and diagnostics

| Field | Type | Meaning |
|---|---|---|
| `success` | boolean | Whether a board was built. |
| `health` | string | Feed result: `ok`, `partial`, `degraded`, `unproven`, `broken`. |
| `generated_at`, `published_at` | ISO strings | Cycle start versus committed publication time. |
| `window_hours`, `window_selection` | number / object | Same effective forecast horizon decision as section 4. |
| `summary` | string | Feed summary, suitable for diagnostics. |
| `paper_mode` | boolean | Paper run; execution stakes are zero. |
| `model` | object | Fit diagnostics, including league reports and optional corner sample counts. |
| `providers` | array | Source status rows described below. |
| `prices` | object | Quote collection diagnostics, not an array of prices. |
| `price_match` | object | Quote-to-fixture matching counts. |
| `notes`, `errors` | string arrays | Operational decisions versus failures. |
| `timings_ms` | object | Optional `calendar`, `history_and_model`, `prices`, `board`, `total`. |
| `board` | object | Market collections and coverage below. |
| `cached` | boolean | Current handler sets true because this is a saved publication. |
| `service` | readiness object | Current producer status. |
| `stale_reason` | string, optional | Supplied when current readiness is false; retain the saved board with a freshness warning. |

Before the first publication: HTTP **503**, with `success:false`,
`state:'starting'`, `board:null`, `error`. Once a saved board exists it can return
HTTP **200** while `service.ready:false`, because a saved forecast survives a
provider outage. Past-kickoff rows/accumulator legs are filtered on read.

Price eligibility is also checked on publication and read. Expired, suspended,
withdrawn or changed native Scalper offers lose `best_odds`, `ev` and stake;
earning rows are removed while model forecasts remain available. Priced rows
carry `price_valid_until`, `price_book_key`, `price_source_event_id` and
`price_quote_identity`; execution metadata is masked for free accounts.

The returned `price_readiness` includes `ready`, `state`, `observed_at`,
`fixtures_priced`, per-state `selections` counts, `fresh_earning_selections`,
`opportunity_state`, `collection_state`, `failed_sources` and
`accumulators_without_current_leg_prices`. Its scope is
`published_model_candidates` when the full research population is retained;
older publications use `published_selections`.
`service.price_ready` is separate from worker/publication `service.ready`.
Coverage retains `fixtures_priced_at_generation` and projects current
`fixtures_priced`; it does not claim complete bookmaker inventory. Multi-source
price/matching diagnostics retain individual entries under `sources` and
aggregate quote counts and unique matched fixtures.

`board` fields: `generated_at`, `window_hours`, `unproven`, `model`, `winning`,
`earning`, `micro_bets`, `accumulators`, `coverage`, `notes`, and optional
`execution_locked`. Current paper publication forces `board.unproven:true`;
this is not identical to the sample-size indicator `model.sufficient`.
Use `model.sufficient:false` for a deficient-training warning; do not infer
short training history from `board.unproven:true`. Paper mode alone also forces
that advisory flag. Display returned `matches_used`/`mean_games_behind` when
explaining training coverage rather than substituting a fixed sample count.

Coverage fields: `window_hours`, `fixtures_seen`, `fixtures_modelled`,
`fixtures_priced`, `meets_volume_target`, `volume_target`, `leagues` (league →
count), `sources` (source → count), `notes`.

Provider status fields: `name`, `configured`, `used`, `leagues` (strings),
`fixtures` (count), `error` (string), `degraded` (boolean), `tier` (source category).
A provider row is metadata, not proof that its credentials or every market work.
Provider failures include safe HTTP classifications such as
`authentication_rejected` (401), `http_access_denied` (403), and
`quota_or_rate_limited` (429). API-Football can instead report
`daily_budget_exhausted` or `history_budget_exhausted` when the application's
local allowance refuses a request before contacting the provider. That is not
an invalid-key diagnosis; non-settlement requests retain the settlement reserve.
Deduplicate diagnostics/notes when consuming older saved publications.

A not-ready service with provider errors can still have just published a new
forecast board. `stale_reason` alone does not prove the displayed board is an
older successful cycle. Use publication/attempt timestamps and `age_sec` versus
`stale_after_sec` to explain actual age staleness, and show each failure once.

Model fit fields: `matches_used`, `teams`, `objective`, `iterations`, `converged`,
`mean_games_behind`, `data_sufficiency`, `sufficient`, `prior_dominance`, `rho`.
The envelope model may add `training_fingerprint`, `leagues` (per-league fit plus
`base_mu/home_adv`) and `corner_matches` (league → real corner sample count).

Price snapshot fields: `source`, `quotes` (integer **count**), `rows`, `pages`,
`truncated`, `stopped_because`, `dropped` (reason → count), `error`,
`quota` (null or `{remaining,limit}`), and `sources` (provider name → snapshot).
Top-level snapshot values reflect the last processed source, not necessarily an
aggregate of all sources. Use `prices.sources` for source-by-source diagnostics.
These objects can be empty on a failed/unavailable collection.

`price_match` fields: `matched_events`, `matched_fixtures`, `unmatched_events`,
`ambiguous_events`, `contested_fixtures`. This also reflects the last matching
pass rather than a guaranteed multi-provider sum.

### 5.2 Opportunity row: all fields

The same row schema appears in `winning`, `earning`, `micro_bets` and accumulator
`legs`. These fields do not use dashboard card names.

| Field | Type | Meaning |
|---|---|---|
| `match_id`, `sport_key` | strings | Fixture and league identity. |
| `kickoff` | ISO string | Primary listing order. |
| `home`, `away` | strings | Teams. |
| `market` | string | Market family; see section 5.3. |
| `selection` | string | Side/result. |
| `line` | number or null | Exact total/handicap line; zero is a valid line. |
| `p_model` | number | Model selection probability; split/refund contracts also expose payout distribution. |
| `fair_odds` | number | Model break-even decimal odds. |
| `best_odds` | number or null | Best observed quote; guest/free view masks it. |
| `best_book`, `best_source` | string or null | Bookmaker identifier and data source; can be masked/missing. |
| `ev` | number or null | Expected net return per unit staked; unknown for unpriced/masked rows. |
| `stake_fraction` | number | Bankroll fraction; zero in paper mode or when no execution approval. |
| `priced` | boolean | Quote comparison exists in this view. Guest mask sets false. |
| `basis` | string | Usually `model_only` or `model_vs_market`. |
| `reason` | string | Explanation or access/validation limitation. |
| `payout_probabilities` | object or null | Grade → probability: `WIN`, `HALF_WIN`, `VOID`, `HALF_LOSS`, `LOSS` as applicable. |

```json
{
  "match_id": "example:fixture-1", "sport_key": "soccer_epl",
  "kickoff": "2026-10-08T08:00:00+00:00",
  "home": "Example Home", "away": "Example Away",
  "market": "double_chance", "selection": "1X", "line": null,
  "p_model": 0.8, "fair_odds": 1.25,
  "best_odds": null, "best_book": null, "best_source": null, "ev": null,
  "stake_fraction": 0, "priced": false, "basis": "model_only",
  "reason": "Model forecast; observed execution prices require a paid account",
  "payout_probabilities": null
}
```

### 5.3 Market families currently produced

| `market` | Selection examples | Line and requirements |
|---|---|---|
| `h2h` | `Home`, `Draw`, `Away` | null; full-time 1X2. |
| `double_chance` | `1X`, `X2`, `12` | null. |
| `draw_no_bet` | `Home`, `Away` | null; draw refunds stake. |
| `totals` | `Over`, `Under` | Goal line, e.g. 2.5; integer/quarter lines need payout-aware rendering. |
| `home_team_totals`, `away_team_totals` | `Over`, `Under` | Team goal line. |
| `btts` | `Yes`, `No` | null. |
| `correct_score` | `0-0`, `2-1` | null; probabilities often lower than other markets. |
| `asian_handicap` | `Home`, `Away` | Requires an offered supported handicap line. |
| `corners` | `Over`, `Under` | Match total corners; requires sufficient real local corner-count history. |

Current contracts are regulation/full-time markets. There is no `period` field
on these opportunity rows and no implemented first-half/player/cards/live-event
contract to infer from them. `spreads` can exist in legacy ledger/odds projections;
do not automatically relabel every spread as a current Asian handicap.

For refund/split markets, `p_model` is not enough to price the bet. Use the
supplied `fair_odds`, `ev` and `payout_probabilities`; do not calculate binary
`p_model * odds - 1` for a quarter line or draw-no-bet contract.

HTTP `winning` and `earning` project the same entitled curated feed from the
private candidate pool before internal ladder caps: one currently priced,
qualifying selection per match, with no default count cap. A configured positive
`LISA_PICK_FEED_LIMIT` optionally caps the publication; zero means unlimited and
API metadata reports `limit:null`. Probability and price value determine the
best market per match, quality rank, optional cap and tier access. Returned
arrays display earliest kickoff first, with quality breaking equal-kickoff ties.
Derived market candidates require
observed offers. Every returned opportunity must clear the shared quality floor;
unpriced, low-probability and poor-value outcomes stay out of user-facing pick
lists. See [the selection policy](../product/PICK_FEED.md).

### 5.4 Accumulator: all fields

| Field | Type | Meaning |
|---|---|---|
| `size` | integer | Number of legs; current sizes 2–5. |
| `legs` | `Opportunity[]` | Server-selected distinct-fixture legs. |
| `description` | string | Human-readable summary; render as text. |
| `p_naive` | number | Product before correlation adjustment. |
| `p_adjusted` | number | Adjusted joint probability; headline probability to display. |
| `correlation_penalty` | number | Multiplicative adjustment; `.90` means a 10% reduction. |
| `fair_odds` | number | Theoretical break-even combination price. |
| `best_odds`, `best_book`, `ev` | nullable | Current research builder does not establish a bookmaker combined offer. |
| `stake_fraction` | number | Currently zero. |
| `priced` | boolean | Currently false for research combinations. |
| `warnings` | string array | Shared-league/day dependence and no confirmed parlay offer. |

Maximum **12** published accumulators, displayed by the earliest leg's kickoff,
then adjusted joint probability. Legs are also displayed earliest kickoff first.
Each leg must meet the shared single-pick acceptance rules; adjusted probability
must also meet `LISA_PICK_FEED_MIN_ACCUMULATOR_PROBABILITY` (default 35%). These are research
combinations, **not executable bookmaker slips**. Do not synthesize booking
codes, multiply best prices from different books into an executable offer, or
combine multiple same-match selections as an independently priced accumulator.

### 5.5 Paid masking and refresh

The same server-enforced ladder applies to dashboard, picks and the opportunity
board. Free receives one full featured pick, two after Telegram verification;
Tier 1 receives up to five; Tier 2/3 receive all qualifying headlines. Winning
and earning lists expose only the caller's entitled picks. Tier 2/3 receive
qualifying accumulator analysis. Only Tier 3 receives `micro_bets` and `research`:
these contain the same bounded qualifying alternatives, up to two per match
from different market families, excluding the headline family. Render that
alternative table once. `research_access` reports availability, required tier,
`candidate_count` (qualifying alternatives), `evaluated_candidates` (private pool
size), `qualifying_candidates`, and `max_alternatives_per_match`. Rejected
calculations remain private at every tier. No tier pads its quota.

An empty earning/accumulator array **plus** `execution_locked:true` means access
is restricted. An unlocked empty earning array may mean missing prices, no safe
fixture match, stale quotes or no qualifying edge. Inspect diagnostics rather
than claiming no model forecast exists.

`GET /api/opportunity-board?refresh=1` queues a worker refresh and returns the
currently saved board. It is not a synchronous generation response. On a
serverless deployment, a queued refresh still needs a scheduled job dispatch;
owner `/api/admin/jobs/generation` is the finite-job control (section 12).

## 6. Dashboard: `GET /api/dashboard`

Alias `/api/data`. HTTP 200 response has the following top-level keys; backend
read/build failure is HTTP 503 with `error` and `error_type`.

| Object | Exact fields / purpose |
|---|---|
| `meta` | `generated_at` (response time), `sports_scope`, `data_provenance:{synthetic,source,statement}`. |
| `summary` | `active_picks_count`, `pending_journal_count`, `awaiting_results_count`, `settled_picks_count`, `total_picks_count`, `total_matches_evaluated`, `won_count`, `lost_count`, `win_rate`, `brier_score`, `ece`, `mean_clv`, `positive_clv_share`. |
| `live` | `state:'live'|'stale'|'never'`, `observed_at`, `age_sec`, `sports_observed`, `matches_observed`, `credits_remaining`, `quota_state:'unknown'|'exhausted'|'constrained'|'ok'`, `last_error`. |
| `pipeline` | `state`, `has_errors`, `published_at`, `generated_at`, `window_hours`, `fixtures_modelled`, `fixtures_priced`, `upcoming_selections`, `selected_matches`, `background_candidates`, `paper_mode`. |
| `active_picks` | Latest curated selection projection, at most **50** distinct matches by default, including safe locked teasers. |
| `pick_feed` | Policy, thresholds, candidate/qualifying/selected counts, rejection reasons, tier and unlocked count. |
| `awaiting_results` | Past-kickoff saved model card projection awaiting official results, at most **200** by default. |
| `settled_ledger` | Recent raw settled rows, at most **200** by default; additionally derives `pnl` for qualifying quoted recommendations. |
| `traps` | At most 10 advisories: `home_team,away_team,public_favorite,cv,fair_odds,public_odds,detected_at`. |
| `calibration` | Object or null; field list below. |
| `clv` | Object or null; field list below. |

Summary journal counts can exceed the lengths of these capped arrays.
`active_picks_count` is the curated card count; `pending_journal_count` separately
includes original predictions awaiting results. `pipeline.upcoming_selections`
and `selected_matches` describe the displayed feed. `total_matches_evaluated` refers to the latest observation, not an
all-time audit total. These endpoints do not provide lifetime profit/ROI charts.

The `live.state` age cutoff is 300 seconds in this projection; it describes the
last saved observation, not a guaranteed live score or bookmaker feed.
`live.credits_remaining` can be null with modern providers; their per-source
quota diagnostics are elsewhere. Readiness uses a separate configured cutoff.

### 6.1 Saved card fields

`model_forecast`, `source`, `basis`, `is_recommendation`, `actual_score`,
`dedupe_key`, `match_id`, `sport_key`, `home_team`, `away_team`, `commence_time`,
`market`, `outcome_name`, `line`, `p_true`, `fair_odds`, `best_book`, `best_odds`,
`best_ev`, `n_books`, `stdev`, `cv`, `conviction_score`, `recommended_stake_pct`,
`recommended_units`, `freshness`, `badge_color`, `gauge_text`.

Curated cards additionally carry `rank`, `selection_score`, `selection_reason`,
current price proof, `is_locked` and `tier_level`. `rank` is the quality rank used
for access; display earliest kickoff first, using rank only for equal kickoffs.
`pick_feed.display_order` is `kickoff_asc`. Locked rows only expose fixture metadata, rank and
required access; market, side, line, probability, fair price and quote identities
are removed. Local tier changes must never unmask a server-locked card.

`freshness` values: `UNPRICED`, `FRESH`, `FAIR`, `DECAYED`. They are derived from
entry price versus fair price, **not bookmaker quote age**. They are not a stake
approval flag. Preserve `model_forecast`, `basis`, `is_recommendation` to distinguish
research from actual recommendations. Model source identifiers currently include
`dixon-coles-v1`, `league-dixon-coles-v2`, `league-dixon-coles-v3`.

`quotes`, when present, contains `home_team`, `away_team`, `sport_key`, `margin`,
`n_books`, `books`. Each book has `book_key`, `book_title`, `prices` (outcome →
decimal odds) and `margin` (fraction or null). Books are capped at four; `n_books`
can be larger. This quote explainer uses a legacy odds cache and can be null even
when a modern opportunity has a price. It has no exposed bookmaker update time.

### 6.2 Calibration and CLV

Calibration fields: `total_evaluated`, `n_won`, `n_lost`, `n_void`, `win_rate`,
`pred_mean`, `brier_score`, `ece`, `mce`, `log_loss`, `reliability`, `resolution`,
`uncertainty`, `bins`. Bin fields: `lower`, `upper`, `count`, `pred_mean`,
`win_rate`, `bias`, `error`. Empty bins have null statistics, not measured zeros.
Metrics use the recent settled sample and eligible binary contracts, not every
goal line or every historical prediction.

CLV fields: `count`, `mean_clv`, `median_clv`, `positive_clv_share`, `min_clv`,
`max_clv`, `win_rate_positive_clv`, `win_rate_negative_clv`. Fractions display as
percentages. A count of zero with null metrics means no recorded closes.

## 7. Calendar: `GET /api/daily-board`

POST is also routed to the same read handler; prefer GET. There is no backend
`tab` parameter. The response supplies all date groups:

- `success`: boolean; errors use HTTP 503.
- `state`: `observed` if a saved worker timestamp exists, otherwise `starting`.
- `board`: `{today:[], tomorrow:[], this_week:[], all_upcoming:[]}`.
- `timezone`: product IANA timezone, normally `Africa/Lagos`.
- `worker_observed_at`: latest saved worker observation time, or null.
- `provenance`: `sources` (strings), `synthetic:false`, `provider_requests:0`,
  `truncated` (boolean), `detail` (freshness explanation).

Each calendar fixture has **exactly this projection**:

```json
{
  "match_id": "example:fixture-1", "sport_key": "soccer_epl",
  "home_team": "Example Home", "away_team": "Example Away",
  "commence_time": "2026-10-08T08:00:00+00:00",
  "status": "SCHEDULED", "completed": false, "score": null,
  "source": "football_data", "worker_observed_at": "2026-10-04T08:00:00+00:00"
}
```

`score` is null or `[homeGoals,awayGoals]` with actual nonnegative integers.
Do not turn null into `[0,0]`. `status` is source-normalized text, e.g.
`SCHEDULED`, `IN_PLAY`, `FINISHED`, `POSTPONED`, `CANCELLED`; accept unknown values.

Today/Tomorrow/This Week may include in-play, completed or postponed observations.
**This Week means today plus the next six local calendar dates**, not an ISO
Monday–Sunday week and not exactly 168 hours. `all_upcoming` excludes started,
completed, canceled/postponed and other noneligible statuses, and can include
saved dates beyond that week. A verified fixture can appear here before the
model knows both teams; a calendar entry is not proof a prediction was generated.

Calendar snapshots are capped at 1,500 observations before merging. Preserve
`provenance.truncated`. Newer settlement observations can update a score without
a generation cycle. Worker observation time can reflect cached source data; it
is not the source's guaranteed update time. There is no score push stream.

## 8. Raw picks and settled ledger

### 8.1 `GET /api/picks`

Response: `active_picks` (curated cards plus safe locked teasers), `count`,
`user_id`, `is_telegram_verified`, `tier`, `is_authenticated`, `pick_feed`.

Authenticated user identity replaces a supplied `user_id`. Normal accounts use
their stored tier. Anonymous `?tier=tier3` does not unlock access. Proven
operators can request a tier preview. All rows use the same access ladder as
dashboard; model forecast rows no longer bypass locks. Future kickoff, current
offer eligibility and quality thresholds are required. The journal remains a
separate settlement/audit resource. Preserve exact market/side/line identity.

### 8.2 `GET /api/ledger?limit=200`

Response fields: `settled_ledger`, `count` (returned), `total` (all settled),
`truncated`, `limit`. Newest settlement first. Default limit 200; valid range
1–1000. No public offset/page/date filter exists; do not create a fake pagination
control when the backend cannot return subsequent slices.

The handler currently returns an empty array if its storage read throws; that
particular endpoint cannot distinguish a read failure from an empty ledger.
Use dashboard/health/operations diagnostics when an unexpectedly empty history
is reported. Dashboard failures, in contrast, return 503.

### 8.3 Raw ledger row: complete current column list

| Field | Type / meaning |
|---|---|
| `dedupe_key`, `match_id`, `sport_key`, `market`, `outcome_name` | Selection identity and labels. |
| `line` | number or null. |
| `home_team`, `away_team`, `commence_time` | Teams and original kickoff; may be null for legacy rows. |
| `p_true`, `fair_odds`, `n_books` | Saved entry probability/fair price/book count; an unpriced model row may have `n_books:0`. |
| `stdev`, `cv` | Nullable dispersion measurements. |
| `state` | Ledger state, including `PENDING_SETTLEMENT`, `TRIGGER_ALERT`, `CONFIRMED`, `SETTLED`, `VOID`, `SETTLED_VOID` and possible legacy states. |
| `result` | null pending; otherwise `WIN`, `LOSS`, `VOID`, `HALF_WIN`, `HALF_LOSS`. |
| `best_book`, `best_odds`, `best_ev` | Nullable entry execution fields. |
| `closing_odds`, `closing_p_true`, `clv` | Nullable captured close metrics; missing close is not an inferred one. |
| `conviction_score` | Legacy score, often zero for model forecasts; do not label as calibrated certainty. |
| `recommended_stake_pct`, `recommended_units` | Saved recommendation amounts; paper forecasts zero. |
| `created_at`, `settled_at` | ISO entry and settlement times; settlement nullable while pending. |
| `source`, `basis`, `model_version` | Model/provenance identifiers. |
| `is_recommendation` | boolean or 0/1. |
| `actual_score` | Nullable text, e.g. `2-1`; do not confuse with predicted correct-score selection. |
| `result_source` | Nullable source of the confirmed outcome. |

Raw pick masking can additionally add `is_locked`, `tier_level`,
`execution_locked`, `gauge_text`, `booking_codes`, `deep_links`; the latter maps
are legacy compatibility/access fields, not current executable booking offers.
`pnl` is **not guaranteed** on `/api/ledger`. Dashboard's settled projection
computes it only for a quoted recommendation as net return per flat unit;
zero-stake forecasts get null. Never display research wins as earned money.

Selection probabilities and entry prices are immutable once recorded. A newer
forecast can differ from a saved prediction for that same fixture. Show the
saved values when evaluating previous predictions; do not overwrite them with
the latest opportunity-board probability.

## 9. Health and readiness

`GET /api/status`, `/api/health`, `/api/telemetry` return the same HTTP 200
envelope, even when its `status` is `degraded`:

- `status`: `healthy` or `degraded`.
- `daily_service`: readiness projection below.
- `settlement_service`: saved settlement status or null.
- `version`, `uptime_seconds`, `storage_driver`, `paper_mode`.
- `ledger_counts`: `total`, `pending`, `settled`, `won`, `lost`, `void`.
- `telegram_bot`: `configured`, `bot_username`, `channel_configured`.
- `odds_feed`: `configured`, `key_present`, `enabled`, `base_url`, `sports_count`.

`odds_feed` is a legacy odds-feed configuration view, not a complete list of all
modern providers. A false legacy `enabled` does not prove the independent
calendar/model worker is disabled. Use admin providers/operations for that.

`GET /api/ready` returns the `daily_service` object directly and HTTP **200** if
`ready:true`, **503** otherwise. Fields can include `last_attempt`,
`last_finished`, `last_success`, `error`, `predictions_added`, `settled`,
`settlement_ready` (boolean or null), `state`, `age_sec` (number or null),
`stale_after_sec`, `ready`. Derived states are `starting`, `ready`, `stale`,
`paused`; when no service is attached a minimal `{ready:false,state:'disabled'}`
can be returned.

Saved generation job state (e.g. `pipeline.state`) is distinct from derived
readiness state. `last_success` can exist for a published board with price
failures; it does not mean all sources succeeded. Settlement status typically
has `last_attempt`, `settled`, `pending_leagues`, `error`, `state`; it does not
consistently supply `last_finished`. Do not require nonexistent completion fields.

Forecasts can continue during odds failures while earning opportunities remain
empty. Keep that distinction visible. Polling a health endpoint does not start
provider jobs or prove profit performance.

## 10. Authentication, Telegram and activation

### 10.1 Product account lifecycle

`GET /api/auth/me` is the authority for the current public identity:

```json
{
  "authenticated": true,
  "user": {
    "id": "example-account-id",
    "email": "reader@example.invalid",
    "display_name": "Example Reader",
    "tier": "free",
    "telegram_id": null,
    "telegram_username": null,
    "telegram_verified": false,
    "created_at": 1791072000,
    "last_login_at": 1791072000,
    "is_operator": false,
    "role": null
  },
  "session_id": "illustrative-session-do-not-log"
}
```

Anonymous access returns HTTP 200 with `{authenticated:false,user:null}`.
Roles currently resolve to `owner`, `admin` or null. Tier values are `free`,
`tier1`, `tier2`, `tier3`, `admin`. Session identifiers are credentials even
though some responses include them; use the HTTP-only cookie in a browser.

| Request | JSON body | Success | Expected refusals |
|---|---|---|---|
| `POST /api/auth/signup` | `email`, `password`, optional `display_name` | 201 `{success:true,user,session_id}` and `lisa_session` cookie | 400 invalid/duplicate account or invalid payload; 500 unavailable auth/registration failure. |
| `POST /api/auth/signin` | `email`, `password` | 200 `{success:true,user,session_id}` and cookie | 401 incorrect credentials; 429 after five failed attempts in 15 minutes; 500 unavailable auth. |
| `POST /api/auth/signout` | Empty object acceptable | 200 `{success:true,message}`; revokes session and expires cookie | Treat transport failures as failures to confirm logout. |

Signup always creates **free** accounts; a client-supplied `tier` cannot buy or
grant access. Registration validates email and requires at least eight password
characters. Signup/signin user objects may contain additional safe account
fields such as `updated_at`, but do not include the role annotations that
auth/me adds. Re-fetch auth/me after either operation.

The product cookie currently uses `HttpOnly; SameSite=Lax; Path=/`, with a
30-day Max-Age. The public cookie helper currently does not add `Secure`;
the admin helper has separate behavior. Do not copy assumptions between them.

### 10.2 Telegram connection and proof

`POST /api/auth/link-telegram` requires a public login. Body:

```json
{
  "telegram_id": "123456789",
  "telegram_username": "example_reader",
  "token": "optional-proof-token"
}
```

Only `telegram_id` is required. Keep IDs as strings. Success is
`{success:true,user,telegram_verified,message}`. A saved connection without
proof can return `telegram_verified:false`; connection and verification are
different states. Backend proof can come from the bot verification registry,
an accepted unlock token or a configured channel-membership check.

Refusals: 401 missing login; 400 missing ID; 403 invalid supplied proof or
mismatched verified identity; 429 verification/link attempt limit. Invalid
explicit proof does not silently create a verified connection. Telegram
verification alone does not upgrade the paid account tier.

Other verification endpoints:

| Endpoint | Input | Response |
|---|---|---|
| `GET /api/verify-status?user_id=...` | Backend account/user ID | 200 `{user_id,verified}`; false when no proof exists. |
| `POST /api/verify-token` | `{user_id,token}` | 200 `{success:true,user_id,verified:true}`; 400 missing fields, 403 invalid token, 429 excessive attempts. |

Use the authenticated backend `user.id` for bot linking; a random ID generated
only in localStorage does not identify the backend account. Where the bot is
configured, its verification deep link is
`https://t.me/<bot_username>?start=verify_<user.id>`. Read the bot username and
availability from `/api/status` → `telegram_bot`; do not hardcode a working bot.

### 10.3 Tier activation and privileged tier updates

`POST /api/activate-tier` accepts `{key}` and requires a logged-in account.
Success: `{success:true,tier,message}`. Expected errors: 400 missing key,
401 missing login, 404 unknown key, 410 used/expired key, 403 key linked to a
different Telegram identity, 500 failure to update the account. Activation keys
currently have a one-hour validity window. Keys must already have been issued
by the backend's provisioning flow; the browser cannot create one.

After activation, re-fetch auth/me and every response whose execution fields
depend on tier. Do not retain a previous guest response as evidence that the
upgrade failed, or a previous paid response after signout.

`POST /api/auth/update-tier` and `/api/auth/tier` are privileged compatibility
routes, not customer upgrade buttons. Body includes `tier` and optionally
`user_id`; accepted target tiers are `free`, `tier1`, `tier2`, `tier3`.
Authorization requires an allowed privileged identity or server webhook/admin
secret. Response: `{success:true,user,tier,message}`. Use the owner-only admin
user route for an operator UI; never embed a webhook secret in frontend code.

The current payment/activation provisioning includes process-local state.
Do not assume activation keys survive process replacement or independently
initialized serverless instances. There is no public checkout-creation endpoint.

## 11. Tier catalog and historical backtest

### 11.1 `GET /api/tiers`

| Field | Type / content |
|---|---|
| `billing_available` | Boolean; currently false. Disable purchase/checkout flows. |
| `features` | Array of `{key,available,label,blurb,upgrade_hint,grant,reveal_minutes}`. |
| `tiers` | Map of tier ID to display label: free and three paid tiers. |
| `ladder` | Map keyed by tier; each value has `unlocked` and `new_vs_previous` feature-key arrays. |

Only `bulletin`, `micro_pack`, `parlay`, `curated_feed`, `full_feed`, and
`research_markets` currently have `available:true` in
this catalog. Other catalog entries describe unavailable features. The exported
`reveal_minutes` maps are currently empty; do not implement timed early-access
promises from promotional descriptions.

Basic match probability analysis is public. Opportunity-board accumulator
analysis starts at Tier 2, and qualifying alternatives start at Tier 3.
Actual endpoint entitlement checks remain authoritative.
The backend does not supply subscription prices here. Prices hardcoded in the
old UI are not a backend pricing contract.

### 11.2 `GET /api/backtest`

This is a **packaged historical archive replay**, computed on demand and cached
for 24 hours. Load it when the research view opens; do not poll it every twenty
seconds. Its first uncached computation can be slower than a saved-board read.

Top-level fields:

| Field | Meaning |
|---|---|
| `summary` | Historical simulation metrics listed below. |
| `sport_breakdown` | Sport/league-keyed metric map, including matches, wins, losses, profit and wagered values. |
| `calibration` | Calibration report or null; treat its bins as research data. |
| `records` | Grade A executed records and a bounded Grade C advisory sample. |
| `strategies` | Map of strategy ID to `{summary,records}`. |
| `strategy_comparison_matrix` | Array of strategy summaries. |
| `data_provenance` | Archive/source metadata; preserve source/note and available coverage fields. |
| `records_trimmed` | `{note,grade_a,grade_c_displayed,grade_c_total}`. |
| `meta` | `{provenance:'packaged_historical_archive',is_live:false,statement,computed_in_sec}`. |

The complete `summary` field set is:

```text
total_matches, executed_bets, wins, losses, pushes, win_rate,
wilson_ci_lower, wilson_ci_upper,
grade_a_count, grade_a_wins, grade_a_win_rate,
grade_b_count, grade_b_wins, grade_b_win_rate,
grade_c_traps_avoided, grade_c_traps_that_lost, grade_c_traps_that_won,
capital_preserved_dollars, net_counterfactual_value,
initial_bankroll, ending_bankroll, total_wagered, net_profit,
roi_pct, flat_profit, flat_roi_pct,
max_drawdown_pct, max_drawdown_dollars,
sharpe_ratio, sortino_ratio, profit_factor,
brier_score, reliability, resolution, uncertainty, ece, mce, log_loss,
positive_clv_rate, mean_clv
```

Win rates and CLV rates are fractions. Fields ending `_pct` are already
percentage values; e.g. `roi_pct:5.2` means 5.2%, not 520%.

Each record contains:

```text
match_id, sport_key, home_team, away_team, commence_time,
grade, market, outcome_name, p_true, fair_odds,
best_book, best_odds, ev, conviction_score,
stake_units, stake_amount, actual_score, result, pnl, capital_saved,
hazard_warning, closing_odds, beat_clv
```

Results can include `PASS_TRAP_AVOIDED` and `PASS_ADVISORY`; those are passed
opportunities, not executed winning bets. Research grades include `GRADE_A`
and `GRADE_C`. These dollar/unit simulation fields are separate from current
paper forecasts and their zero stake recommendations.

Strategy IDs are `conservative`, `high_yield_pivots`, `smart_parlays`,
`hybrid_portfolio`, `always_home`. Each strategy summary has:

```text
strategy_id, name, badge, description, total_matches, executed_bets,
wins, losses, pushes, win_rate, wilson_ci_lower, wilson_ci_upper,
avg_odds, total_wagered, net_profit, roi_pct,
max_drawdown_pct, max_drawdown_dollars, sharpe_ratio, sortino_ratio,
profit_factor, risk_level, best_for
```

The web response returns all Grade A records plus at most 500 Grade C rows.
Non-conservative strategy `records` arrays are intentionally empty; summaries
still exist. Do not interpret this trim as zero strategy activity.

This route evaluates the legacy archive/consensus implementation. It is not
forward validation or profit approval for the current league prediction model.
Unavailability returns HTTP 503 with `{error:'backtest unavailable',detail}`.

## 12. Operator console API

Implement this section only if the replacement includes an operator console.
Keep operator-only diagnostics and secrets out of public views.

### 12.1 Session, roles and writes

1. `GET /api/admin/session` restores a console session. Anonymous response:
   `{authenticated:false,role:null,csrf_token:null}`.
2. `POST /api/admin/login` accepts `{email,password,ttl_seconds?}`. It requires
   an existing operator account. Success returns
   `{success:true,role,csrf_token,expires_at,user}` and sets `lisa_admin`.
3. Protected reads use that cookie with `credentials:'include'`.
4. Every protected mutation, including logout, requires
   `X-CSRF-Token: <csrf_token>` plus the session. The backend also checks origin.
5. Dangerous settlement writes additionally require JSON `confirm:true`.

The authenticated session response includes
`{authenticated:true,role,csrf_token,expires_at,user}`; `user` has `id`, `email`,
`display_name`, `tier`, `telegram_id`. Login's user projection omits Telegram
ID. Expiry is Unix seconds. Requested session TTL is clamped to 300–28,800
seconds. The cookie is HttpOnly, SameSite=Strict, scoped to `/api/admin`; it
adds Secure when the server's HTTPS configuration/detection requires it.

Role is re-evaluated on each request. Current roles are `owner` and `admin`;
owners are explicitly email-allowlisted. Do not infer owner from paid tier or
from an operator's display name. Admin login attempts are limited to five per
15 minutes; protected writes have a 240-per-minute per-identity limit.

Common console errors are `{success:false,error,detail}` (detail optional or
null): 401 absent/expired operator session; 403 role, origin or CSRF refusal;
400 invalid body/confirmation; 409 conflict; 429 rate limit; 503 unavailable
capability/read model; 500 unexpected backend failure. Hide owner-only buttons
for admins but retain server-error handling: a role can change mid-session.

### 12.2 Complete route inventory

“Operator” means owner or admin. All writes below require CSRF except login.

| Method | Path | Access | Request / response purpose |
|---|---|---|---|
| GET | `/api/admin/session` | Public session probe | Session state and CSRF token. |
| POST | `/api/admin/login` | Operator credentials | Email/password login. |
| POST | `/api/admin/logout` | Operator | Revoke console session; `{success:true}`. |
| GET | `/api/admin/overview` | Operator | Configuration, runtime, counts and legacy pool/scheduler overview. |
| GET | `/api/admin/health` | Operator | Named checks, not the public health envelope. |
| GET | `/api/admin/keys` | Operator | Masked legacy odds key-pool state. |
| GET | `/api/admin/providers` | Operator | Modern providers, quotas/coverage plans and credential availability. |
| PUT | `/api/admin/providers/{provider}` | Owner | Replace, disable or inherit one provider credential. |
| POST | `/api/admin/keys/cooldown` | Owner | Temporary legacy pool-key cooldown. |
| GET | `/api/admin/settings` | Operator | Typed editable field definitions and effective settings. |
| PATCH | `/api/admin/settings` | Owner | Persist validated runtime overrides. |
| POST | `/api/admin/settings/reset` | Owner | Remove specified overrides or all overrides. |
| GET | `/api/admin/sports` | Operator | Legacy sports list, markets, regions and cache counts. |
| PUT | `/api/admin/sports` | Owner | Update legacy sports scope. |
| GET | `/api/admin/picks` | Operator | Paginated raw ledger search and statistics. |
| GET | `/api/admin/picks/stats` | Operator | Ledger statistics. |
| GET | `/api/admin/picks/{key}` | Operator | One raw ledger pick by dedupe key. |
| POST | `/api/admin/picks/{key}/settle` | Owner + confirm | Manually grade one pending pick. |
| POST | `/api/admin/picks/settle-match` | Owner + confirm | Apply one manual grade to pending rows for a match. |
| GET | `/api/admin/notifications` | Operator | Paginated notification queue and status counts. |
| POST | `/api/admin/notifications/retry` | Owner | Requeue failed notifications. |
| POST | `/api/admin/telegram/test` | Operator | Explicitly send a test message. |
| GET | `/api/admin/users` | Operator | Paginated safe account projection. |
| PATCH | `/api/admin/users/{id}` | Owner | Update account tier. |
| GET | `/api/admin/audit` | Operator | Paginated mutation audit. |
| GET | `/api/admin/cache` | Operator | Live/stale cache counts and bytes. |
| POST | `/api/admin/cache/purge` | Owner | Prune expired cache entries. |
| GET | `/api/admin/database` | Operator | Driver-specific database diagnostics. |
| GET | `/api/admin/telemetry` | Operator | Internal telemetry with secret-shaped values redacted. |
| GET | `/api/admin/forecast` | Operator | Legacy bulletin projection; see warning below. |
| POST | `/api/admin/system/pause` | Owner | Pause/resume forecast generation. |
| POST | `/api/admin/system/poll` | Owner | Request a generation cycle/legacy poll. |
| GET | `/api/admin/operations` | Operator | Paper-pilot, measured timings and fixture observations. |
| POST | `/api/admin/jobs/{job}` | Owner | Serverless generation, settlement or history job. |

### 12.3 Overview and diagnostic read models

`GET /api/admin/overview` returns:

- `version`, `role`, `pid`, `uptime_seconds`, `started_at`, `paused`.
- `stats` (pick statistics), `ledger_counts`.
- `scheduler`: presence plus attached scheduler/readiness fields.
- `odds_pool`: presence and legacy pool fields when attached.
- `settings`: `sports_count`, `sports`, `markets`, `regions`,
  `cadence_prematch_sec`, `cadence_live_sec`, `cadence_settle_sec`,
  `credit_budget_daily`, `forecast_horizon_hours`, `forecast_min_matches`,
  `forecast_max_matches`, `enable_inplay`, `enable_extra_markets`,
  `enable_micro_predictions`, `require_positive_ev`, `inplay_max_leagues`.
- `settings_revision`, `overrides`.
- `capabilities`: `telegram_configured`, `odds_keys`, `storage_driver`,
  `scheduler_attached`, `runtime_config`.

The modern daily pipeline need not attach the old odds pool. A missing pool is
not proof that modern provider credentials are missing.

`GET /api/admin/health` returns
`{healthy,checks:[{name,ok,detail}],uptime_seconds,checked_at}`. Check names
include `database`, `odds_credits`, `odds_snapshot`, `ledger`, `scheduler`.
Its database check uses legacy SQLite-oriented fields; PostgreSQL diagnostics
have a different shape and can produce a misleading failed check. Show the
reported detail alongside `/database` and `/operations`; do not invent an
all-clear status in the UI.

`GET /api/admin/database` returns `{database}`. SQLite fields can include
`path`, `exists`, `bytes`, `bytes_on_disk`, `sidecars`, `page_count`, `page_size`,
`journal_mode`, `integrity`, `tables`, `row_counts`, `error`. PostgreSQL fields
include `database`, `bytes`, `version`, `driver`, `schema`, `pool`. Treat driver
diagnostics as a tagged/optional shape, not a required SQLite schema. Never
move this operational metadata into public client responses.

`GET /api/admin/telemetry` returns
`{telemetry:[{key,value,updated_at,redacted?}]}`. Values can be arbitrary JSON.
Secret-shaped telemetry keys have redacted values. This is an operator
inspection surface; it is not a stable substitute for the product endpoints.

### 12.4 Providers and credential replacement

`GET /api/admin/providers` returns:

```text
providers: [{provider, configured, configuration_source}]
coverage_plan
settlement_coverage
oddspapi_status
the_odds_api_status
credential_updates_allowed
```

`configuration_source` is `admin` or `environment`. Supported credential IDs:
`oddspapi`, `oddspapi_rapidapi`, `api_football`, `allsports`, `football_data`,
`sharpapi`, `the_odds_api`. Openfootball/OpenLigaDB do not require a credential
entry. `coverage_plan` can include `updated_at`, `requested_leagues`, `plans`,
`calendar_routes`, `calendar_strategy`; provider-specific plans/statuses include
available cached, quota, rotation and coverage observations. Keep these flexible
and display only fields actually returned. Plans are not guaranteed live quota
balances from every upstream provider.

Owner-only `PUT /api/admin/providers/{provider}` accepts one of:

```json
{"operation":"replace","credential":"example-placeholder-only"}
```

```json
{"operation":"disable"}
```

```json
{"operation":"inherit"}
```

Replace is the default operation. A replacement must be a nonempty string,
at most 4,096 characters and without control characters. Disable stores an
empty override that blocks the environment credential. Inherit removes the
override and uses the environment credential again. Response:
`{success:true,provider,operation,detail}`. A change applies to the next worker
cycle; an in-flight request may still use the old key.

This is the connection for the “change provider key without editing source”
control. Use a write-only password input, clear it on success, and never store
it in localStorage, logs, analytics or example fixtures. GET does not reveal
the saved key. Replacement does not reset a provider account's quota, change
its subscription limits or guarantee access to blocked endpoints.

`GET /api/admin/keys` is a separate **legacy odds pool** view. If absent it
returns `configured:false`, `keys:[]`, `note`. If present, fields include
`configured`, `credits_remaining`, `any_exhausted`, `active_index`, `keys`,
`note`. A key row can contain `index`, `label` (masked), `remaining`, `used`,
`spent_today`, `budget_day`, `requests_today`, `budget_daily`, `requests`,
`errors`, `last_used_at`, `disabled_until`, `cooldown_until`, `cooldown_active`.

`POST /api/admin/keys/cooldown` accepts `{label,cooldown_seconds?}`; default
900 seconds, clamped to 60–86,400. `index` and `minutes` are compatibility
inputs. Success: `{success:true,label,cooldown_until}`. This cools a pool entry;
it does not replace a modern provider credential or replenish credits.

### 12.5 Runtime settings and sports scope

`GET /api/admin/settings` returns `fields`, `overrides`, `revision`,
`secrets_present`, `readonly_fields`, `scope_leagues`, `effective`. Each field
definition has `name`, `type`, `value`, `default`, `overridden`, and applicable
`min`, `max`, `choices`. Render the supplied type/constraints, rather than
hardcoding every form field. `default` means the base environment setting.
`secrets_present` contains availability booleans, not credentials.

Owner PATCH example:

```json
{
  "settings": {
    "board_window_hours": 24,
    "board_volume_target": 12
  }
}
```

The wrapper must contain a nonempty settings object; validation applies to the
whole update, with at most 60 fields. Response:
`{success:true,overrides,revision,effective}`. Reset accepts `{fields:[...]}`
or omit `fields` to remove all overrides; returns the same response shape.
Read-only values, including secrets and paper-mode restrictions, are not runtime
form fields. Use the provider credential endpoint for secrets.

The `effective` summary is partly legacy and does not contain every modern
board setting. Read actual `fields` values for `board_leagues`,
`board_window_hours`, `board_volume_target`, `daily_interval_sec`. The initial
window is a baseline; the nearest-fixture policy can expand the final horizon.

`GET /api/admin/sports` returns `configured`, `markets`, `regions`, `cost_note`,
`leagues:[{key,configured,events_cached}]`. Owner PUT accepts
`{sports:["soccer_example"]}` with a nonempty list of at most 60 keys; success
is `{success:true,sports,revision}`. This changes legacy `sports` scope.
The current model/calendar league selection uses **`board_leagues`** instead;
do not wire the two controls as if they were interchangeable.

### 12.6 Pick search, statistics and manual grading

`GET /api/admin/picks` query fields: `state`, `market`, `sport`, `outcome`,
`q` (alias `search`), `page`, `page_size`, `order`. Page defaults to 1 and
page size to 50, with a server-enforced cap. Allowed orders include
`created_at`, `created_at_asc`, `commence_time`, `settled_at`, `best_ev`,
`best_ev_asc`, `n_books`, `p_true`; default is newest creation first.
Response: `{rows,total,page,page_size,pages,stats}`. Rows use the raw pick schema
in section 8. Do not confuse this paginated admin route with public `/api/picks`.

`GET /api/admin/picks/stats` returns `{stats}` with these statistical fields:

```text
graded, wins, losses, voids, pending, awaiting_settlement,
hit_rate, void_rate,
quoted_recommendations_settled, flat_stake_profit_units, flat_stake_roi,
avg_p_true, avg_best_ev, avg_odds, avg_books, avg_clv,
by_market, by_sport, calibration
```

`by_market` rows: `{market,n,wins,hit_rate,avg_p,avg_ev}`.
`by_sport` rows: `{sport_key,n,wins,hit_rate,avg_p}`.
Calibration rows: `{bucket,p_low,p_high,n,wins,realised,claimed}`.
Binary wins/losses do not include all half-result grades; do not derive a new
hit rate by dividing binary wins by every graded row.

`GET /api/admin/picks/{key}` returns `{pick}`. Here `key` is the ledger
**dedupe key**, not match ID. **Existing path issue:** the admin router matches
path segments without URL-decoding captured parameters. Encoded dedupe keys
containing special characters can therefore fail to resolve. Do not claim this
detail/manual-settlement feature works for arbitrary keys without correcting
and testing the backend's path normalization.

Manual settlement inputs:

- `POST /api/admin/picks/{key}/settle`:
  `{result:'WIN'|'LOSS'|'VOID',confirm:true}` →
  `{success:true,dedupe_key,result}`; 409 if already settled.
- `POST /api/admin/picks/settle-match`:
  `{match_id,result:'WIN'|'LOSS'|'VOID',confirm:true}` →
  `{success:true,rows_settled,result}`; 404 if no pending rows. Result defaults
  to WIN in current code, so **always send an explicit operator-selected value**.

These are operator overrides. Bulk match settlement applies the same grade to
every affected pick and does not evaluate different market outcomes from a
score. The normal score-based settlement worker is the product's default.

### 12.7 Notifications, users, audit and cache

| Endpoint | Input | Response / fields |
|---|---|---|
| `GET /api/admin/notifications` | `status` (`PENDING`, `SENT`, `FAILED`), `page`, `page_size` | Paginated rows and `counts`; row: `id`, `dedupe_key`, `status`, `attempts`, `last_error`, `created_at`, `sent_at`, `text` (trimmed to 400 characters). |
| `POST /api/admin/notifications/retry` | Optional `limit`, default 100, clamp 1–500 | `{success:true,requeued,note}`. Requeue is not delivery confirmation. |
| `POST /api/admin/telegram/test` | Optional `chat_id`, `message` | `{success:true,chat_id,detail}`; defaults to configured chat/test message; message maximum 900 characters. 503 missing bot, 400 missing target, 502 rejection. |
| `GET /api/admin/users` | `q`/`search`, `page`, `page_size` | Paginated rows: `id`, `email`, `display_name`, `tier`, `telegram_id`, `telegram_username`, `telegram_verified`, `created_at`, `updated_at`, `last_login_at`, `is_operator`; no password material. |
| `PATCH /api/admin/users/{id}` | `{tier:'free'|'tier1'|'tier2'|'tier3'|'admin'}` | `{success:true,user_id,tier}`; 404 unknown account, 409 protection against removing the last operator. |
| `GET /api/admin/audit` | `admin_id`, `page`, `page_size` | Paginated rows: `id`, `admin_id`, `action`, `target`, `details`, `timestamp`. Details may be stored JSON text; timestamp is Unix seconds. |
| `GET /api/admin/cache` | None | `{cache:{entries,live,stale,bytes,pruned,by_prefix}}`; each prefix has count/bytes/live/stale. Read also prunes expired entries. |
| `POST /api/admin/cache/purge` | Empty object | `{success:true,cache,note}`; expires/prunes stale entries, not a promise to delete current publications. |

Sending a Telegram test is an explicit user action, never an automatic effect
when an admin page mounts. Notification retry still requires a configured,
running delivery mechanism to send the requeued messages.

### 12.8 Operations, jobs, pause and poll

`GET /api/admin/operations` returns:

```text
pilot
performance
generation_fixtures
settlement_fixtures
scheduler: "worker" | "serverless"
```

`performance` has `observed_at`, `timings_ms` (keys `calendar`,
`history_and_model`, `prices`, `board`, `total` where measured), `fixtures`,
`results`. These are observed worker measurements,
not continuous browser throughput measurements.

Each fixture-observation block has `worker_observed_at`, `rows`,
`provisional_rows_withheld`, `truncated`, `detail`. Rows contain `source`,
`match_id`, `league`, `home`, `away`, `kickoff`, `status`, `completed`, `score`.

`pilot` fields include:

```text
schema_version, checked_at, storage_driver, paper_mode, window_days,
daily_publications, jobs, settlement_counts, running_cycles,
unfinished_old_cycles, missing_completed_days, overdue_unsettled_forecasts,
stake_violations, odds_observations, blockers, operational_state,
profitability_approved, limitations
```

Daily publication rows report `day`, `publication_present`, optional
`generated_at`, `forecasts`, `winning_picks`, `earning_picks`, `micro_bets`,
`accumulators`. Job summaries include `job`, `cycles`, `failures`,
`last_finished`, `longest_sec`. Settlement counts group by `market`
and `result`, with `picks`; odds observations include `source`, `observations`,
`latest`. Current `operational_state` is `blocked` or `observed` and
`profitability_approved` is false. A later successful run does not automatically
erase older failures within the rolling pilot window.

Owner-only `POST /api/admin/jobs/{job}` supports `generation`, `settlement`,
`history` when the **serverless** job context is attached. A local worker does
not expose that capability through this route and returns 400. The job observes
its usual cadence/locking; results can be `not_due` or `another_worker`.
Executed responses include `state`, `job`, `failed`, `paper_mode` and applicable
`predictions_added`, `settled`, `observations`, `statistics_requests`,
`error_type`. Nonexecuted responses can be smaller. This admin route can return
HTTP 200 with `failed:true`; inspect the payload, not just HTTP success.

`POST /api/admin/system/pause` accepts `{paused:true}` or `{paused:false}` and
returns `{success:true,paused,note}`. Send actual JSON booleans. Forecast pause
does not imply settlement and history processing have also stopped.

`POST /api/admin/system/poll` accepts `{what:'cycle'|'settlement',mode:'due'|'sync'}`.
For the modern local DailyService it requests a refresh and returns
`{success:true,queued:true,note}`; it does not synchronously wait for a new
publication or independently select a settlement-only cycle. Legacy scheduler
responses can use `queued`/`mode` or a synchronous summary with `ran_cycle`,
`ran_settlement`, `skipped_reason`, `errors`, `cycle_reports`. On Vercel, use
the serverless job route for actual job execution, not an assumed background
thread. Poll a saved publication afterward to observe a changed generation time.

**Legacy forecast warning:** `GET /api/admin/forecast` returns `{board}` from
the old bulletin or `{board:null,reason:'no live odds snapshot yet'}`. It does
not return the modern DailyService forecast/opportunity board. For the current
model's admin preview, connect the normal public endpoints and establish any
required public session/tier separately.

## 13. Server-only endpoints and legacy curation

### 13.1 Scheduled jobs on Vercel

These routes are for authenticated schedulers and server-side operators, not
ordinary frontend requests:

| Method | Route | Purpose |
|---|---|---|
| GET or POST | `/api/cron/generation` | Fetch/assemble a new saved prediction publication. |
| GET or POST | `/api/cron/settlement` | Fetch confirmed outcomes and grade pending predictions. |
| GET or POST | `/api/cron/history` | Backfill supporting match history/statistics. |

They require `Authorization: Bearer <CRON_SECRET>` in a production Vercel
environment. The configured secret must have at least 32 characters. Never
bundle it in a UI. Expected refusals include 401 bad/missing authorization,
403 non-production environment, 503 unavailable secret, 405 unsupported method,
404 unknown job. An executed failing job returns HTTP 503; a healthy job can
also return a nonexecuted `not_due`/`another_worker` result.

Jobs use persisted cadence and locking. Generation/history have one-hour
dispatch gates and settlement a fifteen-minute gate, but those gates do not
schedule invocations themselves. Deployment cron/external scheduler frequency
controls the actual update frequency. A Vercel cold start or visitor GET is
not a replacement for an authenticated schedule. The frontend only observes
saved job/publication times and reports missing/stale work.

### 13.2 Payment webhook aliases

`POST /api/v1/webhook/payment`, `/api/webhook/payment`,
`/api/webhook/stripe` are server integration routes. They require the configured
high-entropy webhook/admin secret through the accepted custom header or body
secret. They are not customer-facing upgrade endpoints. The Stripe-named alias
does **not** implement native Stripe-Signature verification by itself.

Depending on the accepted event, responses have `success:true` and can have
`action:'PROVISIONED'`
with `email`, `tier`, `invite_link`, `unlock_code`, `audit_id`;
`action:'DOWNGRADED'` with `email`, `tier`, `audit_id`; or `action:'IGNORED'`
with event information. Do not render these server-to-server provisioning
responses as a public purchase API. `/api/tiers` currently reports billing
unavailable.

### 13.3 Curated picks

`POST /api/curated-picks` returns
`{success,tier,picks:[...],count,pick_feed}`. `picks` contains only the caller's
unlocked current curated cards. It no longer returns every tier's old cached
curation. The handler is read-only and uses the same price and entitlement
projection as dashboard/picks. An empty selection is a valid result.

## 14. Client loading, polling and state handling

### 14.1 Use one transport adapter

Preserve endpoint-specific envelopes. The following minimal helper also keeps
503 bodies available for readiness/starting-state rendering:

```javascript
async function readApi(path, { signal, timeoutMs = 12000 } = {}) {
  const controller = new AbortController();
  const forwardAbort = () => controller.abort();
  if (signal?.aborted) forwardAbort();
  else signal?.addEventListener('abort', forwardAbort, { once: true });
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      credentials: 'include',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      signal: controller.signal
    });
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) {
      throw new Error(`Unexpected API response (${response.status})`);
    }
    return {
      ok: response.ok,
      status: response.status,
      data: await response.json()
    };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', forwardAbort);
  }
}
```

For mutations, add method, serialized JSON body, Content-Type and, for admin
writes, X-CSRF-Token. Treat aborted/failed requests separately from valid empty
responses. Never pass provider keys as browser headers or query parameters.

Create explicit adapters for forecast, opportunity, calendar and saved-card
schemas. Map them to a shared view model only after retaining the original
IDs, market, selection, line, kickoff, source, probability and uncertainty.
Avoid converting null prices or null scores to zero in those adapters.

### 14.2 Initial loading and subsequent refresh

Independent first reads can run concurrently with `Promise.allSettled`:

```javascript
const responses = await Promise.allSettled([
  readApi('/api/auth/me'),
  readApi('/api/forecast'),
  readApi('/api/opportunity-board'),
  readApi('/api/dashboard'),
  readApi('/api/daily-board'),
  readApi('/api/tiers')
]);
```

Do not let a failed profile, unavailable price board or empty calendar prevent
successful forecasts/results from rendering. Each resource needs its own
loading, error, empty and last-success state. Auth-dependent resources may need
a second read after a newly established session.

The current UI refreshes its main saved data about every **20 seconds**. This
is a sensible starting interval for replacement UI reads, not a promise of
20-second upstream data updates. Poll the active view; reduce polling while
hidden. Use one in-flight read per resource, cancel on unmount, and avoid
installing duplicate intervals when switching tabs. Apply bounded retry/backoff
after network failures and retain the last successful payload with its age.

Suggested connections:

- Forecast/opportunity/overview views: refresh the corresponding saved feeds;
  compare `generated_at` before replacing stable groups.
- Calendar: refresh while visible; use returned date groups and observed times.
- Results: refresh `/api/ledger?limit=200` while visible, or when dashboard
  settlement counts change. Its result cap is not pagination.
- Auth: restore once; refresh after login, logout, link, verification or tier
  activation. Invalidate tier-dependent resource caches at the same time.
- Tier catalog: load once per session or explicit configuration refresh.
- Backtest: lazy-load on research navigation; use its backend cache and allow a
  longer timeout for the first computation. Do not put it on the main poll timer.
- Admin: restore `/api/admin/session` first, then request the selected console
  resources. Read the updated revision/job state after a successful write.

Never retain unlocked execution fields after logout merely because a previous
paid response is still in memory. Clear/refetch them. A selected pricing tab
or a client `tier` parameter is not entitlement.

### 14.3 State matrix

| Backend state | UI behavior |
|---|---|
| First start, forecast `count:0` / `no_live_data` | Show waiting-for-first-publication state; show calendar data separately if present. |
| Opportunity response 503 with no saved board | Show unavailable/starting state, not an invented empty valid board. |
| Saved data present, readiness stale/degraded | Render saved data with publication age and status; allow independent feeds to remain visible. |
| Today empty, `all_upcoming` nonempty | Default to Next Matches; expose nearest recorded kickoff and retain date filters. |
| Calendar fixture has no forecast | Render fixture/status; indicate model coverage missing rather than fabricating probability. |
| `execution_locked:true` | Keep public probability data; mark execution/value/accumulator access as locked. |
| Unlocked but `earning:[]` | Explain that no priced, validated qualifying opportunities are currently returned; use coverage/status to distinguish causes. |
| `best_odds:null` or no quote | Render model fair odds separately; show available bookmaker price as unknown. |
| Paper mode / zero stake | Label paper/research; do not compute expected cash earnings from zero-stake forecasts. |
| Kickoff passed, no confirmed result | Move saved prediction to awaiting-results presentation; do not assume win/loss or discard its history. |
| Settled quarter/half result | Display `HALF_WIN`, `HALF_LOSS`, `VOID` accurately alongside actual score and source. |
| Partial provider failures | Show scoped coverage/freshness warning while keeping successful predictions and observations. |
| Auth 401 / admin CSRF 403 | Restore session or request login as appropriate; do not retry mutations blindly. |

Default listing order is **earliest kickoff first**. Curated picks use quality
rank to break equal-kickoff ties; forecasts use strongest outright probability.
Honor server order or use an equivalent stable
sort. Do not promote a later “hero” match above the nearest fixtures because it
has a high probability. Calendar's supplied timezone groups may differ from
the browser/device timezone; label that distinction explicitly.

Group micro picks by fixture and market, retaining side and numeric line.
For example, total goals over 2.5 and over 3.5 are different selections, and
team totals need the team identity. Accumulator leg order and leg IDs must come
from the backend; do not generate more parlays by multiplying arbitrary UI rows.

When showing previous results, join only exact match IDs where shared. Different
providers can identify the same real-world fixture differently. Do not fuzzy-join
team labels/dates in the browser or overwrite saved prediction inputs with
latest estimates.

## 15. Missing capabilities and integration traps

The new UI must distinguish features with returned data from requested future
features. The current backend does **not** expose a complete product API for:

- Minute-by-minute event betting, possession/event streams, next-event markets
  or event-triggered live micro selections.
- Executable same-game accumulators, bookmaker booking-code creation, bet
  placement or guaranteed combined bookmaker payouts.
- Player props, player statistics/lineups, cards markets, first-half markets,
  or team corner handicaps.
- A normalized public match-detail endpoint with minute clock, injuries,
  suspensions, weather, referee, lineup or player-specific model inputs.
- Public quote-change history, every quote's current fetch timestamp, or a
  guarantee that a stored best price remains executable now.
- A public date-indexed archive of every past forecast/opportunity publication.
  The public previous-prediction view is the settled ledger; admin pilot rows
  provide publication counts, not all historical public boards.
- Public all-history pagination or server-side ledger date/market filtering.
- A functioning public checkout/purchase API or a persisted, production-ready
  activation-key workflow across independent instances.

Corner totals are conditional on real corner-history/model and price coverage;
the presence of a corner model module does not guarantee picks in every saved
board. Expected goals in forecasts are **model estimates**, not an exposed
observational xG feed. Internal provider payloads are not automatically public
API contracts.

Specific current inconsistencies to account for:

1. Forecast/board cards/calendar/raw ledger use different field names and
   envelopes; one guessed universal match shape will lose data.
2. Operator role and public account tier are separate, and their cookies differ.
3. Catalog promotional text can exceed implemented availability/access rules.
4. Admin forecast, sports and legacy key-pool endpoints are not the modern board
   pipeline's equivalents.
5. Existing CORS preflight is insufficient for a separate-origin admin console.
6. Encoded admin dedupe-key paths are not normalized by the current router.
7. A public ledger storage-read failure can return an empty result rather than
   an explicit storage error; correlate with health/diagnostics before presenting
   that as a confirmed absence of history.
8. HTTP 200 does not guarantee readiness, provider success, job execution or
   a fresh quoted price. Masking and absent prices are meaningful states.
9. Old saved publications can lack new optional fields such as
   `window_selection`; tolerate absence until a restarted worker republishes.
10. Nearest-match selection covers verified collected fixtures and model scope.
    It does not prove the displayed fixture is the world's next football match.
11. Legacy admin database health checks can misclassify PostgreSQL diagnostics.
12. Archive profit metrics and paper-pilot operational status do not certify
    live prediction profitability. `profitability_approved:false` must remain
    visible where the UI describes validation.

Do not fill these gaps with sample scores, synthetic edges, static records,
random confidence, made-up book offers or inactive controls presented as
working features. Render honest empty/unavailable states or omit unsupported
controls until the backend capability exists.

## 16. Completion checklist and source references

### 16.1 Replacement UI acceptance checklist

- [ ] Forecast rows bind real kickoff, league, teams, probabilities, uncertainty
  and provenance, and preserve nearest-first ordering.
- [ ] Horizon/count labels use the actual response; they do not label all future
  rows as today's picks or impose a new fixed 24/48-hour cutoff.
- [ ] Winning, earning, micro and accumulator sections bind their distinct
  board arrays and have distinct locked/empty/error states.
- [ ] Every supported returned micro market renders side, line, probability,
  fair odds and observed execution fields without conflating them.
- [ ] Accumulators show every backend leg, adjusted probability and research
  warnings; no executable booking promise is invented.
- [ ] Calendar defaults to all upcoming, retains supplied date tabs/timezone,
  and shows observed scores/status without claiming minute-by-minute coverage.
- [ ] Saved active, awaiting and settled predictions are accessible; previous
  results show original inputs, actual outcome, source and settlement time.
- [ ] Statistical charts retain sample sizes/nulls, and paper rows do not become
  cash profit claims.
- [ ] Auth restores through the server and refreshes all masked resources on
  account/tier changes, including signout.
- [ ] Tier/billing and Telegram controls reflect actual server availability.
- [ ] Archive research is labeled separate from current live-model results.
- [ ] Partial failures do not blank successful independent feeds; stale data
  remains visibly stale rather than silently appearing current.
- [ ] Polling is cleaned up on unmount and does not fetch/backfill providers or
  repeatedly run archive computations from ordinary page navigation.
- [ ] If an admin console is included, every route in section 12 is deliberately
  connected, omitted, or marked unavailable; owner checks and CSRF work.
- [ ] Provider credential replacement is write-only and no secret appears in
  shipped assets, localStorage, URLs, analytics or console logs.
- [ ] Browser verification covers guest/free, paid, owner/admin, expired session,
  logout, first-start, empty-today/future-fixtures and partial-provider states.

### 16.2 Authoritative source map

These repository files define the current contract. If implementation changes,
update this guide from the handler/projection rather than from old UI text.

| Source | What it defines |
|---|---|
| [server.py](../../engine/lisa/server.py) | Public/account routing, JSON/cookies, access masking, verification, activation and webhooks. |
| [serverless.py](../../engine/lisa/serverless.py) | Vercel routing, production cron authorization, persisted scheduled-job execution. |
| [daily_service.py](../../engine/lisa/daily_service.py) | Saved publications, readiness, forecast/calendar reads and worker refresh. |
| [feed.py](../../engine/lisa/feed.py) | Fixture verification, adaptive horizon, forecast payload and provider evidence. |
| [board.py](../../engine/lisa/board.py) | Opportunity rows, market eligibility, pricing, accumulator construction and ranking. |
| [calendar_snapshot.py](../../engine/lisa/calendar_snapshot.py) | Calendar/observed-score projections, local date groups and all-upcoming scope. |
| [dashboard.py](../../engine/lisa/dashboard.py) | Summary, projected cards, bounded active/awaiting/result lists and live-state fields. |
| [storage.py](../../engine/lisa/storage.py) / [postgres_storage.py](../../engine/lisa/postgres_storage.py) | Ledger/account persistence and driver-specific admin read models. |
| [auth.py](../../engine/lisa/auth.py) | Safe account projection, session validation and Telegram connection state. |
| [admin_api.py](../../engine/lisa/admin_api.py) | Full operator route table, owner restrictions, CSRF and console payloads. |
| [runtime.py](../../engine/lisa/runtime.py) | Runtime-editable field definitions, validation and effective overrides. |
| [provider_credentials.py](../../engine/lisa/provider_credentials.py) | Credential provider IDs, redacted inventory and replace/disable/inherit semantics. |
| [pilot.py](../../engine/lisa/pilot.py) / [observability.py](../../engine/lisa/observability.py) | Paper-pilot status, job/publication evidence, worker timings and fixture observations. |
| [tiers.py](../../engine/lisa/tiers.py) | Feature catalog availability and tier ladder. |
| [backtest.py](../../engine/lisa/backtest.py) / [calibration.py](../../engine/lisa/calibration.py) | Archive research metrics, record export and calibration shapes. |
| [api.js](../../web/js/api.js) / [auth.js](../../web/js/auth.js) | Existing browser transports/session flows; useful examples, not a substitute for server checks. |
| [app.js](../../web/js/app.js) / [admin.js](../../web/js/admin.js) | Existing rendering/polling/console consumers and legacy assumptions to avoid copying blindly. |
| [vercel.json](../../vercel.json) / [scheduled-jobs.yml](../../.github/workflows/scheduled-jobs.yml) | Deployment routing and scheduler invocation configuration. |

Related operating instructions:
[LOCAL_RUNNING.md](../operations/LOCAL_RUNNING.md),
[LOCAL_TEST_ACCEPTANCE.md](../operations/MANUAL_VERIFICATION.md),
[VERCEL_DEPLOYMENT.md](../operations/VERCEL.md),
[DATABASE_ARCHITECTURE.md](DATABASE.md),
[PAPER_PILOT.md](../operations/PAPER_PILOT.md).
