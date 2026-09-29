"""Tests for the BetExplorer settlement/CLV backfill.

The whole point of the backfill is to write results into the accuracy ledger,
so the tests concentrate on the failure that actually matters: joining the wrong
fixture or grading it wrong. Every "must not match" case below is a real
club-name collision that a naive substring or fuzzy match would get wrong.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lisa.backfill import BackfillReport, SettlementBackfiller
from lisa.betexplorer import (
    BetExplorerClient,
    BetExplorerError,
    BetExplorerMatch,
    normalise_team,
    parse_matches,
    team_match_score,
)
from lisa.gate import Execution, Pick
from lisa.odds import utcnow
from lisa.storage import InMemoryStorage


# -- name normalisation ---------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Inter Miami", "inter miami"),
    ("Inter Miami CF", "inter miami"),
    ("Paris Saint-Germain", "paris saint germain"),
    ("Paris Saint Germain", "paris saint germain"),
    ("Atlético Madrid", "atletico madrid"),
    ("Brighton and Hove Albion", "brighton and hove albion"),
    ("Nott'm Forest", "nottm forest"),
    ("", ""),
])
def test_normalise_team(raw, expected):
    assert normalise_team(raw) == expected


def test_normalise_keeps_distinguishing_words():
    """Legal-form noise may be dropped; identity words may not."""
    assert "united" in normalise_team("Manchester United")
    assert "city" in normalise_team("Manchester City")


# -- the collisions that must not join ------------------------------------
@pytest.mark.parametrize("a,b,why", [
    ("Manchester United", "Manchester City", "united/city disambiguate"),
    ("Atletico Madrid", "Real Madrid", "shared 'madrid' only"),
    ("Real Madrid", "Real Betis", "shared 'real' only"),
    ("Inter Miami", "Inter Miami B", "senior vs reserves"),
    ("Arsenal", "Arsenal Women", "senior vs women"),
    ("Liverpool", "Liverpool FC Women", "senior vs women"),
    ("Bayern Munchen", "Bayern Munich II", "senior vs II"),
    ("Chelsea", "Chelsea U21", "senior vs youth"),
])
def test_distinct_clubs_never_match(a, b, why):
    assert team_match_score(a, b) == 0.0, why


@pytest.mark.parametrize("a,b", [
    ("Brighton and Hove Albion", "Brighton"),
    ("Manchester United", "Manchester United"),
    ("Inter Miami", "Inter Miami CF"),
    ("Paris Saint-Germain", "Paris Saint Germain"),
    ("Atletico Madrid", "Atlético Madrid"),
])
def test_same_club_matches(a, b):
    assert team_match_score(a, b) >= 0.85


# -- payload parsing ------------------------------------------------------
def _payload(**overrides):
    match = {
        "event_id": "e1", "country": "England", "league": "Premier League",
        "home_team": "Arsenal", "away_team": "Chelsea", "time": "15:00",
        "status": "FIN", "score": "2:1",
        "odds_home": "1.50", "odds_draw": "4.50", "odds_away": "6.00",
    }
    match.update(overrides)
    return {"status": "success", "data": {"date": "2026-09-28", "matches": [match]}}


def test_parse_reads_score_and_odds():
    m = parse_matches(_payload())[0]
    assert m.score == (2, 1)
    assert (m.odds_home, m.odds_draw, m.odds_away) == (1.50, 4.50, 6.00)
    assert m.is_final and m.has_1x2


def test_parse_tolerates_string_and_dash_scores():
    assert parse_matches(_payload(score="3-0"))[0].score == (3, 0)


def test_parse_rejects_garbage_score_as_unfinal():
    """An unparseable score must not be treated as a final result."""
    m = parse_matches(_payload(score="aet"))[0]
    assert m.score is None
    assert not m.is_final


def test_parse_drops_null_odds_but_keeps_fixture():
    m = parse_matches(_payload(odds_home=None, odds_draw=None, odds_away=None))[0]
    assert m.is_final
    assert not m.has_1x2


def test_parse_skips_rows_missing_a_team():
    assert parse_matches(_payload(home_team="")) == []


def test_parse_rejects_unexpected_shape():
    with pytest.raises(BetExplorerError):
        parse_matches({"status": "success"})
    with pytest.raises(BetExplorerError):
        parse_matches({"data": {"matches": "nope"}})


# -- price lookup ---------------------------------------------------------
def _fixture(**over):
    base = dict(
        event_id="e1", country="England", league="Premier League",
        home_team="Arsenal", away_team="Chelsea", kickoff_local="15:00",
        status="FIN", score=(2, 1), odds_home=1.50, odds_draw=4.50, odds_away=6.00,
    )
    base.update(over)
    return BetExplorerMatch(**base)


def test_price_for_each_side_and_draw():
    f = _fixture()
    assert f.price_for("Arsenal") == 1.50
    assert f.price_for("Chelsea") == 6.00
    assert f.price_for("Draw") == 4.50


def test_price_uses_normalised_team_matching():
    """A pick on the long name must still find the price on the short one."""
    f = _fixture(home_team="Brighton")
    assert f.price_for("Brighton and Hove Albion") == 1.50


def test_price_returns_none_for_unknown_outcome():
    assert _fixture().price_for("Sheffield United") is None


# -- CLV ------------------------------------------------------------------
@pytest.mark.parametrize("taken,closing,expected", [
    (2.00, 2.00, 0.0),
    (2.20, 2.00, 0.10),   # beat the close
    (1.80, 2.00, -0.10),  # took worse than the close
])
def test_clv_matches_pipeline_convention(taken, closing, expected):
    got = SettlementBackfiller._clv(taken, closing)
    assert got == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("taken,closing", [(None, 2.0), (2.0, None), (2.0, 0.0), (0, 2.0)])
def test_clv_is_none_without_both_prices(taken, closing):
    assert SettlementBackfiller._clv(taken, closing) is None


# -- end-to-end backfill --------------------------------------------------
class _StubClient(BetExplorerClient):
    """In-memory BetExplorerClient that never touches the network."""

    def __init__(self, by_day):
        self.by_day = by_day
        self.calls_made = 0
        self.requested = []

    def get_matches(self, day):
        self.calls_made += 1
        self.requested.append(day)
        return self.by_day.get(day.isoformat(), [])


def _make_pick(storage, *, match_id="m1", sport_key="soccer_epl",
               home="Arsenal", away="Chelsea", outcome="Arsenal",
               best_odds=1.60, fair_odds=1.55, market="h2h",
               commence_days_ago=1):
    pick = Pick(
        match_id=match_id, sport_key=sport_key, home_team=home, away_team=away,
        commence_time=utcnow() - timedelta(days=commence_days_ago),
        market=market, outcome_name=outcome, p_true=0.66, fair_odds=fair_odds,
        n_books=3, stdev=0.01, cv=0.02,
        best_execution=Execution("book1", "Book1", best_odds, 0.05),
        state="TRIGGER_ALERT", created_at=utcnow(),
    )
    storage.insert_pick(pick)
    return pick


def test_backfill_settles_winning_pick_and_writes_clv():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage)
    client = _StubClient({day.isoformat(): [
        _fixture(score=(2, 1), odds_home=1.55, odds_draw=4.5, odds_away=6.0)
    ]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)

    assert report.settled == 1 and report.won == 1
    row = storage.list_settled_picks()[0]
    assert row["result"] == "WIN"
    # Taken 1.60 against a 1.55 close => +3.2% CLV.
    assert row["clv"] == pytest.approx(1.60 / 1.55 - 1.0, abs=1e-9)
    assert row["closing_odds"] == pytest.approx(1.55)


def test_backfill_settles_losing_pick():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage, outcome="Chelsea")
    client = _StubClient({day.isoformat(): [
        _fixture(score=(2, 1), odds_home=1.55, odds_draw=4.5, odds_away=6.0)
    ]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    assert report.settled == 1 and report.lost == 1
    assert storage.list_settled_picks()[0]["result"] == "LOSS"


def test_backfill_settles_draw_pick():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage, outcome="Draw")
    client = _StubClient({day.isoformat(): [
        _fixture(score=(2, 2), odds_home=1.55, odds_draw=4.5, odds_away=6.0)
    ]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    # A 2:2 draw settles the "Draw" leg as a WIN, not a loss.
    assert report.settled == 1 and report.won == 1
    assert storage.list_settled_picks()[0]["result"] == "WIN"


def test_backfill_dry_run_writes_nothing():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage)
    client = _StubClient({day.isoformat(): [_fixture(score=(2, 1), odds_home=1.55)]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=True)

    assert report.settled == 1              # would have settled
    assert report.clv_updated == 1
    assert storage.list_settled_picks() == []
    assert storage.list_pending_picks()[0]["result"] is None
    assert report.settled_picks[0]["dry_run"] is True


def test_backfill_refuses_to_settle_a_women_collision():
    """The headline safety rule: a senior pick must never grade off a women's
    fixture that happens to share most of the name."""
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage, home="Arsenal", away="Chelsea")
    client = _StubClient({day.isoformat(): [
        _fixture(home_team="Arsenal Women", away_team="Chelsea Women", score=(2, 1))
    ]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    assert report.settled == 0
    assert report.skipped_no_match == 1
    assert storage.list_settled_picks() == []


def test_backfill_ignores_unsupported_market():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage, market="totals", outcome="Over 2.5")
    client = _StubClient({day.isoformat(): [_fixture(score=(2, 1))]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    assert report.skipped_unsupported_market == 1
    assert report.considered == 0
    assert storage.list_settled_picks() == []


def test_backfill_skips_fixture_without_a_close():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage)
    client = _StubClient({day.isoformat(): [
        _fixture(status="", score=None, odds_home=None, odds_draw=None, odds_away=None)
    ]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    assert report.settled == 0 and report.skipped_not_final == 1
    assert storage.list_settled_picks() == []


def test_backfill_refuses_when_home_away_are_swapped():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage, home="Arsenal", away="Chelsea")
    client = _StubClient({day.isoformat(): [
        _fixture(home_team="Chelsea", away_team="Arsenal", score=(2, 1))
    ]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    assert report.settled == 0 and report.skipped_no_match == 1


def test_backfill_is_idempotent():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage)
    client = _StubClient({day.isoformat(): [_fixture(score=(2, 1), odds_home=1.55)]})
    backfiller = SettlementBackfiller(storage=storage, client=client)

    first = backfiller.run(dry_run=False)
    second = backfiller.run(dry_run=False)

    assert first.settled == 1
    # Re-running is a no-op: the row left the pending set, so there is nothing
    # left to consider and the ledger holds exactly one settled pick.
    assert second.considered == 0
    assert second.settled == 0
    assert second.clv_updated == 0
    assert len(storage.list_settled_picks()) == 1
    assert storage.list_settled_picks()[0]["result"] == "WIN"


def test_backfill_reports_ambiguity_instead_of_guessing():
    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage)
    twin = _fixture(event_id="e2", score=(0, 3), odds_home=1.55)
    client = _StubClient({day.isoformat(): [_fixture(score=(2, 1), odds_home=1.55), twin]})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    assert report.settled == 0
    assert report.skipped_ambiguous == 1
    assert storage.list_settled_picks() == []


def test_backfill_survives_a_failed_day_page():
    """One bad page must not abort the pass; the rest of the window still runs."""

    class _Flaky(_StubClient):
        def __init__(self, by_day, fail_on):
            super().__init__(by_day)
            self.fail_on = fail_on

        def get_matches(self, day):
            if day.isoformat() in self.fail_on:
                raise BetExplorerError("boom")
            return super().get_matches(day)

    day = (utcnow() - timedelta(days=2)).date()
    storage = InMemoryStorage()
    _make_pick(storage, commence_days_ago=2)
    client = _Flaky({day.isoformat(): [_fixture(score=(2, 1), odds_home=1.55)]},
                    fail_on={(day + timedelta(days=1)).isoformat()})

    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)

    # The slack day failed but the pick's own day still settled.
    assert len(report.errors) == 1
    assert "boom" in report.errors[0]
    assert report.settled == 1
    assert report.to_dict()["errors"] == report.errors


def test_backfill_survives_a_mid_pass_connection_reset():
    """ConnectionResetError must be recorded, not propagate and kill the run.

    A rate-limited or mid-handshake peer raises this and urllib does not wrap
    it, so it needs explicit handling to stay a per-day error.
    """
    class _Resetting(_StubClient):
        def get_matches(self, day):
            if day.isoformat() == "2020-01-01":
                raise ConnectionResetError(104, "Connection reset by peer")
            return super().get_matches(day)

    day = (utcnow() - timedelta(days=1)).date()
    storage = InMemoryStorage()
    _make_pick(storage)
    client = _Resetting({day.isoformat(): [_fixture(score=(2, 1), odds_home=1.55)]})

    # A raw reset (not wrapped) would escape the run() loop entirely.
    report = SettlementBackfiller(storage=storage, client=client).run(dry_run=False)
    assert report.settled == 1


def test_backfill_reports_no_pending_picks():
    report = SettlementBackfiller(
        storage=InMemoryStorage(), client=_StubClient({})).run()
    assert report.considered == 0
    assert report.to_dict()["considered"] == 0
