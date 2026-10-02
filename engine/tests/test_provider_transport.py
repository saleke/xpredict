"""Transport retry behaviour: real errors must survive to the caller.

A bug here is invisible until the network misbehaves, and it misreports *why*:
the retry loop used to fall through to the redirect check when the final
attempt raised, so ``status`` was never bound and Python raised
``UnboundLocalError``. That replaced the real transport error (connection
refused, TLS failure, timeout) with a meaningless name, and made a plain
network fault look like a provider bug. Production saw exactly that:

    fetch failed: openligadb/soccer_germany_bundesliga --
    unexpected UnboundLocalError: cannot access local variable 'status'
"""
from __future__ import annotations

import os
import urllib.parse

import pytest

from lisa.providers.base import ApiError, HttpTransport, SourceTier


def _transport(monkeypatch, behaviour) -> HttpTransport:
    t = HttpTransport(max_retries=2, time_budget=30.0)
    t._single = behaviour  # type: ignore[assignment]
    return t


def test_final_attempt_raises_the_real_transport_error(monkeypatch):
    calls = []

    def _always_fail(parts, headers, state, host):
        calls.append(host)
        raise OSError("connection refused")

    t = _transport(monkeypatch, _always_fail)
    with pytest.raises(ApiError) as exc:
        t.get_json("https://openligadb.de/getavailableleagues", provider="test")

    message = str(exc.value)
    assert "connection refused" in message, (
        "the underlying transport error must reach the caller, not be "
        "replaced by a bookkeeping error"
    )
    assert "UnboundLocalError" not in message
    assert "status" not in message
    # max_retries + 1 attempts.
    assert len(calls) == 3


def test_retry_then_succeed(monkeypatch):
    state = {"n": 0}

    def _flaky(parts, headers, st, host):
        state["n"] += 1
        if state["n"] < 3:
            raise OSError("temporary failure")
        return 200, {}, b'{"ok": true}'

    t = _transport(monkeypatch, _flaky)
    assert t.get_json("https://example.test/x", provider="test") == {"ok": True}
    assert state["n"] == 3


def test_api_error_from_transport_is_not_double_wrapped(monkeypatch):
    """A typed provider error must keep its type so callers can classify it."""
    def _raise_typed(parts, headers, state, host):
        raise ApiError("time budget exhausted", provider="test")

    t = _transport(monkeypatch, _raise_typed)
    with pytest.raises(ApiError) as exc:
        t.get_json("https://example.test/x", provider="test")
    assert str(exc.value) == "time budget exhausted"


def test_redirect_is_followed_and_terminates(monkeypatch):
    seen: list[str] = []

    def _redirect(parts, headers, state, host):
        url = urllib.parse.urlunsplit(parts)
        seen.append(url)
        if url.endswith("/nodash"):
            return 301, {"location": "https://example.test/withslash/"}, b""
        return 200, {}, b'{"ok": true}'

    t = _transport(monkeypatch, _redirect)
    assert t.get_json("https://example.test/nodash", provider="test") == {"ok": True}
    assert len(seen) >= 2
    assert seen[-1].endswith("/withslash/")


def test_time_budget_stops_a_slow_source(monkeypatch):
    def _slow(parts, headers, state, host):
        raise OSError("timeout")

    t = HttpTransport(max_retries=50, time_budget=0.0)
    t._single = _slow  # type: ignore[assignment]
    with pytest.raises(ApiError) as exc:
        t.get_json("https://example.test/x", provider="test")
    assert "budget" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Hard network errors: unreachable is not "briefly unhappy"
# ---------------------------------------------------------------------------


def test_unreachable_host_is_not_retried():
    """ENETUNREACH must cost one attempt, not four.

    The machine that produced this had lost IPv4 egress. OpenLigaDB is
    IPv4-only, so every cycle burned 4 x timeout on it and then reported a
    generic transport failure -- the same wall time whether or not the network
    could ever come back inside the request.
    """
    calls = []

    def _unreachable(parts, headers, state, host):
        calls.append(host)
        raise OSError(101, "Network is unreachable")

    t = HttpTransport(max_retries=3, time_budget=45.0)
    t._single = _unreachable  # type: ignore[assignment]
    with pytest.raises(ApiError) as exc:
        t.get_json("https://openligadb.de/getavailableleagues", provider="openligadb")

    assert len(calls) == 1, "an unroutable network must not be retried"
    assert "unreachable" in str(exc.value)


def test_unreachable_error_names_the_real_cause():
    """The message must be actionable, not a bare errno name.

    "Network is unreachable" repeated per provider reads like a provider
    outage. The operator needs the mismatch: an IPv4-only source on a host
    with no IPv4 route.
    """
    def _unreachable(parts, headers, state, host):
        raise OSError(101, "Network is unreachable")

    t = HttpTransport(max_retries=3)
    t._single = _unreachable  # type: ignore[assignment]
    with pytest.raises(ApiError) as exc:
        t.get_json("https://openligadb.de/getavailableleagues", provider="openligadb")

    message = str(exc.value)
    assert "openligadb.de" in message
    assert "route" in message.lower()


def test_transient_errors_are_still_retried():
    """Only genuinely hard network conditions skip the retry budget."""
    calls = []

    def _reset(parts, headers, state, host):
        calls.append(host)
        raise ConnectionResetError(104, "Connection reset by peer")

    t = HttpTransport(max_retries=3, time_budget=30.0)
    t._single = _reset  # type: ignore[assignment]
    with pytest.raises(ApiError):
        t.get_json("https://example.test/x", provider="test")
    assert len(calls) == 4, "a reset connection is transient and must be retried"


@pytest.mark.parametrize("code", [
    __import__("errno").ENETUNREACH,
    __import__("errno").ENETDOWN,
    __import__("errno").EHOSTUNREACH,
])
def test_errno_classification(code):
    from lisa.providers.base import is_hard_network_error
    assert is_hard_network_error(OSError(code, os.strerror(code)))


def test_errno_classification_sees_through_wrappers():
    """http.client and callers both wrap; the errno must survive the chain."""
    from lisa.providers.base import is_hard_network_error

    original = OSError(101, "Network is unreachable")
    wrapped = ValueError("could not fetch")
    wrapped.__cause__ = original

    assert is_hard_network_error(wrapped)


def test_non_network_errors_are_not_hard():
    from lisa.providers.base import is_hard_network_error
    assert not is_hard_network_error(ValueError("bad json"))
    assert not is_hard_network_error(OSError(2, "No such file or directory"))


def test_stale_connection_is_not_hard():
    """A pooled socket the peer closed is recoverable, so it must still retry."""
    from lisa.providers.base import is_hard_network_error
    assert not is_hard_network_error(
        OSError(104, "Connection reset by peer"))
