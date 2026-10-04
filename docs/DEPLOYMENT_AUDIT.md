# Deployment readiness audit

Architecture follow-up: PostgreSQL is now the selected pre-production backend.
Shared repositories, authentication, pooling, migrations, independent settlement,
and separate-process deployment are implemented. See
[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md) for current startup and
validation status; older migration recommendations below describe the prior state.


Audit date: 2026-10-03. Base commit: `dc81581`. Includes the local Micro Bets tab changes.

**Original audit verdict: not ready for the promised autonomous daily prediction service.**
The model and market-board components work in isolation, but the default deployment
does not connect them to scheduled generation, permanent prediction records,
settlement, or the existing results dashboard. Buying API access alone will not fix
these integration gaps.

## Remediation in this checkout

The table below records the original defects, rather than the current implementation.
The follow-up adds a SQLite-backed daily worker, startup scheduling, first-publication
prediction records, official-result settlement (including micro markets), restart
recovery, provider/model reuse, a worker lease, read-only board routes, and readiness
reporting. Micro Bets now has a dedicated user-facing tab. Payment confirmation no
longer issues an unverified activation key; paid checkout is disabled. The pricing
matrix marks unsupported integrations unavailable and removes unimplemented timing.
Accumulator offers remain theoretical and carry no executable price or stake.

Run `PYTHONPATH=engine python3 scripts/audit_offline.py` for the current 26 isolated
lifecycle checks. The JSON files below are preserved **original audit evidence**.
Current frontend checks: 28. Full pytest and actual production deployment remain
unverified in this environment.

**Deployment is still not certified ready.** Required external work: configure and
verify calendar/result access and SharpAPI for real prices; provide persistent writable
SQLite storage, outbound HTTPS, process supervision, TLS, and backups; monitor
`/api/ready` for failures or stale publications. Configure Telegram credentials if bot
access is required. Autonomous scheduled Telegram alerts, verified billing, booking
codes, live arbitrage, steam alerts, executable parlays, model-specific CLV capture,
and complete portfolio management remain unavailable. Legacy Telegram bankroll
menus still contain fixed profit projections; these are not measured returns and
need replacement before enabling that product flow. Do not sell these capabilities.
The packaged historical backtest does not validate the current independent model.
Quiet days can have zero fixtures; a successful dated publication must not invent picks.

See [DAILY_SERVICE_DESIGN.md](DAILY_SERVICE_DESIGN.md) for implementation details.

## Evidence and limits

- Reviewed Docker/systemd deployment, CLI startup, feed/providers, independent model,
  board, storage, settlement, REST routes, tier catalog, payments, Telegram, and web views.
- Ran `PYTHONPATH=engine python3 scripts/audit_offline.py`. This uses **artificial
  test fixtures**, mocks, and temporary SQLite; none of its outputs are real picks
  or evidence of predictive accuracy. Results: [audit-offline-results.json](audit-offline-results.json).
- All 28 existing/extended frontend checks passed (18 model-board, 3 renderer
  robustness, 7 accumulator math).
- Ran the real packaged archive backtest successfully. Its summary and provenance
  are in [audit-backtest-summary.json](audit-backtest-summary.json).
- CLI imports and SQLite read/write, deduplication, restart persistence, and
  write-once terminal settlement worked in the isolated probe.
- **Python pytest suite not run:** pytest is absent; installation failed because
  package-host DNS is unavailable. This audit does not certify that suite as green.
- **Live deployment not verified:** no `.env`, production database, provider tokens,
  Telegram configuration, or webhook secret exists in this checkout/environment.
  This does not establish whether a separate production server has them.
- Shell DNS resolution failed for all five provider/Telegram hosts. Local socket
  binding is prohibited, and Docker daemon access is denied in this environment.
  Consequently HTTP behavior was exercised directly through handlers, not through
  a listening server; Docker and authenticated provider calls remain unverified.
- No messages, transactions, production data changes, or deployment were performed.

## Critical and high-priority findings

