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