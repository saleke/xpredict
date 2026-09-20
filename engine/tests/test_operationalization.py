"""Unit and integration tests for Milestone 5: Operationalization & Commercial Architecture.

Tests cover:
1. Hardened Ingestion Validator (American odds rejection, degenerate prices, malformed game structures).
2. Conviction Scoring Formula.
3. Volume Controller & Waiting-Room Node (throttling alerts while preserving ledger records).
4. Dynamic Volatility Switch & Slippage / Freshness Guard.
5. Production Notifiers (Telegram, Discord, Composite) with resilient error handling.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from lisa import config as cfg
from lisa.gate import (
    Execution, Pick, PickFreshness, compute_conviction_score,
    evaluate_pick_freshness,
)
from lisa.notify import (
    CompositeNotifier, DiscordNotifier, LogNotifier, Notifier,
    TelegramNotifier, pick_alert_text,
)
from lisa.odds import Book, Match
from lisa.parsing import (
    parse_odds_payload, validate_market_outcomes, validate_raw_game,
)
from lisa.pipeline import Pipeline
from lisa.storage import InMemoryStorage


# -- 1. Ingestion Validation Tests -------------------------------------------


def test_validate_raw_game_rejects_malformed_structures() -> None:
    # Not a dict
    ok, reason = validate_raw_game("not-a-dict")
    assert not ok and reason == "payload_not_dict"

    # Missing id
    ok, reason = validate_raw_game({
        "sport_key": "basketball_nba", "home_team": "A", "away_team": "B",
        "commence_time": "2026-09-20T20:00:00Z", "bookmakers": []
    })
    assert not ok and "missing_or_empty_id" in reason

    # Invalid ISO commence time
    ok, reason = validate_raw_game({
        "id": "m1", "sport_key": "basketball_nba", "home_team": "A", "away_team": "B",
        "commence_time": "not-a-date", "bookmakers": []
    })
    assert not ok and "invalid_or_missing_commence_time" in reason

    # Bookmakers not a list
    ok, reason = validate_raw_game({
        "id": "m1", "sport_key": "basketball_nba", "home_team": "A", "away_team": "B",
        "commence_time": "2026-09-20T20:00:00Z", "bookmakers": "none"
    })
    assert not ok and "bookmakers_not_list" in reason


def test_validate_market_outcomes_filters_american_and_degenerate_odds() -> None:
    # American odds: -110, -105 (all negative prices filtered)
    raw_american = [
        {"name": "TeamA", "price": -110},
        {"name": "TeamB", "price": -105},
    ]
    ok, clean, line, err = validate_market_outcomes("h2h", "TeamA", raw_american)
    assert not ok
    assert err == "no_valid_outcomes"

    # Degenerate odds: 0.5, 0.0, 1.0 (all <= 1.0 filtered)
    raw_degenerate = [
        {"name": "TeamA", "price": 0.0},
        {"name": "TeamB", "price": 1.0},
    ]
    ok, clean, line, err = validate_market_outcomes("h2h", "TeamA", raw_degenerate)
    assert not ok

    # Mixed American and decimal odds: negative is dropped, valid decimal survives
    raw_mixed = [
        {"name": "TeamA", "price": -110},
        {"name": "TeamB", "price": 2.05},
    ]
    ok, clean, line, err = validate_market_outcomes("h2h", "TeamA", raw_mixed)
    assert ok
    assert clean == {"TeamB": 2.05}

    # Valid decimal odds
    raw_valid = [
        {"name": "TeamA", "price": 1.91, "point": -3.5},
        {"name": "TeamB", "price": 1.91, "point": 3.5},
    ]
    ok, clean, line, err = validate_market_outcomes("spreads", "TeamA", raw_valid)
    assert ok
    assert clean == {"TeamA": 1.91, "TeamB": 1.91}
    assert line == -3.5


# -- 2. Conviction Scoring Tests ---------------------------------------------


def test_compute_conviction_score_properties() -> None:
    # High certainty (85%), tight consensus (CV 1.5%), positive EV (+4%)
    high = compute_conviction_score(p_true=0.85, threshold=0.75, cv=0.015, ev=0.04)
    # (0.10 / 0.015) * 1.04 = 6.6667 * 1.04 = 6.9333
    assert high > 6.0

    # Borderline certainty (76%), loose consensus (CV 8.0%), negative EV (-2%)
    borderline = compute_conviction_score(p_true=0.76, threshold=0.75, cv=0.08, ev=-0.02)
    # (0.01 / 0.08) * 1.00 = 0.125
    assert borderline < 0.5
    assert high > borderline * 10


# -- 3. Volume Controller & Waiting-Room Node Tests --------------------------


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def test_volume_controller_throttles_alerts_and_preserves_ledger() -> None:
    """Verify that when multiple picks qualify, all are inserted into cold storage,
    but only the top-conviction picks up to max_alerts_per_cycle are alerted.
    """
    commence = datetime(2026, 9, 20, 20, 0, 0, tzinfo=timezone.utc)
    t_now = datetime(2026, 9, 20, 18, 0, 0, tzinfo=timezone.utc)

    def _make_game(gid: str, p_team_a_odds: float) -> dict:
        return {
            "id": gid,
            "sport_key": "basketball_nba",
            "home_team": f"TeamA_{gid}",
            "away_team": f"TeamB_{gid}",
            "commence_time": commence.isoformat(),
            "bookmakers": [
                {"key": f"book_{i}", "title": f"Book {i}", "markets": [{
                    "key": "h2h", "outcomes": [
                        {"name": f"TeamA_{gid}", "price": p_team_a_odds},
                        {"name": f"TeamB_{gid}", "price": 5.0},
                    ]
                }]}
                for i in range(5)
            ],
        }

    # 4 games all favoring Team A, but with varying certainty:
    # Game 1: odds 1.15 (highest certainty ~85%)
    # Game 2: odds 1.20 (certainty ~81%)
    # Game 3: odds 1.25 (certainty ~77%)
    # Game 4: odds 1.28 (certainty ~75.5%)
    games = [
        _make_game("g1", 1.15),
        _make_game("g2", 1.20),
        _make_game("g3", 1.25),
        _make_game("g4", 1.28),
    ]

    storage = InMemoryStorage()
    notifier = RecordingNotifier()
    # Configure throttle to maximum 2 alerts per cycle
    settings = cfg.Settings(
        sports=("basketball_nba",),
        gate_threshold=0.74,
        max_alerts_per_cycle=2,
        max_alerts_per_sport_cycle=2,
    )

    class MockClient:
        def get_odds(self, sport, regions="eu,us", markets="h2h"):
            return games

    pipeline = Pipeline(client=MockClient(), storage=storage, settings=settings, notifier=notifier)
    reports = pipeline.run_cycle(now=t_now)
    rep = reports[0]

    # All 4 picks passed the certainty gate and were inserted into storage!
    assert rep.picks_emitted == 4
    assert len(storage.list_pending_picks()) == 4

    # BUT the Volume Controller throttled notifications to exactly 2 alerts!
    assert len(notifier.sent) == 2

    # The 2 alerts sent were the highest conviction: g1 and g2!
    assert "TeamA_g1" in notifier.sent[0]
    assert "TeamA_g2" in notifier.sent[1]

    # g3 and g4 were recorded as suppressed due to waiting room quota
    assert any("g3:waiting_room_sport_quota" in s for s in rep.suppressed)
    assert any("g4:waiting_room_sport_quota" in s for s in rep.suppressed)


# -- 4. Dynamic Volatility Switch & Slippage Guard Tests ----------------------


def test_evaluate_pick_freshness_lifecycle() -> None:
    commence = datetime(2026, 9, 20, 20, 0, 0, tzinfo=timezone.utc)
    t_prematch = datetime(2026, 9, 20, 19, 0, 0, tzinfo=timezone.utc)
    t_postkick = datetime(2026, 9, 20, 20, 30, 0, tzinfo=timezone.utc)

    pick = Pick(
        match_id="m-fresh",
        sport_key="soccer_epl",
        home_team="Arsenal",
        away_team="Chelsea",
        commence_time=commence,
        market="h2h",
        outcome_name="Arsenal",
        line=None,
        p_true=0.80,
        fair_odds=1.25,
        n_books=5,
        stdev=0.01,
        cv=0.02,
        best_execution=Execution(book_key="pinnacle", book_title="Pinnacle", odds=1.35, ev=0.08),
        state="TRIGGER_ALERT",
        created_at=t_prematch,
    )

    # 1. Steady market (odds 1.35): FRESH
    m_steady = Match(
        id="m-fresh", sport_key="soccer_epl", commence_time=commence,
        home_team="Arsenal", away_team="Chelsea", completed=False, market="h2h",
        bookmakers=(
            Book("pinnacle", "Pinnacle", t_prematch, {"Arsenal": 1.35, "Chelsea": 3.8}),
            Book("b2", "B2", t_prematch, {"Arsenal": 1.34, "Chelsea": 3.9}),
        ),
    )
    f_steady = evaluate_pick_freshness(pick, m_steady, now=t_prematch)
    assert f_steady.status == "FRESH"
    assert f_steady.current_odds == 1.35
    assert f_steady.current_ev == pytest.approx(0.08)

    # 2. Minor price slippage (odds 1.30 > fair_odds 1.25): SLIPPED_POSITIVE_EV
    m_slipped = Match(
        id="m-fresh", sport_key="soccer_epl", commence_time=commence,
        home_team="Arsenal", away_team="Chelsea", completed=False, market="h2h",
        bookmakers=(
            Book("pinnacle", "Pinnacle", t_prematch, {"Arsenal": 1.30, "Chelsea": 4.0}),
            Book("b2", "B2", t_prematch, {"Arsenal": 1.29, "Chelsea": 4.1}),
        ),
    )
    f_slipped = evaluate_pick_freshness(pick, m_slipped, now=t_prematch)
    assert f_slipped.status == "SLIPPED_POSITIVE_EV"
    assert f_slipped.current_ev == pytest.approx(0.04)

    # 3. Severe price crash (odds 1.20 < fair_odds 1.25): DECAYED_NEGATIVE_EV
    m_decayed = Match(
        id="m-fresh", sport_key="soccer_epl", commence_time=commence,
        home_team="Arsenal", away_team="Chelsea", completed=False, market="h2h",
        bookmakers=(
            Book("pinnacle", "Pinnacle", t_prematch, {"Arsenal": 1.20, "Chelsea": 5.0}),
            Book("b2", "B2", t_prematch, {"Arsenal": 1.20, "Chelsea": 5.1}),
        ),
    )
    f_decayed = evaluate_pick_freshness(pick, m_decayed, now=t_prematch)
    assert f_decayed.status == "DECAYED_NEGATIVE_EV"
    assert f_decayed.current_ev == pytest.approx(-0.04)
    assert "Edge extinguished" in (f_decayed.warning or "")

    # 4. Volatility spike: Pinnacle crashed to 1.15, but B2 is lagging at 1.45 (CV jumps)
    m_spike = Match(
        id="m-fresh", sport_key="soccer_epl", commence_time=commence,
        home_team="Arsenal", away_team="Chelsea", completed=False, market="h2h",
        bookmakers=(
            Book("pinnacle", "Pinnacle", t_prematch, {"Arsenal": 1.15, "Chelsea": 5.5}),
            Book("b2", "B2", t_prematch, {"Arsenal": 1.45, "Chelsea": 3.0}),
        ),
    )
    f_spike = evaluate_pick_freshness(pick, m_spike, now=t_prematch, max_cv_drift=0.03)
    assert f_spike.status == "VOLATILITY_SPIKE"
    assert "Consensus volatility spiked" in (f_spike.warning or "")

    # 5. Expired after kickoff
    f_expired = evaluate_pick_freshness(pick, m_steady, now=t_postkick)
    assert f_expired.status == "EXPIRED"


# -- 5. Notifiers Tests ------------------------------------------------------


def test_telegram_and_discord_notifiers() -> None:
    # Test Telegram notifier formatting and mocked HTTP request
    tg = TelegramNotifier(token="bot123", chat_id="@channel")
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_open.return_value.__enter__.return_value = mock_resp

        tg.send("Hello Telegram Alert")
        assert mock_open.called
        req = mock_open.call_args[0][0]
        assert "api.telegram.org/botbot123/sendMessage" in req.full_url
        assert b"chat_id=%40channel" in req.data

    # Test Discord notifier formatting
    disc = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/test")
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{}'
        mock_open.return_value.__enter__.return_value = mock_resp

        disc.send("Hello Discord Alert")
        assert mock_open.called
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://discord.com/api/webhooks/test"
        payload = json.loads(req.data.decode("utf-8"))
        assert "Hello Discord Alert" in payload["content"]

    # Test CompositeNotifier error isolation (one failing does not halt others)
    bad_tg = TelegramNotifier(token="fake", chat_id="fake")
    rec = RecordingNotifier()
    comp = CompositeNotifier([bad_tg, rec])
    with patch("urllib.request.urlopen", side_effect=RuntimeError("Network down")):
        comp.send("Test fanout")
    # RecordingNotifier still received the alert!
    assert rec.sent == ["Test fanout"]


def test_pick_alert_text_includes_conviction() -> None:
    pick = Pick(
        match_id="m1", sport_key="nba", home_team="Lakers", away_team="Warriors",
        commence_time=datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc),
        market="h2h", outcome_name="Lakers", p_true=0.82, fair_odds=1.22,
        n_books=5, stdev=0.01, cv=0.015, best_execution=None,
        state="TRIGGER_ALERT", created_at=datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc),
        conviction_score=4.67,
    )
    text = pick_alert_text(pick)
    assert "LISA ALERT [Conviction: 4.7]: Lakers vs Warriors" in text
