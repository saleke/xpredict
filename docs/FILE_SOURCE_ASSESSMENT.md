# Free football file sources

Checked 2026-10-04 against publisher pages and sample files. This assessment
does not enable a provider, import data, establish all-season completeness, or
validate the running application's network access.

## Decision

Use bulk files to reduce schedule discovery and historical result requests.
Openfootball is the best candidate in this list for a supplementary source.
Keep authenticated feeds for current fixture verification, detailed statistics,
bookmaker offers, and prompt settlement. No source examined supplies the entire
data contract needed for every requested market.

| Source | Verified contribution | Integration decision |
|---|---|---|
| [Openfootball](https://github.com/openfootball/football.json) | Current-season JSON files and historical fixtures/results; [CC0 licence](https://github.com/openfootball/football.json/blob/master/LICENSE.md) | Integrated for goal history and provisional discovery across eight leagues; timed fixtures and settlement still require verified feeds |
| [Oritzio sample](https://github.com/oritzio/football-database/blob/master/England_PremierLeague.json) | Sample contains team IDs, names, abbreviations and stadiums | Not a fixture feed; do not use its README's season label as evidence of schedules or current results |
| [Football-Data.co.uk](https://football-data.co.uk/data.php) | Historical results, match statistics and odds; upcoming fixture files | Valuable data, but its free-use notice excludes commercial or automated training products; production use is not established |
| [FixtureDownload](https://fixturedownload.com/sport/football) | Schedule and result exports in JSON/CSV and other formats | [Terms](https://fixturedownload.com/terms) restrict third-party/website reuse; not an approved public-product input |
| [KickoffClock](https://kickoffclock.com/) | Advertises fixture exports and calendar subscriptions | Stable bulk endpoint, current broad reuse permission and update guarantees are not established |

## Important corrections to the supplied research

Openfootball's [README](https://raw.githubusercontent.com/openfootball/football.json/master/README.md)
describes a daily 05:00 UTC JSON build but explicitly says its upstream text
datasets do not have automatic daily updates. A successful download or recent
build is therefore insufficient evidence of recent match information.

The [sample EPL file](https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json)
contains dates, times, team names, rounds and scores. Scores appear both as an
object containing `ft`/`ht` and as a two-element array. Future rows omit scores.
The sampled records contain no explicit UTC offset or stable event ID. Do not
assume every score is available in both periods, infer a live match state from a
date, or silently treat local clock strings as UTC. Historical date-only rows
need a distinct time-unknown state.

Football-Data's [fixture page](https://football-data.co.uk/matches.php) describes
Tuesday/Friday price collection; the page checked displayed an upload dated
2 October 2026. Downloadable prices are snapshots, not continuously executable
offers. Its data page also warns about stale Pinnacle odds since July 2025.
Free downloading does not supersede the publisher's usage restrictions.

FixtureDownload's [JSON guidance](https://fixturedownload.com/view/json/ligue-1-2026)
says its feed is normally updated daily, is not live, and can change schema
without notice. Its general terms restrict commercial distribution and storage
or transmission through another website. It cannot simply be added to the
public dashboard because a JSON URL is accessible.

KickoffClock's retrieved [www terms](https://www.kickoffclock.com/terms) still
describe a World Cup service and personal-use calendar downloads, whereas its
current main page advertises league coverage. This does not establish a current
automated multi-league product contract. The original `/download` URL was not
retrievable through the research tool; this alone does not prove an outage.

## What these inputs support

Trusted scores and schedules can expand the input available to goal models for
1X2, double chance, draw no bet, goal totals, BTTS and cross-match accumulator
research. These remain model outputs requiring chronological validation.
Published half-time scores could support separate first-half research only where
coverage is sufficient. These files do not supply the missing current corner or
card statistics, player availability, event sequences, suspended market states,
or bookmaker-adjusted same-game accumulator quotes.

A winning probability is insufficient to rank earning opportunity: that ranking
also needs an actual available price for the precise market and settlement
contract. Importing schedules cannot solve an odds-provider access failure or
increase a different provider's credit allowance.

## Integration responsibilities

1. Download an allowlisted league-season file on a worker schedule, with bounded
   responses, timeouts, conditional HTTP requests when supported, and retry
   backoff. Backfill completed seasons once rather than on every prediction run.
2. Persist raw snapshots or durable snapshot references, content hashes,
   retrieval times and source provenance. In production, retain these in the
   managed database/object storage; a Vercel function's local files are not the
   durable store. Keep retrieval time separate from evidence of source updates.
3. Parse into staging records. Check season, teams, duplicates, score shapes,
   plausible dates and document bounds. Measure completeness separately. Quarantine unknown kickoffs and unmatched
   identities; do not substitute a midday kickoff into live selection logic.
4. Reconcile identities and rescheduled fixtures with existing observations.
   Preserve provider IDs and revisions. A changed kickoff must not create a
   second real match or overwrite a trustworthy cancellation/result.
5. Use authenticated feeds to verify shortlisted upcoming fixtures and resolve
   results. Keep conflicting scores out of model training and settlement;
   mirrors of a common upstream are not independent confirmation.
6. Run the existing generation, publication and settlement workers against the
   stored data. Website visits remain reads. Reserve metered requests for the
   provider's useful enrichment, verification, odds and settlement capabilities.
7. Measure accepted fixtures, upcoming coverage, result lag, price join rate,
   rejected rows and requests saved per league. Enable more leagues only after
   those measurements establish useful coverage.

Empty days must distinguish no scheduled eligible fixtures, missing coverage,
failed collection and no qualifying bets. Daily job execution does not imply a
justified bet exists every day.

## Current repository boundary

`scripts/import_history.py` is an importer for completed Football-Data.co.uk
CSV matches. It requires full-time goal values and supports five league codes.
It is not an upcoming-fixture CSV or generic JSON importer. Copying a download
into a directory does not connect it to daily generation.

`engine/lisa/providers/openfootball.py` now integrates the vetted CC0 JSON files
through the existing worker, observation repository and model pipeline. Current
and previous season files bootstrap generation; the history job collects older
seasons through resumable checkpoints. Persistent caches, conditional HTTP,
bounded downloads and daily failure cooldowns limit repeat traffic. All file
times remain unknown and all file rows are barred from settlement. No new
service or database was introduced. See [LEAN_DATA_STACK.md](LEAN_DATA_STACK.md)
for source roles, configuration and validation boundaries.
