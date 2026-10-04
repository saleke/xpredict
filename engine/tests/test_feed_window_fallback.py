"""The look-ahead window is a target, not a ceiling.

An international week leaves a 24-hour window genuinely empty. The feed already
fetches everything the calendar holds, so widening the window costs no requests
-- refusing to look further is a pure loss, and publishing an empty board while
the next day's fixtures sit unread in the same response is a coverage problem
that does not exist.

The effective horizon must describe the fixtures actually published. Choose it
before pricing and board construction, without a fixed 48-hour ceiling.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.feed import run_feed  # noqa: E402
from lisa.config import Settings

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _row(kickoff: datetime, *, completed: bool = False,
         home: str = "Sao Paulo FC", away: str = "Santos FC") -> dict[str, Any]:
    """A calendar row in the shape ``to_scored`` and ``to_fixtures`` read.

    Both key names matter: a row using ``home`` instead of ``home_team`` is
    silently dropped by both, so a fixture that never reaches the board looks
    exactly like a fixture that does not exist.
    """
    return {
        "match_id": f"football_data:{kickoff.isoformat()}:{home}",
        "sport_key": "soccer_brazil_serie_a",
        "home_team": home, "away_team": away,
        "kickoff": kickoff.isoformat(), "epoch": kickoff.timestamp(),
        "completed": completed,
        "home_score": 1, "away_score": 0,
        "provider": "football_data", "league": "soccer_brazil_serie_a",
    }


class _StubProviders:
    """Just the shape ``run_feed`` touches, so no network is involved."""

    def __init__(self) -> None:
        self.statuses: list[Any] = []
        self.prices: list[Any] = []
        self.calendar: list[Any] = []


class _Counter:
    """Counts calendar fetches so a wasted second read cannot pass unnoticed."""

    def __init__(self) -> None:
        self.fetches = 0


def _patch_calendar(monkeypatch, rows: list[dict[str, Any]]) -> _Counter:
    counter = _Counter()

    def _fake(providers, leagues, statuses, **kw):
        counter.fetches += 1
        return {league: list(rows) for league in leagues}

    monkeypatch.setattr("lisa.feed.fetch_calendar", _fake)
    return counter


class _StubModel:
    sufficient = True

    class _Report:
        sufficient = True
        matches_used = 0
        teams = 2
        mean_games_behind = 30.0
        prior_dominance = 0.1

        def to_dict(self):
            return {"sufficient": True, "matches_used": self.matches_used,
                    "teams": self.teams}

    report = _Report()

    def fit(self, matches, as_of=None):
        self.report.matches_used = len(list(matches))
        return self.report

    def knows(self, team):
        # The board only publishes a fixture both of whose clubs have a rating.
        # Claiming every team is known keeps this suite about the window rather
        # than about rating coverage.
        return True

    def predict(self, home, away):
        return {
            "p_home": 0.5, "p_draw": 0.3, "p_away": 0.2, "p_btts": 0.5,
            "over": {"2.5": 0.5},
            "double_chance": {}, "home_team_over": {}, "away_team_over": {},
            "expected_goals": {"home": 1.5, "away": 1.0},
            "most_likely_scores": [{"home_goals": 1, "away_goals": 0, "p": 0.2}],
        }


def _settings(**over):
    """Settings as an *object*, because run_feed reads them with getattr.

    A dict is silently ignored here: getattr on a dict misses every key, so the
    cycle quietly falls back to the default league list and builds its board from
    leagues the test never mentioned. Every assertion below would then be about
    the wrong fixtures while still looking like it passed.
    """
    base = {
        "board_leagues": ("soccer_brazil_serie_a",),
        "board_volume_target": 12,
        "enable_sharpapi": False,
        "sharpapi_max_pages": 1,
    }
    base.update(over)
    return Settings(**base)


def _run(monkeypatch, rows, window, target=12):
    # A cycle with no finished result never reaches the board at all: it returns
    # early reporting "model not fitted". Every scenario here is about the
    # window, so give each one one training row to fit against -- well outside
    # any window under test, so it can never become a fixture.
    rows = list(rows) + [_row(_at(-72), completed=True,
                              home="Training Home", away="Training Away")]
    counter = _patch_calendar(monkeypatch, rows)
    providers = _StubProviders()
    report = run_feed(_settings(board_volume_target=target), providers=providers,
                      now=NOW, window_hours=window, model=_StubModel())
    return report, counter


def _at(hours: float) -> datetime:
    return NOW + timedelta(hours=hours)


# ---------------------------------------------------------------------------


def test_a_window_that_meets_the_target_is_never_widened(monkeypatch) -> None:
    rows = [_row(_at(h)) for h in (2, 6, 10, 14, 18, 22)]
    report, providers = _run(monkeypatch, rows, 24.0)

    assert report.window_hours == 24.0
    assert report.board.coverage.fixtures_modelled == 6
    assert not any("Widened" in n for n in report.notes), (
        f"the window was widened despite meeting the target: {report.notes}")


def test_an_empty_window_widens_and_finds_the_next_days_fixtures(monkeypatch) -> None:
    # Nothing in 24h; three matches on day two.
    rows = [_row(_at(h), home=f"Home {h}", away=f"Away {h}") for h in (26, 30, 40)]

    report, _ = _run(monkeypatch, rows, 24.0)

    assert report.window_hours == 40.0
    assert report.board.coverage.fixtures_modelled == 3
    assert report.board.winning, "widening found fixtures but published none"


def test_widening_spends_no_additional_requests(monkeypatch) -> None:
    """The rows are already in hand. A second fetch would waste the budget."""
    rows = [_row(_at(h), home=f"H{h}", away=f"A{h}") for h in (26, 30, 40)]

    _, counter = _run(monkeypatch, rows, 24.0)

    assert counter.fetches == 1, "widening the window re-fetched the calendar"


def test_the_widening_is_reported_rather_than_done_silently(monkeypatch) -> None:
    rows = [_row(_at(30), home="H", away="A")]

    report, _ = _run(monkeypatch, rows, 24.0)

    note = " ".join(report.notes)
    assert "Widened the look-ahead" in note
    assert "24h to 30h" in note
    # The note must carry the counts that justified it, so an operator can tell
    # whether the widening helped.
    assert "0 fixture(s) against a target of 12" in note
    assert "holds 1" in note


def test_a_48_hour_window_reaches_the_nearest_later_fixture(monkeypatch) -> None:
    rows = [_row(_at(70), home="H", away="A")]

    report, _ = _run(monkeypatch, rows, 48.0)

    assert report.window_hours == 70.0
    assert report.board.coverage.fixtures_modelled == 1


def test_widening_is_not_adopted_when_it_finds_nothing_extra(monkeypatch) -> None:
    """Same fixture count, different window: the operator's choice stands.

    Adopting it would replace an accurate coverage note -- which names the 24h
    window the operator configured -- with one describing a window they never
    asked for, to look for fixtures that are not there.
    """
    rows = [_row(_at(2))]  # inside 24h, so the wide window adds nothing

    report, _ = _run(monkeypatch, rows, 24.0)

    assert report.window_hours == 24.0
    assert report.board.coverage.fixtures_modelled == 1
    assert not any("Widened" in n for n in report.notes), (
        f"it widened even though the wide window found nothing extra: {report.notes}")


def test_completed_matches_are_never_pulled_in_by_widening(monkeypatch) -> None:
    """A finished match is a training row, not a fixture. Widening must not
    promote one onto the board."""
    rows = [_row(_at(2), completed=True), _row(_at(30), completed=True)]

    report, _ = _run(monkeypatch, rows, 24.0)

    assert report.board.coverage.fixtures_modelled == 0
    assert not report.board.winning


def test_the_reported_window_matches_the_board_that_was_built(monkeypatch) -> None:
    """A report claiming 48h over a board built from 24h rows is a lie."""
    rows = [_row(_at(h), home=f"H{h}", away=f"A{h}") for h in (26, 30, 40)]

    report, _ = _run(monkeypatch, rows, 24.0)

    assert report.window_hours == report.board.window_hours, (
        "the report and the board disagree about the window they cover")


def test_every_published_fixture_really_lies_inside_the_reported_window(
        monkeypatch) -> None:
    """The widened board must not contain a match outside the window it names.

    This is the failure a wrong window produces: fixtures computed over a wider
    span than the report admits, so the coverage count and the published rows
    describe different things.
    """
    rows = [_row(_at(h), home=f"H{h}", away=f"A{h}") for h in (26, 30, 40)]

    report, _ = _run(monkeypatch, rows, 24.0)

    horizon = NOW + timedelta(hours=report.window_hours)
    published = [o for o in report.board.winning + report.board.earning]
    assert published, "the widened board published nothing, so this proves nothing"
    for row in published:
        assert NOW <= row.kickoff <= horizon, (
            f"{row.home} v {row.away} at {row.kickoff} is outside the "
            f"{report.window_hours:.0f}h window the report names")


def test_widening_does_not_spend_a_second_price_fetch(monkeypatch) -> None:
    """Prices cost real requests out of a twelve-a-minute budget.

    Widening is meant to be free. If it re-read prices, a board that was going
    to cost one page would cost two, for a result the operator did not ask for.
    """
    rows = [_row(_at(h), home=f"H{h}", away=f"A{h}") for h in (26, 30, 40)]

    calls = []

    class _CountingPriceSource:
        name = "counting"

        def is_available(self):
            return True

        def fetch(self, **kw):
            calls.append(kw)
            raise RuntimeError("no network in this test")

    providers = _StubProviders()
    providers.prices = [_CountingPriceSource()]
    _patch_calendar(monkeypatch, list(rows) + [
        _row(_at(-72), completed=True, home="TH", away="TA")])

    run_feed(_settings(), providers=providers, now=NOW, window_hours=24.0,
             model=_StubModel())

    assert len(calls) <= 1, (
        f"the price source was read {len(calls)} times for one cycle")
