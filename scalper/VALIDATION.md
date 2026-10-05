# Scalper verification

Verified in this workspace on 2026-10-05. Initial probes used isolated temporary
databases. The subsequently requested localhost run uses a dedicated paper
database; the installation's private settings and original ledger were not changed.

## Automated checks

* Browser-phase complete engine suite: **885 passed, 11 skipped**, plus 32 passing
  subtests. The final focused run, including empty closed-market revocation and
  concurrent independent commits and sole-source history delegation, passed
  **84 tests** and nine subtests, with two PostgreSQL skips. The earlier
  core-phase full run passed 860 tests.
  The skipped checks require an isolated PostgreSQL test database, which was
  unavailable. PostgreSQL-specific Scalper merge/revocation/concurrency tests
  are included; a real PostgreSQL run remains necessary for that deployment.
* Core Scalper's focused suite: 41 cases, 39 passed and two PostgreSQL skips.
  This includes a sole-source DailyService publication using persisted history
  and current quotes, with paper-mode stakes kept at zero.
* All four frontend test scripts passed. The public asset build passed in a
  temporary output directory. Compilation and whitespace checks passed.

Existing test fixtures were repaired where they omitted required market lines,
used old ledger keys or readiness/error contracts, froze a pacing clock without
advancing it, or leaked process settings between tests. The production contract,
HTML escaping, error sanitization and evidence requirements remain enforced.

## Actual collection

The native one-cycle worker made two real requests using an isolated temporary
SQLite database. The ESPN EPL daily response was valid but empty. Openfootball's
current-season file supplied **380 records**, including **50 completed matches**
accepted by the historical repository at the observation time. Calendar bounds
and unknown-time flags prevent its remaining fixtures becoming timed predictions.

Separately inspected ESPN completed-match and summary responses supplied actual
scores, corners, fouls, shots, half-time scores and cards for the sampled EPL
matchday. This establishes parsing for those responses, not universal coverage
of every mapped league or statistic.

SportyBet's tested endpoint returned **403**. Its experimental adapter is opt-in,
and its unverified bookmaker-update times cannot produce executable quotes.
These initial direct-request probes did not establish a bookmaker price feed.
Subsequent browser verification did, as detailed below. Existing bookmaker
adapters remain available in supporting mode; normalized collectors can also
supply quotes with explicit market contracts and actual source timestamps.

## Browser-feed verification

Ordinary headless Chrome loaded both public bookmaker pages. The worker captures
only source-specific JSON response routes and a small freshness-header allowlist;
cookies, authorization headers and arbitrary URL queries are never archived.
Direct SportyBet access returned 403; its browser page supplied usable data.
DraftKings' normal browser page still returned 403 and has no new verified
browser adapter.

An isolated SQLite trial exercised the actual browser CLI and its concurrent
worker, then the unchanged goal models through the sole-source DailyService:

* SportyBet: **10 EPL events, 324 parsed quotes, 320 fresh supported quotes**.
  Four suspended quotes were rejected. Coverage is a partial landing page.
* Pinnacle: **20 EPL events, 1,482 parsed quotes, 942 fresh regulation quotes**.
  Its 540 first-half quotes remained outside the regulation bridge. An earlier
  response with several minutes of CDN age produced zero executable quotes;
  a later current response admitted its prices without changing price clocks.
* Combined provider: **1,262 fresh quotes**, 24 source events matched across
  17 calendar fixtures, six unmatched, zero ambiguous/contested identities.
* Sole-source publication: **13 forecasted fixtures, all 13 priced**, with 57
  priced entries across the published research ladders. Stakes remained zero.
  Fifty actual Openfootball results supplied the model; its average of five
  games per rating correctly left it unproven.
* Published provenance retained the publisher confirmation timestamp and left
  Pinnacle's absent individual price-change timestamp null.

The browser regression suite exercises HTTP Date/Age, stale/missing/future
clocks, old snapshots, suspensions, disappearing events, partial coverage,
period isolation, selected-side Asian lines, immutable history deduplication,
restart/cooldown recovery, independent leases, sanitized passive capture and
database failures. Browser price history records actual changes/reopenings and
hourly confirmation checkpoints rather than every unchanged poll.

These trials establish current prematch prices and pipeline compatibility.
They do not establish every bookmaker/league, in-play latency, profitable
selection calibration or working player/card/foul/throw-in prediction models.

## Localhost verification

The dashboard and admin console run on port 8080 with independent core and
browser collectors sharing `data/localhost-scalper.db`. The run uses Scalper-only
mode and paper stakes. It imports 4,397 real historical results from the existing
paper database, retaining their source attribution, then adds collected history.

HTTP checks passed for both pages, their application assets, health, readiness,
forecasts and the authenticated opportunity board. Dashboard and owner admin
sign-in passed. A Chrome check rendered 12 forecast cards and 50 prediction cards,
opened Production testing and recorded no JavaScript page errors. At that check,
the board contained 47 priced ladder entries, with a minimum offered price of
1.18 and all stakes zero. Prices and counts can change on subsequent cycles.

Verification exposed and fixed a misleading history failure when API backfill
providers are intentionally absent in Scalper-only mode. History now reports
the separate collector's state without launching duplicate collection. The
local launcher's port check also permits normal address reuse after shutdown.
Generation, history and settlement subsequently reported OK. The pilot audit
retains the earlier failed startup cycle; it was not erased to claim a clean run.

See [setup and source boundaries](README.md) and [the research decision](DESIGN.md).
