"""Configuration: env wiring, strict league scope, sane defaults."""
from __future__ import annotations

import pytest

from lisa import config as cfg


def test_default_scope_is_the_nine_league_whitelist(monkeypatch):
    monkeypatch.delenv("LISA_SPORTS", raising=False)
    settings = cfg.load_settings()
    assert settings.sports == cfg.SCOPE_LEAGUES
    assert len(settings.sports) == 9
    assert "basketball_nba" in settings.sports
    assert "basketball_euroleague" in settings.sports
    assert "soccer_epl" in settings.sports


def test_api_base_url_defaults_to_api_host(monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_BASE_URL", raising=False)
    monkeypatch.delenv("LISA_API_BASE_URL", raising=False)
    assert cfg.load_settings().api_base_url == "https://api.the-odds-api.com"


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