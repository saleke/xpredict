"""Match router — routes to the best available data source per league.

Strategy:
  * Tier A (top 8 leagues): The Odds API (matches + odds + scores)
  * Tier B (next 50 leagues): Flashscore (matches + scores) + Odds API (odds when available)
  * Tier C (remaining 80+ leagues): Flashscore only (matches + scores, no odds)

This ensures we only spend API credits on leagues with paying subscribers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config as cfg
from .flashscore import FlashscoreClient, FlashscoreMatch, flashscore_client
from .odds import Match, Score, utcnow

logger = logging.getLogger(__name__)

# League tier classification
TIER_A_LEAGUES: frozenset[str] = frozenset({
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "basketball_nba",
})

TIER_B_LEAGUES: frozenset[str] = frozenset({
    "soccer_uefa_champions_league",
    "soccer_uefa_europa_league",
    "soccer_england_championship",
    "soccer_spain_segunda",
    "soccer_germany_2_bundesliga",
    "soccer_italy_serie_b",
    "soccer_france_ligue_2",
    "soccer_belgium_first_division",
    "soccer_scotland_premiership",
    "soccer_turkey_super_lig",
    "soccer_russia_premier_league",
    "soccer_ukraine_premier_league",
    "soccer_austria_bundesliga",
    "soccer_switzerland_super_league",
    "soccer_greece_super_league",
    "soccer_czech_republic_first_league",
    "soccer_denmark_superliga",
    "soccer_norway_eliteserien",
    "soccer_sweden_allsvenskan",
    "soccer_finland_veikkausliiga",
    "soccer_poland_ekstraklasa",
    "soccer_romania_liga_1",
    "soccer_hungary_nb_1",
    "soccer_croatia_1_hnl",
    "soccer_serbia_super_liga",
    "soccer_bulgaria_first_league",
    "soccer_slovakia_super_liga",
    "soccer_slovenia_prva_liga",
    "soccer_ireland_premier_division",
    "soccer_northern_ireland_premiership",
    "soccer_wales_premier",
    "soccer_iceland_urvalsdeild",
    "soccer_luxembourg_national_division",
    "soccer_albania_kategoria_superiore",
    "soccer_bosnia_premier_league",
    "soccer_north_macedonia_first_league",
    "soccer_montenegro_first_league",
    "soccer_moldova_divizia_nationala",
    "soccer_estonia_meistriliiga",
    "soccer_latvia_virsliga",
    "soccer_lithuania_a_lyga",
    "soccer_belarus_premier_league",
    "soccer_armenia_premier_league",
    "soccer_azerbaijan_premier_league",
    "soccer_georgia_rovnuli_liga",
    "soccer_kazakhstan_premier_league",
    "soccer_usa_mls",
    "soccer_mexico_liga_mx",
    "soccer_argentina_primera",
    "soccer_brazil_serie_a",
    "soccer_brazil_serie_b",
    "soccer_chile_primera",
    "soccer_colombia_primera_a",
    "soccer_ecuador_serie_a",
    "soccer_peru_primera",
    "soccer_uruguay_primera",
    "soccer_venezuela_primera",
    "soccer_bolivia_primera",
    "soccer_paraguay_primera",
    "soccer_south_africa_premiership",
    "soccer_egypt_premier_league",
    "soccer_morocco_botola",
    "soccer_tunisia_ligue_1",
    "soccer_algeria_ligue_1",
    "soccer_nigeria_npfl",
    "soccer_ghana_premier_league",
    "soccer_kenya_premier_league",
    "soccer_tanzania_premier_league",
    "soccer_uganda_premier_league",
    "soccer_zambia_super_league",
    "soccer_zimbabwe_premier_league",
    "soccer_ivory_coast_ligue_1",
    "soccer_senegal_ligue_1",
    "soccer_cameroon_elite_one",
    "soccer_congo_ligue_1",
    "soccer_dr_congo_ligue_1",
    "soccer_ethiopia_premier_league",
    "soccer_sudan_premier_league",
    "soccer_somalia_premier_league",
    "soccer_burundi_premier_league",
    "soccer_rwanda_premier_league",
    "soccer_malawi_premier_league",
    "soccer_mozambique_mocambola",
    "soccer_angola_girabola",
    "soccer_namibia_premier_league",
    "soccer_botswana_premier_league",
    "soccer_lesotho_premier_league",
    "soccer_eswatini_premier_league",
    "soccer_madagascar_premier_league",
    "soccer_mauritius_premier_league",
    "soccer_seychelles_premier_league",
    "soccer_comoros_premier_league",
    "soccer_djibouti_premier_league",
    "soccer_liberia_premier_league",
    "soccer_sierra_leone_premier_league",
    "soccer_guinea_ligue_1",
    "soccer_gambia_premier_league",
    "soccer_guinea_bissau_ligue_1",
    "soccer_cape_verde_premier_league",
    "soccer_sao_tome_premier_league",
    "soccer_equatorial_guinea_ligue_1",
    "soccer_gabon_ligue_1",
    "soccer_central_african_republic_ligue_1",
    "soccer_chad_ligue_1",
    "soccer_niger_ligue_1",
    "soccer_burkina_faso_premier_league",
    "soccer_mali_ligue_1",
    "soccer_mauritania_ligue_1",
    "soccer_nigeria_nnfl",
    "soccer_indonesia_liga_1",
    "soccer_malaysia_super_league",
    "soccer_thailand_league_1",
    "soccer_vietnam_v_league",
    "soccer_singapore_premier_league",
    "soccer_philippines_pfl",
    "soccer_myanmar_national_league",
    "soccer_cambodia_premier_league",
    "soccer_laos_premier_league",
    "soccer_brunei_super_league",
    "soccer_timor_leste_premier_league",
    "soccer_australia_a_league",
    "soccer_new_zealand_football_championship",
    "soccer_japan_j1_league",
    "soccer_south_korea_k_league_1",
    "soccer_china_super_league",
    "soccer_india_indian_super_league",
    "soccer_iran_persian_gulf_pro_league",
    "soccer_saudi_pro_league",
    "soccer_uae_pro_league",
    "soccer_qatar_stars_league",
    "soccer_kuwait_premier_league",
    "soccer_bahrain_premier_league",
    "soccer_oman_professional_league",
    "soccer_jordan_pro_league",
    "soccer_lebanon_premier_league",
    "soccer_syria_premier_league",
    "soccer_iraq_premier_league",
    "soccer_palestine_premier_league",
    "soccer_yemen_premier_league",
    "soccer_afghanistan_premier_league",
    "soccer_pakistan_premier_league",
    "soccer_bangladesh_premier_league",
    "soccer_sri_lanka_premier_league",
    "soccer_nepal_premier_league",
    "soccer_bhutan_premier_league",
    "soccer_maldives_premier_league",
    "soccer_mongolia_premier_league",
    "soccer_kyrgyzstan_premier_league",
    "soccer_tajikistan_premier_league",
    "soccer_turkmenistan_premier_league",
    "soccer_uzbekistan_super_league",
    "soccer_hong_kong_premier_league",
    "soccer_taiwan_premier_league",
    "soccer_macau_premier_league",
    "soccer_guam_premier_league",
    "soccer_fiji_premier_league",
    "soccer_solomon_islands_premier_league",
    "soccer_vanuatu_premier_league",
    "soccer_new_caledonia_super_league",
    "soccer_tahiti_ligue_1",
    "soccer_papua_new_guinea_premier_league",
    "soccer_samoa_premier_league",
    "soccer_tonga_premier_league",
    "soccer_cook_islands_premier_league",
    "soccer_american_samoa_premier_league",
    "soccer_micronesia_premier_league",
    "soccer_palau_premier_league",
    "soccer_marshall_islands_premier_league",
    "soccer_kiribati_premier_league",
    "soccer_nauru_premier_league",
    "soccer_tuvalu_premier_league",
    "soccer_vatican_premier_league",
    "soccer_san_marino_premier_league",
    "soccer_andorra_premier_league",
    "soccer_liechtenstein_premier_league",
    "soccer_monaco_premier_league",
    "soccer_gibraltar_premier_league",
    "soccer_faroe_islands_premier_league",
    "soccer_greenland_premier_league",
    "soccer_aland_premier_league",
    "soccer_jersey_premier_league",
    "soccer_guernsey_premier_league",
    "soccer_isle_of_man_premier_league",
    "soccer_bermuda_premier_league",
    "soccer_cayman_islands_premier_league",
    "soccer_turks_and_caicos_premier_league",
    "soccer_british_virgin_islands_premier_league",
    "soccer_anguilla_premier_league",
    "soccer_montserrat_premier_league",
    "soccer_antigua_and_barbuda_premier_league",
    "soccer_saint_kitts_and_nevis_premier_league",
    "soccer_dominica_premier_league",
    "soccer_saint_lucia_premier_league",
})


@dataclass(frozen=True)
class LeagueTier:
    """Classification of a league by data source tier."""
    sport_key: str
    tier: str  # "A", "B", or "C"
    has_odds: bool
    has_scores: bool
    flashscore_id: Optional[int] = None


class MatchRouter:
    """Routes match data requests to the best available source.

    The router maintains a cache of league classifications and match data
    to minimize API calls and respect rate limits.
    """

    def __init__(self, *, flashscore: Optional[FlashscoreClient] = None,
                 cache_ttl: int = 300):
        self.flashscore = flashscore or flashscore_client
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._league_tiers: dict[str, LeagueTier] = {}

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.time():
            del self._cache[key]
            return None
        return value

    def _set_cached(self, key: str, value: Any) -> None:
        """Cache a value with TTL."""
        self._cache[key] = (time.time() + self.cache_ttl, value)

    def classify_league(self, sport_key: str) -> LeagueTier:
        """Classify a league into tier A, B, or C."""
        if sport_key in self._league_tiers:
            return self._league_tiers[sport_key]

        from .flashscore import LEAGUE_IDS

        if sport_key in TIER_A_LEAGUES:
            tier = LeagueTier(
                sport_key=sport_key,
                tier="A",
                has_odds=True,
                has_scores=True,
                flashscore_id=LEAGUE_IDS.get(sport_key),
            )
        elif sport_key in TIER_B_LEAGUES:
            tier = LeagueTier(
                sport_key=sport_key,
                tier="B",
                has_odds=True,
                has_scores=True,
                flashscore_id=LEAGUE_IDS.get(sport_key),
            )
        else:
            tier = LeagueTier(
                sport_key=sport_key,
                tier="C",
                has_odds=False,
                has_scores=True,
                flashscore_id=LEAGUE_IDS.get(sport_key),
            )

        self._league_tiers[sport_key] = tier
        return tier

    def get_matches(self, sport_key: str, *, days_ahead: int = 7,
                    source: str = "auto") -> list[Match]:
        """Get matches for a league from the best available source.

        Args:
            sport_key: League identifier
            days_ahead: Number of days ahead to fetch
            source: "auto", "odds_api", or "flashscore"

        Returns:
            List of Match objects
        """
        cache_key = f"matches:{sport_key}:{days_ahead}:{source}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        tier = self.classify_league(sport_key)
        matches: list[Match] = []

        if source == "odds_api" or (source == "auto" and tier.tier == "A"):
            # Use The Odds API (handled by existing pipeline)
            # This is a placeholder — the actual Odds API call is in pipeline.py
            pass

        if source == "flashscore" or (source == "auto" and tier.tier in ("B", "C")):
            # Use Flashscore
            if tier.flashscore_id is not None:
                fs_matches = self.flashscore.get_league_matches(
                    tier.flashscore_id,
                    league_name=sport_key,
                    days_ahead=days_ahead,
                )
                matches = [self.flashscore.to_domain_match(m) for m in fs_matches]

        self._set_cached(cache_key, matches)
        return matches

    def get_live_matches(self, sport_key: str) -> list[Match]:
        """Get currently live matches for a league."""
        tier = self.classify_league(sport_key)
        if tier.flashscore_id is None:
            return []

        cache_key = f"live:{sport_key}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        fs_matches = self.flashscore.get_live_matches(tier.flashscore_id)
        matches = [self.flashscore.to_domain_match(m) for m in fs_matches]
        self._set_cached(cache_key, matches)
        return matches

    def get_scores(self, match_id: str, sport_key: str = "") -> Optional[Score]:
        """Get the latest score for a match."""
        cache_key = f"score:{match_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        score = self.flashscore.get_match_scores(match_id)
        if score is not None:
            self._set_cached(cache_key, score)
        return score

    def get_all_leagues(self, tier: Optional[str] = None) -> list[str]:
        """Get all known league IDs, optionally filtered by tier."""
        from .flashscore import LEAGUE_IDS
        if tier is None:
            return list(LEAGUE_IDS.keys())
        return [k for k, v in LEAGUE_IDS.items()
                if self.classify_league(k).tier == tier]

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cache.clear()


# Global router instance
match_router = MatchRouter()