| ID | Severity | Finding and evidence | Deployment consequence |
|---|---|---|---|
| A01 | Critical | `cli._cmd_start` only starts ingestion when legacy Odds API keys are present and enabled. Compose forces that API off. There is no scheduled `run_feed` worker. The isolated default-start probe recorded **zero background prediction threads**. | No unattended daily predictions; visitor traffic triggers the new board. |
| A02 | Critical | `feed.run_feed` returns a report but has no storage argument or prediction writes. `server._handle_opportunity_board` stores only `server._board_cache`. After a successful board request the probe found **0 picks and 0 persisted live keys**. | Published predictions disappear on restart and cannot become an auditable result history. |
| A03 | Critical | `LiveSettler` is not attached to production startup. The legacy scheduler calls settlement through the disabled legacy transport. No free-stack publication-to-settlement lifecycle exists. | Previous model and micro-bet results will not populate automatically. |
| A04 | Critical | Telegram `_handle_payment_confirmation` creates an activation key immediately on the user callback, without checking a transaction, amount, address, or confirmations. The probe generated a key and activated a test linked account to **tier3 without any payment proof**. | Paid access can be obtained without paying. Production payment handling must be repaired before selling access. |
| A05 | High | `/api/dashboard`, `/api/picks`, `/api/ledger` and Telegram pick/stat views consume the old ledger/cache. `/api/forecast` only reads `LIVE_ODDS_PREFIX` payloads that the new feed never writes. A successful new-board request still produced **no_live_data**, 0 forecasts and 0 previous results. | Home, Picks, Analytics, results and bot can remain empty while Model Board/Micro Bets show selections. |
| A06 | High | `Score.grade_pick('h2h', ...)` compares team names, but the board emits `Home`/`Away`. A real-named test home winner graded `Home` as **LOSS**. `correct_score` is unsupported and returned **None** for the correct score. | Simply connecting the existing settler would create incorrect or permanently pending results. |
| A07 | High | `LiveSettler._get_live_matches` checks only `now - kickoff <= 3h`, with no lower bound. It includes tomorrow's fixture and excludes old overdue fixtures. | Premature polling and missed recovery after extended downtime. |
| A08 | High | `storage.pick_key` omits the market line. The probe inserted Over 2.5 then found Over 3.5 silently rejected as a duplicate. | Different micro/totals predictions cannot safely share the existing ledger format unchanged. |
| A09 | High | `_default_leagues` selects only Bundesliga and 2. Bundesliga even when a football-data token is present. The example env explicitly selects those two. Window widening stops at 48h and cannot create fixtures on quiet days. | Neither daily nonempty picks nor 10–12 matches every day is guaranteed by this resource plan. |
| A10 | High | Both `OpportunityBoard` instances in `run_feed` receive only `volume_target`; model home advantage and all `board_min_*`, Kelly, max-stake and accumulator-size settings are not passed through. The probe configured min EV 900% yet still got 12 earning selections below it. | Operator changes can appear accepted while risk and selection behavior remains unchanged. |
| A11 | High | Opportunity-board route does not check the current user, entitlement, or reveal time. It returns full earning, micro and accumulator data publicly. The catalog labels several of those paid features. | Paid feature promises and actual access enforcement disagree. A public product is possible, but the catalog must match it. |
| A12 | High | Anonymous `?refresh=1` bypasses the cache without authorization. Two sequential forced requests caused two feed cycles in the probe. | Public visitors can repeatedly trigger expensive provider fetches and model fits. |
| A13 | High | Accumulator pricing multiplies the independently best price of each leg even when those prices come from different books, and marks the result `priced=True`. The probe reproduced mixed-book priced accumulators. | A displayed parlay price/EV may not be available at any single bookmaker. |
| A14 | High | `/api/health` unconditionally reports `healthy`; Docker healthcheck only confirms HTTP success. The probe got healthy with zero predictions, results and live data. `doctor` principally checks the retired odds feed, not successful free-stack generation. | Deployment can be green while the product is not producing predictions. |

Source locations: [cli.py](../engine/lisa/cli.py), [feed.py](../engine/lisa/feed.py),
[server.py](../engine/lisa/server.py), [board.py](../engine/lisa/board.py),
[live_settler.py](../engine/lisa/live_settler.py), [odds.py](../engine/lisa/odds.py),
[storage.py](../engine/lisa/storage.py), [telegram_bot.py](../engine/lisa/telegram_bot.py).

## Promised feature coverage

