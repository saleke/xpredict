"""Shared test fixtures: a fixed clock, fixture client, in-memory storage."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lisa import config as cfg
from lisa.client import FixtureClient
from lisa.fixtures import FIXTURE_SPORTS, ODDS_PAYLOADS, SCORES_PAYLOADS
from lisa.notify import Notifier
from lisa.pipeline import Pipeline
from lisa.storage import InMemoryStorage

# Fixed clock so every test is deterministic regardless of wall time.
NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


class CollectNotifier(Notifier):
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def settings() -> cfg.Settings:
    return cfg.Settings(sports=FIXTURE_SPORTS)


@pytest.fixture
def fixture_client() -> FixtureClient:
    return FixtureClient(ODDS_PAYLOADS, SCORES_PAYLOADS)


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def notifier() -> CollectNotifier:
    return CollectNotifier()


@pytest.fixture
def pipeline(fixture_client, storage, settings, notifier):
    return Pipeline(fixture_client, storage, settings, notifier=notifier)


def insert_postponed_pick(storage, match_id: str = "nba-g", home: str = "Spurs",
                          away: str = "Rockets", outcome: str = "Spurs") -> None:
    """Record a pick that was legitimately emitted *before* its fixture kicked off.

    ``nba-g`` is the postponed fixture: ingestion correctly refuses to mint a
    pre-game pick for a match that already commenced, so the settlement tests
    insert this row directly to represent the pre-kickoff alert that later went
    unplayed.
    """
    from lisa.gate import Pick
    from lisa.odds import utcnow

    storage.insert_pick(Pick(
        match_id=match_id, sport_key="basketball_nba", home_team=home,
        away_team=away, commence_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
        market="h2h", outcome_name=outcome, p_true=0.84, fair_odds=1.19,
        n_books=5, stdev=0.01, cv=0.01, best_execution=None,
        state="TRIGGER_ALERT", created_at=utcnow(),
    ))