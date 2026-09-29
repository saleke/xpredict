"""Live end-to-end check of the BetExplorer backfill against the real API.

Not a unit test — this spends Parse credits and hits the network, so it is a
script rather than part of the suite. Run:

    cd engine && PYTHONPATH=. python tests/live_backfill_check.py

It builds synthetic pending picks from fixtures that the live feed *actually*
returned, so a green run proves the real payload shape, the real team names and
the real 1X2 prices all flow through to a settled ledger row.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.backfill import SettlementBackfiller  # noqa: E402
from lisa.betexplorer import BetExplorerClient, BetExplorerError  # noqa: E402
from lisa.gate import Execution, Pick  # noqa: E402
from lisa.odds import utcnow  # noqa: E402
from lisa.storage import InMemoryStorage  # noqa: E402

KEY = os.environ.get("PARSE_API_KEY", "")


def build_storage_from(fixtures, day, take: int = 6, *, better: bool = True):
    """Create one pending pick per real fixture, priced against the close.

    With ``better=True`` the pick is priced 5% *above* the closing line, i.e.
    it beat the close, so CLV must come out positive. That is the direction
    that proves the CLV formula is wired up rather than merely present: a
    negative result would indicate a sign or inversion error.

    Pick *both* winning and losing sides so settlement is exercised in both
    directions — picking only favourites would prove nothing about grading.
    """
    storage = InMemoryStorage()
    for fixture in fixtures[:take]:
        if not fixture.has_1x2 or fixture.score is None:
            continue
        # Alternate the picked side so roughly half the rows must settle LOSS.
        pick_home = (len(storage.list_pending_picks()) % 2 == 0)
        outcome = fixture.home_team if pick_home else fixture.away_team
        close = fixture.odds_home if pick_home else fixture.odds_away
        price = round(close * (1.05 if better else 0.95), 2)
        pick = Pick(
            match_id=f"live-{fixture.event_id}",
            sport_key="soccer_unknown",
            home_team=fixture.home_team,
            away_team=fixture.away_team,
            commence_time=datetime.combine(day, datetime.min.time(),
                                           tzinfo=timezone.utc) + timedelta(hours=15),
            market="h2h",
            outcome_name=outcome,
            p_true=0.60,
            fair_odds=price,
            n_books=3,
            stdev=0.01,
            cv=0.02,
            best_execution=Execution("book1", "Book1", price, 0.05),
            state="TRIGGER_ALERT",
            created_at=utcnow(),
        )
        storage.insert_pick(pick)
    return storage


def main() -> int:
    if not KEY:
        print("PARSE_API_KEY not set", file=sys.stderr)
        return 1

    client = BetExplorerClient(api_key=KEY, min_interval_sec=1.0)
    day = (utcnow() - timedelta(days=1)).date()

    try:
        fixtures = client.get_finalised(day)
    except BetExplorerError as exc:
        print(f"live fetch failed: {exc}", file=sys.stderr)
        return 1

    print(f"{day}: {len(fixtures)} finalised fixture(s) with a 1X2 line")
    if not fixtures:
        print("no usable fixtures returned; cannot exercise the join")
        return 1

    storage = build_storage_from(fixtures, day)
    pending = len(storage.list_pending_picks())
    print(f"seeded {pending} synthetic pending pick(s) from real fixtures\n")

    print("--- DRY RUN ---")
    dry = SettlementBackfiller(storage=storage, client=client).run(dry_run=True)
    print(f"  settled={dry.settled} W{dry.won}/L{dry.lost}/V{dry.void} "
          f"clv={dry.clv_updated} nomatch={dry.skipped_no_match} "
          f"notfinal={dry.skipped_not_final} noprice={dry.skipped_no_price} "
          f"ambiguous={dry.skipped_ambiguous} errors={len(dry.errors)}")
    if storage.list_settled_picks():
        print("  FAIL: dry run wrote to the ledger")
        return 1
    for row in dry.settled_picks[:4]:
        print(f"    {row['home_team']} {row['final_score']} {row['away_team']} "
              f"| took {row['best_odds']} vs close {row['closing_odds']} "
              f"=> CLV {row['clv']:+.4f}")

    print("\n--- APPLY (in-memory storage only) ---")
    applied = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    print(f"  settled={applied.settled} W{applied.won}/L{applied.lost}/V{applied.void} "
          f"clv={applied.clv_updated}")

    settled = storage.list_settled_picks()
    print(f"\n  ledger rows: {len(settled)}")
    failures = []
    for row in settled:
        print(f"    {row['home_team'][:22]:22} {str(row['result']):5} "
              f"close={row['closing_odds']} clv={row['clv']:+.4f}")
        if row["result"] not in ("WIN", "LOSS", "VOID"):
            failures.append(f"bad result {row['result']!r}")
        if row["clv"] is None or row["clv"] <= 0:
            failures.append(f"clv not positive for {row['dedupe_key']}")

    # No row may be left pending, and both WIN and LOSS must appear, otherwise
    # the fixture set happened to be one-sided and grading was not exercised.
    if len(settled) != pending:
        failures.append(f"expected {pending} settled, got {len(settled)}")
    if applied.won == 0 or applied.lost == 0:
        failures.append(f"expected both WIN and LOSS, got W{applied.won}/L{applied.lost}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nOK: {len(settled)} real fixture(s) joined, graded and CLV-stamped.")
    print(f"    Parse credits spent: {client.calls_made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
