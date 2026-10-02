"""Configuration: env wiring, strict league scope, sane defaults."""
from __future__ import annotations

import os

import pytest

from lisa import config as cfg


def test_default_scope_matches_the_declared_allowlist():
    # Read the dataclass default rather than load_settings(): the local .env
    # sets LISA_SPORTS, so load_settings() reflects the deployment, not the
    # default this test is about.
    settings = cfg.Settings()
    assert settings.sports == cfg.SCOPE_LEAGUES
    # The original nine were all European, which meant a European matchweek
    # pause emptied the 24h board. The widened scope must keep the originals
    # AND add leagues on an independent calendar.
    for original in ("basketball_nba", "basketball_euroleague", "soccer_epl",
                     "soccer_spain_la_liga", "soccer_germany_bundesliga"):
        assert original in settings.sports
    for independent in ("americanfootball_nfl", "baseball_mlb",
                        "basketball_wnba", "icehockey_liiga",
                        "soccer_uefa_nations_league"):
        assert independent in settings.sports
    assert len(settings.sports) > 9
    assert len(set(settings.sports)) == len(settings.sports), "no duplicates"


def test_api_base_url_defaults_to_api_host(monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_BASE_URL", raising=False)
    monkeypatch.delenv("LISA_API_BASE_URL", raising=False)
    assert cfg.load_settings().api_base_url == "https://api.the-odds-api.com"


def test_dotenv_loads_once_and_does_not_clobber_the_live_environment(monkeypatch, tmp_path):
    """``.env`` fills gaps but never overwrites a real environment variable.

    Also pins the one-shot latch: a test that ``setenv``s a credential *before*
    the first ``load_settings()`` makes the file skip it, and the teardown then
    deletes it for the rest of the session. Tests that mutate credentials must
    force the load first.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("LISA_DOTENV_PROBE=from-file\n", encoding="utf-8")

    monkeypatch.delenv("LISA_DOTENV_PROBE", raising=False)
    cfg._load_dotenv(str(env_file), force=True)
    assert os.environ["LISA_DOTENV_PROBE"] == "from-file"

    # A real environment variable outranks the file.
    monkeypatch.setenv("LISA_DOTENV_PROBE", "from-operator")
    cfg._load_dotenv(str(env_file), force=True)
    assert os.environ["LISA_DOTENV_PROBE"] == "from-operator"


def test_regions_default_to_eu_us(monkeypatch):
    monkeypatch.delenv("LISA_REGIONS", raising=False)
    assert cfg.load_settings().regions == "eu,us"


def test_the_odds_api_key_env_is_primary(monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "key-a")
    monkeypatch.setenv("LISA_ODDS_API_KEY", "key-b")
    assert cfg.load_settings().odds_api_key == "key-a"


def test_lisa_api_key_fallback(monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    monkeypatch.setenv("LISA_ODDS_API_KEY", "key-b")
    assert cfg.load_settings().odds_api_key == "key-b"


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_BASE_URL", "https://proxy.example.com")
    assert cfg.load_settings().api_base_url == "https://proxy.example.com"


def test_base_url_lisa_alias_override(monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_BASE_URL", raising=False)
    monkeypatch.setenv("LISA_API_BASE_URL", "https://proxy.example.com")
    assert cfg.load_settings().api_base_url == "https://proxy.example.com"


def test_unsupported_sport_key_raises(monkeypatch):
    monkeypatch.setenv("LISA_SPORTS", "basketball_nba,chess_open")
    with pytest.raises(ValueError, match="chess_open"):
        cfg.load_settings()


def test_scope_validation_rejects_foreign_keys():
    with pytest.raises(ValueError, match="soccer_unknowable"):
        cfg.validate_sports(("basketball_nba", "soccer_unknowable"))


def test_scope_validation_accepts_subset():
    assert cfg.validate_sports(("basketball_nba",)) == ("basketball_nba",)