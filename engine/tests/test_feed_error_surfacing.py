"""A broken cycle must say *why* it was broken.

Regression: when no finished results arrived, ``run_feed`` returned early with
only ``"no finished results available; model not fitted"``. The provider errors
were collected further down the function, on the success path, so the early
return skipped them entirely.

The consequence in production: every fetch had failed with "Network is
unreachable" and the API reported a *modelling* problem. The symptom was
reported and the cause was thrown away, which sends an operator to debug the
model instead of the network.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone

from lisa.feed import FeedReport, ProviderSet, ProviderStatus, run_feed


def _settings(**overrides):
    base = {
        "board_leagues": ["soccer_germany_bundesliga"],
        "board_window_hours": 48.0,
        "board_volume_target": 12,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FailingProvider:
    """A calendar source whose every call raises, as a dead network would."""

    name = "openligadb"

    def available_leagues(self):
        return []

    def leagues(self):
        return ('soccer_germany_bundesliga',)

    def get_fixtures(self, sport_key):
        raise OSError(101, "Network is unreachable")

    def get_matchdata(self, sport_key):
        raise OSError(101, "Network is unreachable")


def _dead_provider_set(error: str) -> ProviderSet:
    status = ProviderStatus(
        name="openligadb", configured=True, used=True, error=error,
        leagues=("soccer_germany_bundesliga",),
    )
    return ProviderSet(calendar=[_FailingProvider()], statuses=[status])


def test_provider_failure_is_reported_when_the_model_cannot_fit():
    report = run_feed(
        _settings(),
        providers=_dead_provider_set(
            "openligadb.de is unreachable from this host: ENETUNREACH"),
        now=datetime(2026, 9, 29, tzinfo=timezone.utc),
    )

    assert report.board is None
    joined = " | ".join(report.errors)

    # The symptom, as before.
    assert "model not fitted" in joined
    # The cause, which is the whole point of this test.
    assert "openligadb" in joined
    assert "unreachable" in joined


def test_unconfigured_provider_is_not_reported_as_an_error():
    """A source that was never set up is absent, not broken."""
    report = FeedReport(began=datetime.now(timezone.utc), window_hours=48.0)
    report.providers = [
        ProviderStatus(name="a", configured=True, used=False, error="boom"),
        ProviderStatus(name="b", configured=True, used=True, error="ignored"),
        ProviderStatus(name="c", configured=False, used=False, error="absent"),
    ]

    from lisa.feed import _collect_provider_errors
    _collect_provider_errors(report)

    assert report.errors == ["a: boom"]


def test_health_is_broken_and_never_reports_zero_errors():
    report = run_feed(
        _settings(),
        providers=_dead_provider_set("openligadb.de is unreachable"),
        now=datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    assert report.health() == "broken"
    assert report.errors, "a broken cycle must never report zero errors"
