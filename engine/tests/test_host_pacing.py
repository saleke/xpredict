"""Rate pacing must outlive the cycle that earned it.

A provider's budget belongs to the provider. football-data.org publishes ten
requests a minute; that is ten across everything LISA does, not ten per cycle.

The bug this pins down is a lifetime error, so it is invisible in any test that
only ever runs one cycle. With the bucket scoped to a transport -- and the
server builds a fresh transport for every board cycle -- each cycle opened with
a full burst allowance. Two cycles a minute apart then fired burst+burst
requests inside the provider's *sliding* window and earned a 429 that in-cycle
pacing could never prevent. Widening the per-call retry budget would not have
fixed it either: the requests were correctly spaced and still collectively
over the limit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.providers.base import (  # noqa: E402
    HttpTransport,
    TokenBucket,
    reset_host_pacing,
)


@pytest.fixture(autouse=True)
def _clean_pacing():
    """Each test starts with an empty meter.

    The registry is process-wide by design, so without this a test would
    inherit a real cycle's accumulated pacing and spend the suite's wall time
    waiting for tokens nobody else is using.
    """
    reset_host_pacing()
    yield
    reset_host_pacing()


def test_two_transports_share_one_meter_per_host() -> None:
    cycle_one = HttpTransport()
    cycle_one.set_rate_limit("api.football-data.org", 10 / 60, 4)
    cycle_two = HttpTransport()
    cycle_two.set_rate_limit("api.football-data.org", 10 / 60, 4)

    assert cycle_one._hosts["api.football-data.org"].bucket is \
        cycle_two._hosts["api.football-data.org"].bucket, (
        "a second cycle got its own pacing state, so it can spend the same "
        "minute's budget a second time")


def test_a_repeated_declaration_does_not_reset_the_meter() -> None:
    """Re-declaring the same rate must not hand back a full burst."""
    transport = HttpTransport()
    transport.set_rate_limit("api.football-data.org", 10 / 60, 4)

    bucket = transport._hosts["api.football-data.org"].bucket
    bucket.acquire()  # spend one
    before = bucket._tokens

    transport.set_rate_limit("api.football-data.org", 10 / 60, 4)
    after = transport._hosts["api.football-data.org"].bucket._tokens

    assert after <= before, (
        "declaring the same rate again refilled the bucket, discarding the "
        "pacing the first declaration earned")


def test_a_genuinely_changed_rate_does_replace_the_meter() -> None:
    """A reconfigured budget must take effect rather than be ignored."""
    transport = HttpTransport()
    transport.set_rate_limit("api.football-data.org", 10 / 60, 4)
    first = transport._hosts["api.football-data.org"].bucket

    transport.set_rate_limit("api.football-data.org", 1.0, 4)
    second = transport._hosts["api.football-data.org"].bucket

    assert second is not first
    assert second.rate == pytest.approx(1.0)


def test_different_hosts_do_not_share_a_meter() -> None:
    transport = HttpTransport()
    transport.set_rate_limit("api.football-data.org", 10 / 60, 4)
    transport.set_rate_limit("openligadb.de", 1.0, 4)

    assert transport._hosts["api.football-data.org"].bucket is not \
        transport._hosts["openligadb.de"].bucket, (
        "one host's pacing state leaked onto another host")


def test_a_different_rate_gets_its_own_meter() -> None:
    """Two budgets for one host must not contaminate each other."""
    transport = HttpTransport()
    transport.set_rate_limit("host", 10 / 60, 4)
    transport.set_rate_limit("host", 1.0, 4)

    assert transport._hosts["host"].bucket.rate == pytest.approx(1.0)


def test_reset_host_pacing_clears_one_host_only() -> None:
    transport = HttpTransport()
    transport.set_rate_limit("a.example", 0.1, 2)
    transport.set_rate_limit("b.example", 0.1, 2)

    reset_host_pacing("a.example")

    fresh = HttpTransport()
    fresh.set_rate_limit("b.example", 0.1, 2)
    # b keeps its history; a starts over.
    assert fresh._hosts["b.example"].bucket is \
        transport._hosts["b.example"].bucket


def test_the_meter_still_meters_a_single_transport(monkeypatch) -> None:
    """Sharing must not have broken the pacing itself."""
    clock = {"t": 0.0}
    monkeypatch.setattr('lisa.providers.base.budget_sleep',
                        lambda seconds: clock.update(t=clock['t'] + seconds))
    bucket = TokenBucket(rate_per_sec=1.0, burst=1.0,
                         monotonic=lambda: clock["t"])
    first = bucket.acquire()
    assert first == pytest.approx(0.0), "the first request should be immediate"

    started = bucket.acquire()
    assert started == pytest.approx(1.0), (
        "the second request should have waited a full second")
