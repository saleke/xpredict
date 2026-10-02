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
from datetime import datetime, timezone

import pytest

from lisa import server as server_mod
from lisa.board import Accumulator, Board, BoardCoverage, Opportunity
from lisa.server import make_production_server
from lisa.storage import InMemoryStorage


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _StubReport:
    """Stands in for a FeedReport without touching the network."""

    def __init__(self, *, board: Board | None, health: str = "ok"):
        self.board = board
        self._health = health
        self.began = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        self.window_hours = 48.0
        self.results_collected = 702
        self.model = {"sufficient": True, "matches_used": 702, "rho": -0.3}
        self.prices: dict[str, Any] = {}
        self.price_match: dict[str, Any] = {}
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.leagues = {"soccer_germany_bundesliga": 0}
        self.fixtures_in_window = 0
        self.providers = []

    def health(self) -> str:
        return self._health

    def summary(self) -> str:
        return "health=ok stub"


def _board_payload() -> Board:
    kickoff = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
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
def live_server(monkeypatch):
    """A running server whose feed is stubbed, so tests never hit the network."""
    calls: list[int] = []
    board = _board_payload()

    def _fake_run_feed(settings, **kwargs):
        calls.append(1)
        return _StubReport(board=board)

    import lisa.feed as feed_mod
    monkeypatch.setattr(feed_mod, "run_feed", _fake_run_feed)

    port = _free_port()
    storage = InMemoryStorage()
    httpd = make_production_server(port=port, storage=storage)
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
    assert body["health"] == "ok"
    board = body["board"]
    # These four are the whole point of the board and were previously
    # unreachable from anywhere.
    for key in ("winning", "earning", "micro_bets", "accumulators"):
        assert key in board
    assert len(board["winning"]) == 1
    assert len(board["accumulators"]) == 1
    assert board["winning"][0]["selection"] == "Home"
    # No price was supplied, so the row must be flagged unpriced, not faked.
    assert board["winning"][0]["priced"] is False
    assert board["winning"][0]["best_odds"] is None
    assert board["winning"][0]["ev"] is None
    assert board["accumulators"][0]["priced"] is False
    # Leg correlation is always reported, never silently applied as the product.
    assert board["accumulators"][0]["correlation_penalty"] == 0.91
    assert board["accumulators"][0]["p_naive"] == 0.55
    assert board["accumulators"][0]["p_adjusted"] == 0.50


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
    assert first.get("cached") is None


def test_board_route_refresh_forces_exactly_one_new_cycle(live_server):
    base, calls = live_server
    _get(base, "/api/opportunity-board")
    _get(base, "/api/opportunity-board?refresh=1")
    _get(base, "/api/opportunity-board")
    assert len(calls) == 2


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
        assert "provider unreachable" in payload["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
