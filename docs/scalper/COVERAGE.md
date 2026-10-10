# Scalper coverage and next work

Coverage means an observed source, normalized fields, sufficient history, a
supported model and an eligible offered contract. A single percentage cannot
represent all five. The source observations below were reviewed on
2026-10-05/06; they are not a fresh probe of today's bookmaker inventory.

## Implemented scope

| Capability | Usable implementation | Boundary |
| --- | --- | --- |
| Calendars and result identity | ESPN's explicit 14-league source map and conservative source-scoped matching | Mapping does not establish continuous complete coverage |
| Goal history | Eight Openfootball league mappings and resumable current/previous-season bootstrap | Date anchors are not verified kickoffs; no rich player/event archive |
| Team statistics and periods | Available ESPN regulation counts and half scores | Missing fields stay missing; extra-time totals cannot silently settle regulation markets |
| Browser prices | Verified Pinnacle EPL route and partial SportyBet Nigeria landing view | Additional leagues/books need verified routes and exact market contracts |
| Price integrity | Atomic event/book replacement, expiry, change/suspension checks and separate confirmation clocks | Cached responses cannot acquire fresh bookmaker times |
| Durable collection | Raw hashes/validators, cooldowns, resumable cursors, leases and batched transactional merges | A successful cycle is not every league, model or offered market |
| Prospective odds history | Active changes/reopenings and hourly checkpoints, with bounded retention | No complete long-term closed-offer history or universal closing-price coverage |
| Advanced prediction markets | Goal families and separately supported corner model in LISA | Cards/fouls/throw-ins/player/half-time/live micro models are not validated |

All existing price-provider bookmakers remain available in `supporting` mode;
Scalper does not impose a global book allowlist. The two current browser adapters
do not imply bespoke collectors for every supported API bookmaker.

## Integrated improvements

Production merges batch prior-state reads, deletes and inserts, preserving
source locking, identity checks, final-result protection, rollback and older
observation rejection. The old experimental repository and candidate monkeypatch
runner were superseded by `engine/lisa/scalper/repository.py`.

Historical local SQLite replays reduced SportyBet/Pinnacle merge statement counts
from 1,865/1,585 to 25/23. These measurements are not a remote PostgreSQL SLA.
Reproduce the current implementation using previously saved captures:

```bash
python3 scripts/benchmark_scalper_merges.py --database data/localhost-scalper.db
```

The source database opens read-only and merges run in temporary databases.
No network calls or user-ledger writes are performed. An absent capture is not
a performance sample.

The offered-price floor, read-time expiry, quality-ranked diverse feed,
chronological display and subscription access are implemented. Their current
behavior is defined by [the pick-feed policy](../product/PICK_FEED.md), rather
than old release snapshot counts. See [checks](../operations/CHECKS.md).

## Remaining priorities

1. **Verified bookmaker view catalogues.** Earlier Bundesliga and La Liga
   Pinnacle captures parsed with parameterized EPL constants; only the sampled
   Bundesliga response had fresh eligible prices. They are not production
   routes. Scope view identities, leases and missing-offer reconciliation so a
   partial page cannot revoke another competition's prices. Validate publisher
   IDs, region, pagination and period/settlement semantics before enabling one.
2. **Typed enrichment from existing responses.** ESPN summaries have contained
   formations, rosters, referee/venue context and structured events beyond the
   normalized subset. Validate multiple matches, source player IDs, event periods,
   missing values and point-in-time availability before using additional fields.
3. **Bounded EPL FPL enrichment.** Public bulk/team/player-history samples were
   useful research candidates. FPL is not integrated. Validate player/season IDs,
   transfers, provisional results, update cadence and historical availability.
   Fantasy player costs are not bookmaker prices.
4. **Longer compact history and adaptive collection.** Preserve closure/gap
   events and identity/kickoff revisions, prioritize nearby kickoffs and useful
   gaps, and measure request yield/CPU/memory. Maintain durable quotas, recovery
   and settlement reserves. Browser context reuse needs coherent event/price
   snapshots and isolation before activation.
5. **New models or stream contracts.** Require exact offered markets, sufficient
   historical observations and chronological calibration/return evidence.
   Observing a socket or SSE connection alone does not establish a useful feed.

A previous conservative asset-blocking sample reduced body bytes by roughly 7%,
not the proposed 70–85%; no sub-second collection guarantee was established.
Async scheduling does not remove same-host pacing. More endpoints or more
markets help only when they improve fresh matched coverage or measured decisions.

No collector guarantees free universal access, immunity from source blocking,
low-latency live betting or profitable selections. Source failures remain
explicit; supporting providers and durable caches provide recovery where usable.
