"""The Odds API is disabled by default and cannot spend a credit.

These are guardrail tests, not feature tests. The migration moved LISA onto the
free stack in ``lisa/feed.py``; a leftover key in ``.env`` must be inert. The
assertion that matters is the negative one: with the kill switch off, no code
path -- CLI, scheduler, key pool -- may open a socket or write to the quota
ledger.
"""
from __future__ import annotations

import urllib.request

import pytest

from lisa import config as cfg
from lisa.client import DISABLED_HINT, OddsApiClient, OddsApiDisabled
from lisa.key_pool import RotatingOddsClient

_ALL_ENDPOINTS = ("list_sports", "get_odds", "get_scores", "get_events")

#: ``list_sports`` is the one endpoint that takes no sport key.
_NO_ARG_ENDPOINTS = {"list_sports"}


def _invoke(client: OddsApiClient, endpoint: str):
    if endpoint in _NO_ARG_ENDPOINTS:
        return getattr(client, endpoint)()
    return getattr(client, endpoint)("soccer_epl")


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if anything tries to actually perform a request."""
    def _boom(*args, **kwargs):
        raise AssertionError("network escape: Odds API request was attempted")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    return _boom


def _disabling_env(monkeypatch):
    """An environment that looks exactly like a stale pre-migration .env.

    ``load_settings()`` is called first, on purpose. The ``.env`` loader is
    one-shot per process and skips any variable already present in
    ``os.environ``; setting ``THE_ODDS_API_KEY`` *before* that first call would
    make the file skip it, and the monkeypatch teardown would then delete it
    for the remainder of the session -- silently hiding the real operator
    credentials from every later test in the run.
    """
    cfg.load_settings()
    monkeypatch.setenv("THE_ODDS_API_KEY", "stale-key-1")
    monkeypatch.setenv("THE_ODDS_API_KEY_2", "stale-key-2")
    monkeypatch.delenv("LISA_ENABLE_ODDS_API", raising=False)


# -- config ------------------------------------------------------------------

def test_odds_api_is_off_by_default(monkeypatch):
    _disabling_env(monkeypatch)
    settings = cfg.load_settings()
    # A key being present must not imply the provider is usable.
    assert settings.odds_api_key == "stale-key-1"
    assert len(settings.odds_api_keys) == 2
    assert settings.odds_api_enabled is False


def test_odds_api_requires_explicit_opt_in(monkeypatch):
    _disabling_env(monkeypatch)
    monkeypatch.setenv("LISA_ENABLE_ODDS_API", "1")
    assert cfg.load_settings().odds_api_enabled is True


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "maybe"])
def test_only_truthy_values_enable_the_odds_api(monkeypatch, falsy):
    """A typo must fail closed, not open."""
    _disabling_env(monkeypatch)
    monkeypatch.setenv("LISA_ENABLE_ODDS_API", falsy)
    assert cfg.load_settings().odds_api_enabled is False


# -- transport ---------------------------------------------------------------

@pytest.mark.parametrize("endpoint", _ALL_ENDPOINTS)
def test_disabled_client_refuses_every_endpoint(no_network, endpoint):
    client = OddsApiClient("stale-key", enabled=False)
    with pytest.raises(OddsApiDisabled):
        _invoke(client, endpoint)


def test_disabled_client_does_not_retry(no_network):
    """A blocked call must cost one raise, not ``max_retries + 1`` attempts."""
    client = OddsApiClient("stale-key", enabled=False, max_retries=4)
    with pytest.raises(OddsApiDisabled):
        client.get_odds("soccer_epl")


def test_disabled_error_never_leaks_the_key():
    """The key must not reach an exception message, a log, or a URL."""
    with pytest.raises(OddsApiDisabled) as exc:
        OddsApiClient("super-secret-key", enabled=False).get_odds("soccer_epl")
    assert "super-secret-key" not in str(exc.value)


def test_disabled_error_names_the_replacement():
    with pytest.raises(OddsApiDisabled) as exc:
        OddsApiClient("k", enabled=False).list_sports()
    message = str(exc.value)
    assert "LISA_ENABLE_ODDS_API" in message
    assert "python -m lisa feed" in message  # points at the free stack


def test_disabled_error_names_only_real_entry_points(monkeypatch):
    """The hint must point at a command that actually exists.

    The replacement path is ``python -m lisa feed``. Asserted against the real
    parser with the handler stubbed out, so this proves the subcommand is
    wired without running a cycle or touching the network.
    """
    from lisa import cli

    reached: list[str] = []

    def _stub(name):
        def _handler(_args):
            reached.append(name)
            return 0
        return _handler

    monkeypatch.setattr(cli, "_cmd_feed", _stub("feed"))
    monkeypatch.setattr(cli, "_cmd_board", _stub("board"))

    for command in ("feed", "board"):
        assert cli.main([command]) == 0
    assert reached == ["feed", "board"]

    assert "python -m lisa feed" in DISABLED_HINT
    # Nothing may advertise the retired provider as a way to get data.
    assert "THE_ODDS_API_KEY=" not in DISABLED_HINT


def test_enabled_client_still_transports(monkeypatch):
    """Guard against the switch becoming a permanent brick."""
    seen = {}

    class _Resp:
        headers = {}

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    assert OddsApiClient("k", enabled=True).get_odds("soccer_epl") == []
    assert "soccer_epl" in seen["url"]


# -- key pool ----------------------------------------------------------------

def test_disabled_pool_blocks_and_leaves_ledger_clean(no_network):
    pool = RotatingOddsClient(["k1", "k2"], enabled=False)
    with pytest.raises(OddsApiDisabled):
        pool.fetch_league_odds("soccer_epl")
    # record_use() runs before the request in the normal path; a disabled pool
    # must not fake a credit spend or the daily budget looks drained.
    assert all(state.used == 0 for state in pool.pool._states)


def test_pool_pushes_the_switch_into_every_transport():
    pool = RotatingOddsClient(["k1", "k2"], enabled=False)
    assert pool._transports and all(not t.enabled for t in pool._transports)


def test_empty_pool_still_refuses_construction():
    """No keys is rejected at construction, before any switch is consulted.

    This is the pre-existing contract (see TestRotatingClient in
    test_key_pool.py) and the kill switch must not change it: a pool with
    nothing to rotate over is a programming error, not a budget decision.
    """
    with pytest.raises(ValueError):
        RotatingOddsClient([], enabled=False)