| Feature | Current assessment |
|---|---|
| Daily probability forecasts | Model/board exist; no autonomous production job or durable publications. |
| 10+ fixtures every day | Target with honest shortfall reporting, not a guarantee. Limited default leagues and fixture schedules can leave it empty. Unknown teams are excluded from the modelled board. |
| Previous predictions and outcomes | SQLite/results views exist; the new predictions never enter them. |
| BTTS, totals, correct-score micro-bets | Generated successfully in offline fixtures and exposed in Model Board and dedicated Micro Bets tab. Observed BTTS/correct-score prices are generally absent because SharpAPI fetch requests only moneyline and total-goals markets. Correct-score results are not supported by the current settler. |
| Winning and earning rankings | Board can compute both; earning requires actual matched prices and positive estimated edge. Not evidence of realized profit. |
| Kelly/bankroll sizing | Calculation and onboarding exist. New board settings are ignored; its table does not display bookmaker identity or stake fraction despite receiving them. Bankroll calculator/old picks are not a unified execution flow. |
| Accumulators | Model probabilities and correlation haircuts exist, including unpriced advisory combinations. Mixed-book pricing is not an executable parlay quote. Telegram `/acca` is a placeholder. |
| Shin consensus and Diamond picks | Original code exists but production transport is disabled. A two-book free price feed cannot satisfy the documented five-book gate as written. |
| Asian handicap and quarter-line settlement | Historical grading utilities exist. New board does not generate handicap opportunities. Live `Score.grade_pick` does not implement half-win/half-loss quarter-line settlement. |
| Trap advisories | Legacy daemon writes trap history and bot can read it; new feed does not publish that history. |
| Steam/line movement, slippage and CLV | Legacy mechanisms/analytics exist; new feed has no durable price time series, closing capture, or settlement lifecycle. Current UI cannot establish fresh steam signals from the new board. |
| Calibration and ROI | Analytics on saved settled rows exist; new board has no saved sample. Some older tracker logic falls back to fair odds for ROI, which must not represent actual returns on unpriced forecasts. |
| Telegram picks, results, alerts | Bot/admin/onboarding code exists; requires credentials and channel permissions. New board is not distributed into its existing pick/result sources. Curated-pick and `/acca` commands include placeholders. |
| Paid tiers and earlier reveals | Catalog and old pick masks exist; new opportunity endpoint bypasses paid timing/masks. Crypto confirmation is unverified; payment keys are in-memory and lost on restart. Subscription expiry needs a defined lifecycle; a payment key's one-hour expiry is not a membership expiry. |
| Card checkout/payments | Card command says coming soon. Existing shared-secret payment webhook is an application adapter, not a complete checkout/provider-native signature integration. A verified server-side payment integration is required. |
| Booking codes across six books | Explicitly unavailable: bot says there is no bookmaker integration. Tier catalog and parts of marketing still promise codes. Book links do not prove a populated sportsbook slip. |
| REST API, outbound subscriber webhooks | REST routes exist. No complete subscriber webhook delivery/registration lifecycle found; a Discord notifier is not that feature. |
| Portfolio covariance, soft-book arbitrage stream, weekly signed audit PDF | Catalog promises exist; no complete production implementations/delivery paths found. |
| Admin controls | Admin API, runtime overrides, audit logs and bot commands exist. Some overrides are ineffective in the new feed; free-stack feed execution does not consult the legacy pause/generation controls. |
| Live/in-play prediction | Current free-stack model board filters upcoming fixtures. Free price delay and free score delays do not support the advertised real-time in-play experience. |
| Multi-sport prediction | Current free provider registry/model board is football-oriented and the scoring model is football Dixon–Coles. Legacy broad sport configuration does not establish working current NBA/MLB/NFL prediction products. |
| Continuous web refresh | Dashboard polls the old dashboard route. Model/Micro board loads once on first visit, then requires manual refresh; its displayed data can age indefinitely on an open tab. |

The tier catalog, README and frontend guide should be reconciled with actual supported
features before taking subscriptions. Optional unfinished features should be visibly
unavailable rather than included in a paid promise.

## Resources needed and what was verified

