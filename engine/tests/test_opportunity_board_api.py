"""The free-stack opportunity board is reachable over HTTP.

``engine/lisa/board.py`` was previously dead code: ``run_feed`` was called by
nothing, so the winning/earning ladders, micro-bets and accumulators were
computed and thrown away. These tests pin the route that makes them reachable,
and the caching and failure behaviour that keeps it from becoming a way to
hammer three public endpoints on every dashboard poll.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import pytest

from lisa import server as server_mod
from lisa.board import Accumulator, Board, BoardCoverage, Opportunity
from lisa.server import make_production_server
from lisa.storage import InMemoryStorage, SqliteStorage
from lisa.config import Settings
from lisa.feed import FeedReport, ProviderSet


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _board_payload() -> Board:
    kickoff = datetime.now(timezone.utc) + timedelta(hours=2)
    leg = Opportunity(
        match_id="m1", sport_key="soccer_germany_bundesliga", kickoff=kickoff,
        home="Home", away="Away", market="h2h", selection="Home",
        p_model=0.55, fair_odds=1.82,
        best_odds=None, best_book=None, ev=None,
        stake_fraction=0.0, priced=False, basis="model_only",
        reason="no market price",
    )
    return Board(
        generated_at=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        window_hours=48.0,
        unproven=False,
        model_report={"sufficient": True, "matches_used": 702},
        winning=(leg,),
        micro_bets=(),
        accumulators=(Accumulator(
            legs=(leg,), p_naive=0.55, p_adjusted=0.50, fair_odds=2.0,
            best_odds=None, ev=None, priced=False, correlation_penalty=0.91,
        ),),
        coverage=BoardCoverage(
            window_hours=48.0, fixtures_seen=0, fixtures_modelled=0,
            fixtures_priced=0, meets_volume_target=False, volume_target=12,
            notes=["Volume short: 0 modelled fixtures against a target of 12."],
        ),
        notes=[],
    )


@pytest.fixture
def live_server(monkeypatch, tmp_path):
    """A running server whose feed is stubbed, so tests never hit the network."""
    calls: list[int] = []
    board = _board_payload()

    def _fake_run_feed(settings, **kwargs):
        calls.append(1)
        return FeedReport(began=datetime.now(timezone.utc), board=board, forecast={"matches": [], "count": 0})

    import lisa.feed as feed_mod
    monkeypatch.setattr(feed_mod, "run_feed", _fake_run_feed)

    port = _free_port()
    storage = SqliteStorage(str(tmp_path / "test.db"))
    httpd = make_production_server(port=port, storage=storage, settings=Settings())
    httpd.daily_service._providers = ProviderSet()
    httpd.daily_service.tick()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", calls
    httpd.shutdown()
    httpd.server_close()


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_board_route_serves_all_four_ladders(live_server):
    base, _ = live_server
    body = _get(base, "/api/opportunity-board")

    assert body["success"] is True
    assert body["health"] in ("ok", "degraded", "healthy", "partial")
    board = body["board"]
    # These four are the whole point of the board and were previously
    # unreachable from anywhere.
    for key in ("winning", "earning", "micro_bets", "accumulators"):
        assert key in board
    assert board["winning"] == []  # Basic forecasts without offers are not published picks.
    assert board["accumulators"] == []  # execution details require a paid account
    assert board["earning"] == []


def test_board_route_reports_coverage_honestly(live_server):
    base, _ = live_server
    coverage = _get(base, "/api/opportunity-board")["board"]["coverage"]
    assert coverage["meets_volume_target"] is False
    assert coverage["volume_target"] == 12
    assert coverage["notes"]  # the shortfall is explained, not hidden


def test_board_route_caches_so_polls_do_not_hammer_the_free_stack(live_server):
    base, calls = live_server
    first = _get(base, "/api/opportunity-board")
    second = _get(base, "/api/opportunity-board")
    third = _get(base, "/api/opportunity-board")
    # Three dashboard polls, one cycle.
    assert len(calls) == 1
    assert second.get("cached") is True and third.get("cached") is True
    assert first.get("cached") is True


def test_anonymous_refresh_cannot_force_provider_requests(live_server):
    base, calls = live_server
    _get(base, "/api/opportunity-board")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, "/api/opportunity-board?refresh=1")
    assert exc.value.code == 403
    assert len(calls) == 1


def test_cache_is_server_scoped_not_handler_scoped(live_server):
    """Regression: a handler instance is built per request, so a cache stored
    on ``self`` is discarded before the next poll ever arrives.

    Both the board cache and the 120s forecast cache had this shape and
    silently rebuilt on every request. The assertion is on behaviour, not on
    where the attribute lives, so the fix cannot be undone quietly.
    """
    base, calls = live_server
    for _ in range(4):
        _get(base, "/api/opportunity-board")
    assert len(calls) == 1, "cache must survive across request instances"


def test_concurrent_polls_run_only_one_cycle(live_server):
    """A burst of dashboard polls must not stampede the free stack."""
    import concurrent.futures

    base, calls = live_server
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda _: _get(base, "/api/opportunity-board"), range(6)))
    assert len(calls) == 1
    assert all(r["success"] is True for r in results)


def test_board_route_reports_failure_without_inventing_a_board(monkeypatch):
    """A failed cycle must say so, never return an empty board as if it were real."""
    import lisa.feed as feed_mod

    def _boom(settings, **kwargs):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(feed_mod, "run_feed", _boom)

    port = _free_port()
    httpd = make_production_server(port=port, storage=InMemoryStorage())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/opportunity-board",
                                   timeout=20)
        assert exc.value.code == 503
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert payload["success"] is False
        assert payload["board"] is None
        assert payload["error"]  # no publication exists; reads never call providers
    finally:
        httpd.shutdown()
        httpd.server_close()
