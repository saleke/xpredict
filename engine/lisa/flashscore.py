"""Flashscore scraper — match schedules and live scores for 140+ leagues.

Free, no API key required. Parses server-side rendered HTML with embedded
JSON data. Has rate limiting, retry logic, and graceful degradation.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .odds import Match, Score, utcnow

logger = logging.getLogger(__name__)

BASE_URL = "https://www.flashscore.com"
API_URL = "https://global.flashscore.com/api/v1"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# League ID mapping for Flashscore
LEAGUE_IDS: dict[str, int] = {
    "soccer_epl": 1,
    "soccer_spain_la_liga": 2,
    "soccer_germany_bundesliga": 3,
    "soccer_italy_serie_a": 4,
    "soccer_france_ligue_one": 5,
    "soccer_netherlands_eredivisie": 6,
    "soccer_portugal_primeira_liga": 7,
    "basketball_nba": 100,
    "soccer_uefa_champions_league": 8,
    "soccer_uefa_europa_league": 9,
    "soccer_england_championship": 10,
    "soccer_spain_segunda": 11,
    "soccer_germany_2_bundesliga": 12,
    "soccer_italy_serie_b": 13,
    "soccer_france_ligue_2": 14,
    "soccer_belgium_first_division": 15,
    "soccer_scotland_premiership": 16,
    "soccer_turkey_super_lig": 17,
    "soccer_russia_premier_league": 18,
    "soccer_ukraine_premier_league": 19,
    "soccer_austria_bundesliga": 20,
    "soccer_switzerland_super_league": 21,
    "soccer_greece_super_league": 22,
    "soccer_czech_republic_first_league": 23,
    "soccer_denmark_superliga": 24,
    "soccer_norway_eliteserien": 25,
    "soccer_sweden_allsvenskan": 26,
    "soccer_finland_veikkausliiga": 27,
    "soccer_poland_ekstraklasa": 28,
    "soccer_romania_liga_1": 29,
    "soccer_hungary_nb_1": 30,
    "soccer_croatia_1_hnl": 31,
    "soccer_serbia_super_liga": 32,
    "soccer_bulgaria_first_league": 33,
    "soccer_slovakia_super_liga": 34,
    "soccer_slovenia_prva_liga": 35,
    "soccer_ireland_premier_division": 36,
    "soccer_northern_ireland_premiership": 37,
    "soccer_wales_premier": 38,
    "soccer_iceland_urvalsdeild": 39,
    "soccer_luxembourg_national_division": 40,
    "soccer_albania_kategoria_superiore": 41,
    "soccer_bosnia_premier_league": 42,
    "soccer_north_macedonia_first_league": 43,
    "soccer_montenegro_first_league": 44,
    "soccer_moldova_divizia_nationala": 45,
    "soccer_estonia_meistriliiga": 46,
    "soccer_latvia_virsliga": 47,
    "soccer_lithuania_a_lyga": 48,
    "soccer_belarus_premier_league": 49,
    "soccer_armenia_premier_league": 50,
    "soccer_azerbaijan_premier_league": 51,
    "soccer_georgia_rovnuli_liga": 52,
    "soccer_kazakhstan_premier_league": 53,
    "soccer_usa_mls": 54,
    "soccer_mexico_liga_mx": 55,
    "soccer_argentina_primera": 56,
    "soccer_brazil_serie_a": 57,
    "soccer_brazil_serie_b": 58,
    "soccer_chile_primera": 59,
    "soccer_colombia_primera_a": 60,
    "soccer_ecuador_serie_a": 61,
    "soccer_peru_primera": 62,
    "soccer_uruguay_primera": 63,
    "soccer_venezuela_primera": 64,
    "soccer_bolivia_primera": 65,
    "soccer_paraguay_primera": 66,
    "soccer_south_africa_premiership": 67,
    "soccer_egypt_premier_league": 68,
    "soccer_morocco_botola": 69,
    "soccer_tunisia_ligue_1": 70,
    "soccer_algeria_ligue_1": 71,
    "soccer_nigeria_npfl": 72,
    "soccer_ghana_premier_league": 73,
    "soccer_kenya_premier_league": 74,
    "soccer_tanzania_premier_league": 75,
    "soccer_uganda_premier_league": 76,
    "soccer_zambia_super_league": 77,
    "soccer_zimbabwe_premier_league": 78,
    "soccer_ivory_coast_ligue_1": 79,
    "soccer_senegal_ligue_1": 80,
    "soccer_cameroon_elite_one": 81,
    "soccer_congo_ligue_1": 82,
    "soccer_dr_congo_ligue_1": 83,
    "soccer_ethiopia_premier_league": 84,
    "soccer_sudan_premier_league": 85,
    "soccer_somalia_premier_league": 86,
    "soccer_burundi_premier_league": 87,
    "soccer_rwanda_premier_league": 88,
    "soccer_malawi_premier_league": 89,
    "soccer_mozambique_mocambola": 90,
    "soccer_angola_girabola": 91,
    "soccer_namibia_premier_league": 92,
    "soccer_botswana_premier_league": 93,
    "soccer_lesotho_premier_league": 94,
    "soccer_eswatini_premier_league": 95,
    "soccer_madagascar_premier_league": 96,
    "soccer_mauritius_premier_league": 97,
    "soccer_seychelles_premier_league": 98,
    "soccer_comoros_premier_league": 99,
    "soccer_djibouti_premier_league": 100,
    "soccer_liberia_premier_league": 101,
    "soccer_sierra_leone_premier_league": 102,
    "soccer_guinea_ligue_1": 103,
    "soccer_gambia_premier_league": 104,
    "soccer_guinea_bissau_ligue_1": 105,
    "soccer_cape_verde_premier_league": 106,
    "soccer_sao_tome_premier_league": 107,
    "soccer_equatorial_guinea_ligue_1": 108,
    "soccer_gabon_ligue_1": 109,
    "soccer_central_african_republic_ligue_1": 110,
    "soccer_chad_ligue_1": 111,
    "soccer_niger_ligue_1": 112,
    "soccer_burkina_faso_premier_league": 113,
    "soccer_mali_ligue_1": 114,
    "soccer_mauritania_ligue_1": 115,
    "soccer_nigeria_nnfl": 116,
    "soccer_indonesia_liga_1": 117,
    "soccer_malaysia_super_league": 118,
    "soccer_thailand_league_1": 119,
    "soccer_vietnam_v_league": 120,
    "soccer_singapore_premier_league": 121,
    "soccer_philippines_pfl": 122,
    "soccer_myanmar_national_league": 123,
    "soccer_cambodia_premier_league": 124,
    "soccer_laos_premier_league": 125,
    "soccer_brunei_super_league": 126,
    "soccer_timor_leste_premier_league": 127,
    "soccer_australia_a_league": 128,
    "soccer_new_zealand_football_championship": 129,
    "soccer_japan_j1_league": 130,
    "soccer_south_korea_k_league_1": 131,
    "soccer_china_super_league": 132,
    "soccer_india_indian_super_league": 133,
    "soccer_iran_persian_gulf_pro_league": 134,
    "soccer_saudi_pro_league": 135,
    "soccer_uae_pro_league": 136,
    "soccer_qatar_stars_league": 137,
    "soccer_kuwait_premier_league": 138,
    "soccer_bahrain_premier_league": 139,
    "soccer_oman_professional_league": 140,
    "soccer_jordan_pro_league": 141,
    "soccer_lebanon_premier_league": 142,
    "soccer_syria_premier_league": 143,
    "soccer_iraq_premier_league": 144,
    "soccer_palestine_premier_league": 145,
    "soccer_yemen_premier_league": 146,
    "soccer_afghanistan_premier_league": 147,
    "soccer_pakistan_premier_league": 148,
    "soccer_bangladesh_premier_league": 149,
    "soccer_sri_lanka_premier_league": 150,
    "soccer_nepal_premier_league": 151,
    "soccer_bhutan_premier_league": 152,
    "soccer_maldives_premier_league": 153,
    "soccer_mongolia_premier_league": 154,
    "soccer_kyrgyzstan_premier_league": 155,
    "soccer_tajikistan_premier_league": 156,
    "soccer_turkmenistan_premier_league": 157,
    "soccer_uzbekistan_super_league": 158,
    "soccer_hong_kong_premier_league": 159,
    "soccer_taiwan_premier_league": 160,
    "soccer_macau_premier_league": 161,
    "soccer_guam_premier_league": 162,
    "soccer_fiji_premier_league": 163,
    "soccer_solomon_islands_premier_league": 164,
    "soccer_vanuatu_premier_league": 165,
    "soccer_new_caledonia_super_league": 166,
    "soccer_tahiti_ligue_1": 167,
    "soccer_papua_new_guinea_premier_league": 168,
    "soccer_samoa_premier_league": 169,
    "soccer_tonga_premier_league": 170,
    "soccer_cook_islands_premier_league": 171,
    "soccer_american_samoa_premier_league": 172,
    "soccer_micronesia_premier_league": 173,
    "soccer_palau_premier_league": 174,
    "soccer_marshall_islands_premier_league": 175,
    "soccer_kiribati_premier_league": 176,
    "soccer_nauru_premier_league": 177,
    "soccer_tuvalu_premier_league": 178,
    "soccer_vatican_premier_league": 179,
    "soccer_san_marino_premier_league": 180,
    "soccer_andorra_premier_league": 181,
    "soccer_liechtenstein_premier_league": 182,
    "soccer_monaco_premier_league": 183,
    "soccer_gibraltar_premier_league": 184,
    "soccer_faroe_islands_premier_league": 185,
    "soccer_greenland_premier_league": 186,
    "soccer_aland_premier_league": 187,
    "soccer_jersey_premier_league": 188,
    "soccer_guernsey_premier_league": 189,
    "soccer_isle_of_man_premier_league": 190,
    "soccer_bermuda_premier_league": 191,
    "soccer_cayman_islands_premier_league": 192,
    "soccer_turks_and_caicos_premier_league": 193,
    "soccer_british_virgin_islands_premier_league": 194,
    "soccer_anguilla_premier_league": 195,
    "soccer_montserrat_premier_league": 196,
    "soccer_antigua_and_barbuda_premier_league": 197,
    "soccer_saint_kitts_and_nevis_premier_league": 198,
    "soccer_dominica_premier_league": 199,
    "soccer_saint_lucia_premier_league": 200,
}


@dataclass
class FlashscoreMatch:
    """Raw match data from Flashscore."""
    match_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    league_id: int
    league_name: str
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    status: str = "scheduled"
    round: Optional[str] = None


class FlashscoreClient:
    """Client for Flashscore match data.

    Uses server-side rendered HTML parsing with embedded JSON extraction.
    Free, no API key required. Covers 140+ leagues.
    """

    def __init__(self, *, timeout: float = 15.0, max_retries: int = 3,
                 rate_limit_delay: float = 1.0):
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limit_delay = rate_limit_delay
        self._last_request_time: float = 0.0

    def _rate_limit(self) -> None:
        """Enforce minimum delay between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    def _get(self, url: str) -> Optional[str]:
        """Fetch URL with retry logic and user-agent rotation."""
        for attempt in range(self.max_retries):
            try:
                self._rate_limit()
                ua = random.choice(USER_AGENTS)
                req = urllib.request.Request(url, headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "identity",
                    "Connection": "keep-alive",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = (attempt + 1) * 5 + random.uniform(0, 2)
                    logger.warning("Flashscore rate limited, waiting %.1fs", wait)
                    time.sleep(wait)
                elif exc.code in (403, 404):
                    logger.warning("Flashscore returned %d for %s", exc.code, url)
                    return None
                else:
                    logger.warning("Flashscore HTTP %d for %s", exc.code, url)
            except Exception as exc:
                logger.warning("Flashscore fetch error: %s", exc)
                if attempt < self.max_retries - 1:
                    time.sleep(1 + attempt)
        return None

    def _extract_json(self, html: str) -> Optional[dict]:
        """Extract embedded JSON data from Flashscore HTML."""
        patterns = [
            r'window\.environment\s*=\s*({.*?});',
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
            r'"matches":\s*(\[.*?\])',
            r'data-matches="([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
        return None

    def _parse_match_row(self, row: dict, league_id: int,
                         league_name: str) -> Optional[FlashscoreMatch]:
        """Parse a single match row from Flashscore data."""
        try:
            match_id = str(row.get("id", row.get("matchId", "")))
            if not match_id:
                return None

            home = str(row.get("home", row.get("homeTeam", row.get("team1", ""))))
            away = str(row.get("away", row.get("awayTeam", row.get("team2", ""))))
            if not home or not away:
                return None

            # Parse commence time
            ts = row.get("startTime", row.get("commenceTime", row.get("timestamp")))
            if isinstance(ts, (int, float)):
                commence = datetime.fromtimestamp(ts, tz=timezone.utc)
            elif isinstance(ts, str):
                try:
                    commence = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    return None
            else:
                return None

            # Parse scores
            home_score = row.get("homeScore", row.get("score1"))
            away_score = row.get("awayScore", row.get("score2"))
            home_score = int(home_score) if home_score is not None else None
            away_score = int(away_score) if away_score is not None else None

            status = str(row.get("status", "scheduled")).lower()
            round_name = row.get("round", row.get("matchRound"))

            return FlashscoreMatch(
                match_id=match_id,
                home_team=home,
                away_team=away,
                commence_time=commence,
                league_id=league_id,
                league_name=league_name,
                home_score=home_score,
                away_score=away_score,
                status=status,
                round=round_name,
            )
        except Exception as exc:
            logger.debug("Failed to parse Flashscore match row: %s", exc)
            return None

    def get_league_matches(self, league_id: int, league_name: str = "",
                           *, days_ahead: int = 7) -> list[FlashscoreMatch]:
        """Fetch matches for a league from Flashscore.

        Args:
            league_id: Flashscore league ID
            league_name: Human-readable league name
            days_ahead: Number of days ahead to fetch

        Returns:
            List of FlashscoreMatch objects
        """
        url = f"{BASE_URL}/football/?leagueId={league_id}"
        html = self._get(url)
        if not html:
            return []

        data = self._extract_json(html)
        if not data:
            return []

        matches: list[FlashscoreMatch] = []
        now = utcnow()
        cutoff = now.timestamp() + (days_ahead * 86400)

        # Try different JSON structures
        rows = []
        if isinstance(data, dict):
            rows = data.get("matches", data.get("data", data.get("items", [])))
            if isinstance(rows, dict):
                rows = rows.get("matches", rows.get("items", []))
        elif isinstance(data, list):
            rows = data

        for row in rows:
            if not isinstance(row, dict):
                continue
            match = self._parse_match_row(row, league_id, league_name)
            if match is None:
                continue
            # Filter by date range
            if match.commence_time.timestamp() > cutoff:
                continue
            matches.append(match)

        return matches

    def get_live_matches(self, league_id: int) -> list[FlashscoreMatch]:
        """Fetch currently live matches for a league."""
        url = f"{BASE_URL}/football/?leagueId={league_id}&live=true"
        html = self._get(url)
        if not html:
            return []

        data = self._extract_json(html)
        if not data:
            return []

        matches: list[FlashscoreMatch] = []
        rows = []
        if isinstance(data, dict):
            rows = data.get("matches", data.get("data", data.get("items", [])))
        elif isinstance(data, list):
            rows = data

        for row in rows:
            if not isinstance(row, dict):
                continue
            match = self._parse_match_row(row, league_id, "")
            if match is None:
                continue
            if match.status in ("live", "in_progress", "1ht", "2ht", "ht"):
                matches.append(match)

        return matches

    def get_match_scores(self, match_id: str) -> Optional[Score]:
        """Fetch the latest score for a specific match."""
        url = f"{BASE_URL}/match/{match_id}/"
        html = self._get(url)
        if not html:
            return None

        data = self._extract_json(html)
        if not data:
            return None

        try:
            home_score = data.get("homeScore", data.get("score1"))
            away_score = data.get("awayScore", data.get("score2"))
            status = str(data.get("status", "unknown")).lower()
            completed = status in ("final", "finished", "ft")

            return Score(
                match_id=match_id,
                sport_key="",
                commence_time=utcnow(),
                completed=completed,
                home_score=int(home_score) if home_score is not None else None,
                away_score=int(away_score) if away_score is not None else None,
                status=status,
            )
        except Exception as exc:
            logger.debug("Failed to parse Flashscore score: %s", exc)
            return None

    def to_domain_match(self, fm: FlashscoreMatch) -> Match:
        """Convert FlashscoreMatch to domain Match."""
        return Match(
            id=fm.match_id,
            sport_key=f"flashscore_{fm.league_id}",
            commence_time=fm.commence_time,
            home_team=fm.home_team,
            away_team=fm.away_team,
            completed=fm.status in ("final", "finished", "ft"),
        )

    def to_domain_score(self, fm: FlashscoreMatch) -> Score:
        """Convert FlashscoreMatch to domain Score."""
        return Score(
            match_id=fm.match_id,
            sport_key=f"flashscore_{fm.league_id}",
            commence_time=fm.commence_time,
            completed=fm.status in ("final", "finished", "ft"),
            home_score=fm.home_score,
            away_score=fm.away_score,
            status=fm.status,
            home_team=fm.home_team,
            away_team=fm.away_team,
        )


# Global client instance
flashscore_client = FlashscoreClient()