| Resource | Required use | Assessment |
|---|---|---|
| Always-on Python host, persistent writable storage | Scheduler, API, results, accounts, recovery | Python/SQLite work locally. Target host and persistent mount not verified. Docker defaults to host networking and UID/GID 1000; these are host-specific and need validation. `start` explicitly uses SQLite even if another storage driver is selected. |
| HTTPS endpoint/reverse proxy and public domain | Browser sessions, payment callbacks and bot links | No complete public HTTPS deployment supplied. Payment messages currently point to `http://localhost:8080`, unusable for remote subscribers. |
| OpenLigaDB access | German fixture/results/model training | Adapter exists; no token needed by code. Actual egress and current fixture coverage unverified. German leagues alone cannot fill every day. |
| `FOOTBALL_DATA_TOKEN` plus explicit wider board leagues | Broader fixtures, results, model history | Missing locally. Official free plan lists 12 competitions, 10 calls/minute, delayed scores/schedules. Adding the token alone does not widen configured/default leagues. Historical depth must be tested for every promoted/new team. |
| `SHARPAPI_KEY` with relevant soccer/book/market coverage | Observed odds, earning ladder, priced staking | Missing locally. Official free plan is two books, 12 requests/minute, 60s delay; API marketing does not prove every configured league has usable prices today. Six pages consume half the advertised per-minute allowance, not a quarter as repo comments state. |
| Telegram token, public/VIP channel IDs, admin IDs and verified permissions | Verification, alerts, administration, invites | Missing locally; channel visibility, membership checks, invite rights and public URLs unverified. |
| Verified payment service, webhook secret, durable keys and expiry records | Provisioning, renewals, revocations | Missing/unconnected locally. Wallet addresses in code are not proof of ownership or transaction verification. |
| Monitoring, backups, deployment recovery | Detect broken feed/settlement; restore ledger | HTTP health exists but lacks prediction freshness/settlement readiness. No proven backup/restore or unattended recovery test supplied. |
| Development test tooling/CI | Regression coverage and release gate | pytest absent here; no CI workflow found. Existing tests are not a passed full release gate in this audit. |

Provider facts checked against official pages:
[football-data pricing](https://www.football-data.org/pricing),
[SharpAPI pricing](https://sharpapi.io/pricing),
[TheSportsDB API](https://www.thesportsdb.com/docs_api).
TheSportsDB describes a dedicated production key in its supporter offering; the
shared example key should not be treated as verified production coverage.

The free data plan can support a **prematch football forecast product after integration**.
It does not establish daily fixture availability, five-book sharp consensus,
instant score updates, a profitable edge, or every advertised premium service.

## Prediction quality evidence

The packaged archive contains 7,155 matches across five football leagues and four
seasons. The executed backtest evaluated 6,729 matches and placed 380 gated bets:

- Wins: 303; losses: 77; win rate: **79.74%**.
- ROI on wagered bankroll: **−1.81%**; net profit: **−687.19** from an initial 10,000 bankroll.
- Positive CLV share: **37.89%**; mean CLV: **−1.11%**.

These are results of the **older market-consensus strategy**, not a validation of
the new Dixon–Coles board. `walkforward` evaluates the earlier Elo/Poisson model.
No equivalent completed chronological evaluation of the current live board's
singles, micro-markets and accumulators was established by this audit. Its
nonempty output and model sufficiency flag do not establish profitability or
calibration. Do not use a high historical win rate as evidence of positive ROI.

## Required path to a deployable service

1. **One autonomous lifecycle:** start a bounded free-stack worker on boot;
   schedule fixture/model refresh and settlement independently of visitors; persist
   run state, errors and last success. Define daily reporting in the product timezone.
2. **Publish before kickoff and retain evidence:** save immutable prediction
   snapshots with match/market/selection/line identity, probability, model version,
   source time and observed price/book. Distinguish forecast-only rows from staked
   recommendations. Retain the first public prediction separately from later updates.
3. **Correct results and recovery:** normalize Home/Away selections; grade BTTS,
   totals, correct scores, pushes and quarter lines explicitly; retry all overdue
   pending fixtures after restart/outage. Preserve final scores and their provenance.
4. **One user-facing source:** serve Home, Picks, Analytics, Micro Bets, result
   history and Telegram from those stored publications; refresh stale open tabs;
   show actual bookmaker, quote age, model status and executable stake/price.
5. **Finish resource and commercial contracts:** configure verified league coverage
   and history; enforce settings, request budgets, paid masks and refresh controls;
   repair payments and persistence; remove promises without implementations.
   Price accumulators only from a valid common-book offer, or label them theoretical.
6. **Prove readiness:** full Python and frontend suites, then target-host integration
   checks using real credentials. Run at least a 7-day unattended canary, covering a
   restart, provider outage, delayed score and quiet fixture day; restore a backup.
   Readiness must fail visibly when prediction/settlement freshness is overdue.

A release should demonstrate: boot generates a report without visitors; every
published prediction can be retrieved after restart; official final scores settle
the same published selections; users can see previous results and micro-bets;
missing data produces a dated shortfall message; no paid key is issued without
verified payment; and the deployed data plan actually covers the selected leagues.
Daily **execution** can be assured by scheduling and monitoring. Daily nonempty
**recommendations** must remain conditional on real fixtures, adequate model data
and prices/quality gates; inventing selections to meet a quota would invalidate
the product.
