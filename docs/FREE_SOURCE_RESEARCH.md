# Free football data source assessment

Checked 3 October 2026 against provider documentation and dataset-owner pages.
This is published-contract research, not authenticated coverage or latency
validation. The workspace still cannot resolve provider hostnames. No accounts,
subscriptions, purchases or new adapters were created for this assessment.

## Decision

An expanded free stack could support a restricted prematch pilot, historical
research and selected advanced markets. It does not establish sufficient resources
for every promised market across multiple leagues, low-latency live micro bets,
or bookmaker-priced same-game accumulators. Data access does not establish model
accuracy or profitability. Coverage must be evaluated per league, season,
bookmaker, market, period and player, rather than per provider.

## Priority candidates

| Source | Published free access | Distinct contribution | Limit / decision |
|---|---|---|---|
| [OddsPapi signup](https://oddspapi.io/us/sign-up) | 250 requests/month; all sports and bookmakers advertised | Additional prices and prospective historical evaluation | Highest-priority authenticated probe; exact football market coverage unverified |
| [OddsPapi history](https://oddspapi.io/us/docs/get-historical-odds) | History from January 2026; five-second cooldown; up to three books per request | Timestamped price history with active/inactive entries | Short history; provider observation timestamps are not proof of bookmaker freshness |
| [The Odds API](https://the-odds-api.com/) | 500 credits/month, no free historical odds | Targeted football price samples | Different service from the similarly named unhyphenated domain |
| [The Odds API market catalog](https://the-odds-api.com/sports-odds-data/betting-markets.html) | Documents football props, corners, cards and period markets | Most directly documented candidate for advanced prematch prices | Actual availability depends on book/event; listed soccer props limited to US bookmakers |
| [Sportmonks free plan](https://www.sportmonks.com/football-api/free-plan/) | Danish Superliga and Scottish Premiership, no expiry | Detailed events, player/team statistics and historical research | Focused two-league pilot; do not assume premium odds/xG add-ons are included |
| [FPL official public feed](https://fantasy.premierleague.com/api/bootstrap-static/) | Public JSON endpoint | EPL player availability and fantasy-related player features | Undocumented integration contract; no uptime commitment or broad commercial permission established |
| [StatsBomb Open Data](https://github.com/hudl/open-data) | Selected competitions with events, lineups and selected 360 data | Prototype event/player models and replay tests | Research resource, not current all-league live feed; inspect its data licence before product use |
| [Wyscout/Pappalardo dataset](https://figshare.com/collections/Soccer_match_event_dataset/4415000) | Static historical event collection | Event-model research and player/period examples | Older competitions; no current odds, lineups or live delivery |
| [Impect Open Data](https://github.com/ImpectAPI/open-data) | Bundesliga 2023/24 data | Event KPIs, player aggregates, lineups and substitutions | Historical research; inspect provider licence and completeness |

[OddsPapi quota documentation](https://oddspapi.io/us/docs/requests-and-quota)
says historical-odds requests do not consume the monthly allowance. However,
exhausting billable quota blocks even that endpoint; account access remains
available. Cache discovery catalogs, batch tournament requests, reserve quota and
verify entitlement before scheduling history downloads. Its
[WebSocket](https://oddspapi.io/us/docs/websocket-api) requires contacting the
provider or B2B access; free streaming is not established.

The Odds API charges by requested/returned market-region combinations under its
[v4 rules](https://the-odds-api.com/liveapi/guides/v4/), including separate event
requests for advanced markets. Reserve it for selected candidates rather than
polling every market for every fixture. The legacy integration is currently
disabled: research identifies a reason to reconsider it, not an enabled adapter
or a new key. Historical endpoints remain paid-only.

The [FPL community archive](https://github.com/vaastav/Fantasy-Premier-League)
states that weekly updates stopped after 2024/25, replaced by three major seasonal
updates. Treat snapshots as archival, not proof of current availability. Its
[dictionary](https://github.com/vaastav/Fantasy-Premier-League/blob/master/DATA_DICTIONARY.md)
describes minutes, goals, cards and expected-goals features. Fantasy assists and
metrics must not be assumed to follow a bookmaker's settlement definitions.

## Historical archive correction

[football-data.co.uk](https://football-data.co.uk/data.php) offers valuable
half-time/full-time results, corners, cards, shots, referees and odds. However,
its current notice restricts free use to private individuals and excludes
commercial or data-training products involving automated bots/scrapers/AI.
Consequently it is not an established freely usable production-training source
for this project. Clarify permission or obtain an appropriately licensed source
before further automated collection or product training. The existing importer
and evaluation artifact do not establish those rights.

The same page warns that Pinnacle prices have been systematically stale since
23 July 2025. Upcoming price collection is scheduled around midweek/weekend
fixtures, not a live feed. Its opening/closing columns do not supply a complete
minute-by-minute quote history. Preserve archive provenance and do not present
retrospective prices as executable historical offers.

## Other candidates and exclusions

| Source | Assessment |
|---|---|
| [UK Odds API](https://ukoddsapi.com/football-odds-api/) | Free sandbox: two UK books, core markets, 300 requests/month. Advanced markets and bookmaker-published bet builders are listed on paid Pro. Useful targeted core-price comparison, not a free SGP solution. |
| [SportsGameOdds](https://sportsgameodds.com/pricing) | Free: 2,500 objects/month, 10-minute updates, Champions League/MLS among listed leagues. Useful prematch samples; refreshes consume objects repeatedly. A visible feature list needs account-specific entitlement checks. |
| [SportsGameOdds SGP page](https://sportsgameodds.com/use-cases/parlay-builder-api) | Explicitly supplies legs, not a correlation-adjusted combined bookmaker price. Does not close the executable SGP gap. |
| [Odds-API.io](https://odds-api.io/pricing) | Main pricing page says new free keys are paused indefinitely. Its free landing page still advertises signup. Do not depend on access until the provider confirms it. |
| [SockOdds](https://sockodds.com/pricing/) | Published free tier lists MLB, NFL, AFL and NRL; soccer catalog does not prove free soccer entitlement. Exclude from recommended free football stack. |
| [Cordax](https://api.cordax.net/Home/Pricing) | Free: 139 competitions, history from 2024, one request/minute, ten-minute live delay, restricted statistics. Potential calendar backup; insufficient for live micro recommendations. |
| [OpenLigaDB](https://api.openligadb.de/index.html) | Already integrated; keyless, 60 requests/minute/IP. Use change-date checks and cached seasons. ODbL attribution/share-alike obligations require appropriate handling. Useful results redundancy, not prices/props. |
| [OpenFootball](https://github.com/openfootball/football.json) | Public-domain results backup. Daily JSON generation does not mean upstream data is updated daily. Not a live-statistics or odds source. |
| [TheSportsDB](https://www.thesportsdb.com/documentation) | Already integrated. Current documented public key is 123, free limit 30/minute; several methods are restricted. Existing default 3 needs live compatibility checking. Premium supplies live scores. |
| [Betfair](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access) | Free delayed development access is not a free production live-price licence. Live betting access has a published activation fee and read-only live-key access is disallowed. |
| [Matchbook](https://developers.matchbook.com/docs/faq) | Account API exists, but fees depend on use. No unconditional free commercial feed established. |

Do not base production coverage on undocumented website endpoints or a scraper
package merely because they are accessible. No verified provider data contract,
commercial permission or update guarantee was established here for that route.

## Combining sources efficiently

1. Select a small competition set and target bookmakers usable by the audience.
   Rich free statistics in Scotland/Denmark are useful only where matching odds
   exist. Broader EPL coverage does not automatically extend to those leagues.
2. Use existing calendars/results plus a source of detailed historical statistics
   with confirmed rights. Keep research datasets separate from production inputs.
3. Use frequent core prematch prices from the existing SharpAPI adapter, and
   scarce new-provider quota only for missing markets on shortlisted fixtures.
4. Store immutable dated observations and prices, source IDs, periods and market
   definitions. Map team/player identities explicitly. The same upstream feed
   repeated through two aggregators is not independent confirmation.
5. Separate requests by purpose and retain settlement capacity. Batch fixtures,
   cache metadata and use conditional updates. More polling cannot eliminate a
   provider's published data delay.
6. Validate each league/market on chronological data before stake approval.
   Treat no offer, unknown timestamp or conflicting result as unavailable.

## Resource sufficiency by feature

| Feature | Conclusion with an expanded free stack |
|---|---|
| Daily fixtures, previous results, goals/BTTS/double chance | Plausible selected-league operation, subject to authenticated checks |
| Ordinary cross-match accumulators | Forecast construction feasible; combined availability/prices still need confirmation |
| First-half markets | Plausible pilot where suitable historical scores and prices overlap |
| Corners, corner handicaps, cards | Promising research/prematch pilot; history completeness, rights and settlement semantics remain gates |
| Player props | Narrow league/player/market pilot possible; detailed current history and bookmaker rules remain gaps |
| Minute-by-minute next-event bets | No documented sustainable free multi-league resource combination established |
| Executable same-game accumulators | No free combined-quote source established |

Next action: obtain optional OddsPapi and original The Odds API keys, verify their
account limits/football market samples, and measure the intersection of statistics
and offers. Consider Sportmonks free access for a focused two-league experiment.
No signup or purchase is needed merely to retain this research plan.
