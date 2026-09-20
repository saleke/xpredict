"""Settlement: official results stamp WIN/LOSS, never-completed go VOID,
transport failure leaves the ledger untouched."""
from __future__ import annotations

from datetime import datetime, timezone

from lisa.settle import run_settlement

NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def test_settlement_grades_the_ledger(pipeline, fixture_client, storage, settings):
    pipeline.run_cycle(now=NOW)
    assert len(storage.list_pending_picks()) == 5

    rep = run_settlement(fixture_client, storage, settings, now=NOW)
    assert rep.pending == 5
    assert rep.settled == 2          # nba-a + lig-a
    assert rep.won == 2
    assert rep.lost == 0
    assert rep.void == 1             # nba-g (postponed, way past grace)
    assert rep.skipped_no_scores == 2  # nba-e + bdl-a (no score rows)

    # rows without an official score in the fixtures stay pending (nba-e, bdl-a)
    remaining = storage.list_pending_picks()
    assert len(remaining) == 2
    assert {r["match_id"] for r in remaining} == {"nba-e", "bdl-a"}

    rows = {r["dedupe_key"]: r for r in storage._picks.values()}
    assert rows["nba-a::h2h::Celtics"]["result"] == "WIN"
    assert rows["nba-a::h2h::Celtics"]["state"] == "SETTLED"
    assert rows["lig-a::h2h::Real Madrid"]["result"] == "WIN"
    assert rows["nba-g::h2h::Spurs"]["state"] == "VOID"
    assert rows["nba-g::h2h::Spurs"]["result"] == "VOID"
    assert rows["nba-e::h2h::Thunder"]["state"] == "TRIGGER_ALERT"  # untouched


def test_settlement_ignores_upstream_failure(pipeline, storage, settings):
    class Boom:
        def get_scores(self, sport, **kwargs):
            raise RuntimeError("scores endpoint down")

    pipeline.run_cycle(now=NOW)
    rep = run_settlement(Boom(), storage, settings, now=NOW)
    assert rep.errors and rep.settled == 0
    assert len(storage.list_pending_picks()) == 5  # nothing graded


def test_settlement_noop_without_pending(storage, settings, fixture_client):
    rep = run_settlement(fixture_client, storage, settings, now=NOW)
    assert rep.pending == 0 and rep.settled == 0