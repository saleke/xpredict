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


def test_score_grade_pick_h2h():
    from lisa.odds import Score
    s = Score(match_id="m1", sport_key="basketball_nba", commence_time=NOW,
              completed=True, home_score=110, away_score=102, status="final",
              home_team="Celtics", away_team="Knicks")
    assert s.grade_pick("h2h", "Celtics") == "WIN"
    assert s.grade_pick("h2h", "Knicks") == "LOSS"
    assert s.grade_pick("h2h", "Draw") == "LOSS"


def test_score_grade_pick_totals():
    from lisa.odds import Score
    s = Score(match_id="m1", sport_key="basketball_nba", commence_time=NOW,
              completed=True, home_score=110, away_score=102, status="final",
              home_team="Celtics", away_team="Knicks")  # total = 212

    # Over / Under with half-point lines
    assert s.grade_pick("totals", "Over", line=211.5) == "WIN"
    assert s.grade_pick("totals", "Under", line=211.5) == "LOSS"
    assert s.grade_pick("totals", "Over", line=212.5) == "LOSS"
    assert s.grade_pick("totals", "Under", line=212.5) == "WIN"

    # Integer line push -> VOID
    assert s.grade_pick("totals", "Over", line=212.0) == "VOID"
    assert s.grade_pick("totals", "Under", line=212.0) == "VOID"


def test_score_grade_pick_spreads():
    from lisa.odds import Score
    s = Score(match_id="m1", sport_key="basketball_nba", commence_time=NOW,
              completed=True, home_score=110, away_score=102, status="final",
              home_team="Celtics", away_team="Knicks")  # margin = Celtics by 8

    # Home favourite covering vs not covering
    assert s.grade_pick("spreads", "Celtics", line=-7.5) == "WIN"   # 110 - 7.5 = 102.5 > 102
    assert s.grade_pick("spreads", "Celtics", line=-8.5) == "LOSS"  # 110 - 8.5 = 101.5 < 102

    # Away underdog covering vs not covering
    assert s.grade_pick("spreads", "Knicks", line=8.5) == "WIN"     # 102 + 8.5 = 110.5 > 110
    assert s.grade_pick("spreads", "Knicks", line=7.5) == "LOSS"    # 102 + 7.5 = 109.5 < 110

    # Integer line pushes -> VOID
    assert s.grade_pick("spreads", "Celtics", line=-8.0) == "VOID"  # 110 - 8.0 = 102 == 102
    assert s.grade_pick("spreads", "Knicks", line=8.0) == "VOID"    # 102 + 8.0 = 110 == 110


def test_settlement_multi_market_ledger_execution(storage, settings):
    from lisa.gate import Execution, Pick
    from lisa.odds import Score, utcnow

    class MockClient:
        def get_scores(self, sport, **kwargs):
            return [{
                "id": "m-multi", "sport_key": "basketball_nba",
                "commence_time": "2026-09-20T00:00:00Z",
                "completed": True, "score_status": "final",
                "home_team": "Celtics", "away_team": "Knicks",
                "scores": [{"name": "Celtics", "score": "110"},
                           {"name": "Knicks", "score": "102"}],
            }]

    # 1. Totals pick (Over 210.5 -> WIN)
    storage.insert_pick(Pick(
        match_id="m-multi", sport_key="basketball_nba", home_team="Celtics",
        away_team="Knicks", commence_time=utcnow(), market="totals",
        outcome_name="Over", p_true=0.8, fair_odds=1.25, n_books=5,
        stdev=0.01, cv=0.01, best_execution=None, state="TRIGGER_ALERT",
        created_at=utcnow(), line=210.5,
    ))

    # 2. Totals push (Over 212.0 -> VOID)
    storage.insert_pick(Pick(
        match_id="m-multi", sport_key="basketball_nba", home_team="Celtics",
        away_team="Knicks", commence_time=utcnow(), market="totals",
        outcome_name="Under", p_true=0.8, fair_odds=1.25, n_books=5,
        stdev=0.01, cv=0.01, best_execution=None, state="TRIGGER_ALERT",
        created_at=utcnow(), line=212.0,
    ))

    # 3. Spreads pick (Knicks +7.5 -> LOSS)
    storage.insert_pick(Pick(
        match_id="m-multi", sport_key="basketball_nba", home_team="Celtics",
        away_team="Knicks", commence_time=utcnow(), market="spreads",
        outcome_name="Knicks", p_true=0.8, fair_odds=1.25, n_books=5,
        stdev=0.01, cv=0.01, best_execution=None, state="TRIGGER_ALERT",
        created_at=utcnow(), line=7.5,
    ))

    assert len(storage.list_pending_picks()) == 3

    rep = run_settlement(MockClient(), storage, settings, now=NOW)
    assert rep.pending == 3
    assert rep.settled == 2
    assert rep.won == 1   # Over 210.5
    assert rep.lost == 1  # Knicks +7.5
    assert rep.void == 1  # Under 212.0 push

    picks = {r["dedupe_key"]: r for r in storage._picks.values()}
    assert picks["m-multi::totals::Over"]["result"] == "WIN"
    assert picks["m-multi::totals::Under"]["result"] == "VOID"
    assert picks["m-multi::totals::Under"]["state"] == "VOID"
    assert picks["m-multi::spreads::Knicks"]["result"] == "LOSS"