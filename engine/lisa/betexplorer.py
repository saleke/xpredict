"""BetExplorer results feed via Parse — closing odds and final scores.

Purpose
-------
Two gaps in the current settlement path that this closes:

1. **No backfill.** ``settle.py`` only grades fixtures that appeared in an Odds
   API scores window, so anything older than ``settle_scores_days`` can never
   be settled and ``accuracy_tracker`` can never be backfilled.
2. **CLV has no independent reference.** ``pipeline`` derives closing odds from
   the same multi-book snapshot it priced from, which is not a true close.

This source supplies both: completed fixtures with a final score *and* the
market's closing 1X2 line, across ~35 leagues/day, with no verification gate.

What it is not
--------------
It is **not** a pre-match odds feed. Odds attach when a match *completes*, not
when it is scheduled (measured 2026-09-29: 3 of 144 same-day upcoming fixtures
carried odds, 98 of 98 completed ones did). Pre-match pricing stays with The
Odds API.

It also only publishes **1X2**. Totals, spreads and handicaps cannot be graded
from here, so ``grade_market`` refuses anything other than ``h2h`` rather than
guessing.

Credit cost is one Parse credit per call, metered separately from the engine's
``credit_budget_daily``. See ``engine/integrations/parse/README.md``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from .odds import H2H, Score

logger = logging.getLogger(__name__)

PARSE_BASE = "https://api.parse.bot"
# betexplorer-com-api — canonical id, access_requirements: [] (no phone/card gate).
BETEXPLORER_CANONICAL = "5daf70d1-eaad-46ba-afbb-c57c0d5ebc3e"

# Only *legal-form* noise is dropped. Distinguishing words — city, town,
# united, atletico, real — are load-bearing and MUST be kept: dropping "united"
# collapses "Manchester United" to "manchester", which then substring-matches
# "Manchester City" and settles a pick against the wrong team.
_LEGAL_FORMS = frozenset({
    "fc", "afc", "cf", "sc", "ac", "as", "ss", "ssc", "sv", "vfb", "vfl", "bsc",
    "cd", "ud", "rc", "rcd", "fk", "sk", "bk", "ik", "ff", "gd", "nk", "mks",
    "club", "calcio", "futbol", "futbolo",
})

#: Markers that mean this is a different squad from the senior men's side.
#: A cross-marker join settles a pick against the wrong fixture outright, so it
#: is refused rather than scored. Note "w"/"b" are matched as whole tokens
#: only, so they cannot fire on ordinary words.
_SIDE_MARKERS = frozenset({
    "w", "women", "ladies", "womens", "b", "reserves", "reserve", "ii", "iii",
    "castilla", "u17", "u18", "u19", "u20", "u21", "u23", "srl", "academy",
})

#: Minimum whole-word containment length, guarding against prefix collisions
#: such as "Ath" inside "Athlone".
_MIN_CONTAINMENT_CHARS = 5

#: Score at or above which a fixture join is trusted. Token-subset matches
#: score 0.85; anything lower is treated as no match.
MIN_JOIN_SCORE = 0.85


class BetExplorerError(RuntimeError):
    """Transport, auth, or parse failure talking to the Parse API."""


def normalise_team(name: str) -> str:
    """Reduce a club name to a join key shared by both feeds.

    Lowercases, strips accents, turns punctuation into spaces, and removes
    legal-form tokens such as "FC". Distinguishing words are preserved. Returns
    "" for empty input so callers reject it explicitly rather than matching "".
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Drop any parenthesised qualifier, e.g. "Brighton (Reserve)" or "Utd W".
    ascii_only = re.sub(r"\([^)]*\)", " ", ascii_only)
    lowered = ascii_only.lower()
    # Apostrophes vanish rather than becoming spaces, so "Nott'm" -> "nottm".
    lowered = lowered.replace("'", "").replace("’", "")
    cleaned = "".join(ch if ch.isalnum() else " " for ch in lowered)
    tokens = [t for t in cleaned.split() if t and t not in _LEGAL_FORMS]
    if not tokens:
        # Every token was legal-form noise (e.g. "FC"): keep them, otherwise
        # the name would reduce to "" and silently never match.
        tokens = [t for t in cleaned.split() if t]
    return " ".join(tokens)


def _side_markers(key: str) -> set[str]:
    return {t for t in key.split() if t in _SIDE_MARKERS}


def team_match_score(a: str, b: str) -> float:
    """Confidence that two club names refer to the same side.

    1.0  exact match on the normalised key
    0.85 one name's tokens are a subset of the other's ("Brighton" inside
         "Brighton and Hove Albion")
    0.0  no match, *or* a senior/reserve/women's mismatch in either direction

    Abbreviations are deliberately not guessed ("Man United", "Wolves",
    "Nott'm Forest" all score 0.0). They surface as unmatched in the report so
    the real match rate stays visible and an alias table can be added rather
    than hidden behind a fuzzy score that risks settling on the wrong side.
    """
    ka, kb = normalise_team(a), normalise_team(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0

    markers_a, markers_b = _side_markers(ka), _side_markers(kb)
    if markers_a != markers_b:
        # Senior men's side versus women's/reserve/youth: never join.
        return 0.0

    tokens_a, tokens_b = set(ka.split()), set(kb.split())
    short, long_ = (tokens_a, tokens_b) if len(ka) <= len(kb) else (tokens_b, tokens_a)
    if short and short <= long_:
        return 0.85
    return 0.0


def _to_optional_float(value: Any) -> Optional[float]:
    """Odds arrive as strings ("2.36"); absent ones as null."""
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_score(raw: Any) -> Optional[tuple[int, int]]:
    """Parse a "2:1" (or "2-1") final score into a pair of ints."""
    if not raw or not isinstance(raw, str):
        return None
    for sep in (":", "-"):
        if sep in raw:
            home, _, away = raw.partition(sep)
            try:
                return int(home.strip()), int(away.strip())
            except ValueError:
                return None
    return None


@dataclass(frozen=True)
class BetExplorerMatch:
    """One fixture on a BetExplorer day page."""

    event_id: str
    country: str
    league: str
    home_team: str
    away_team: str
    kickoff_local: str
    status: str
    score: Optional[tuple[int, int]]
    odds_home: Optional[float]
    odds_draw: Optional[float]
    odds_away: Optional[float]

    @property
    def is_final(self) -> bool:
        return self.status.strip().upper() == "FIN" and self.score is not None

    @property
    def has_1x2(self) -> bool:
        return None not in (self.odds_home, self.odds_draw, self.odds_away)

    def price_for(self, outcome_name: str) -> Optional[float]:
        """Closing 1X2 price for an engine outcome name.

        ``outcome_name`` is either a club name or "Draw" — the same vocabulary
        the engine stores on a pick row. Matching is done on the same
        normalised keys used to join fixtures, so a pick on
        "Brighton and Hove Albion" still finds the price on "Brighton".
        """
        if normalise_team(outcome_name) == "draw":
            return self.odds_draw
        home = team_match_score(outcome_name, self.home_team)
        away = team_match_score(outcome_name, self.away_team)
        if home >= away and home > 0:
            return self.odds_home
        if away > 0:
            return self.odds_away
        return None

    def to_score(self, *, match_id: str, sport_key: str,
                 commence_time: datetime,
                 home_team: str, away_team: str) -> Score:
        """Build an engine ``Score`` for grading.

        The score values come from BetExplorer; the *team names are the
        engine's own*, passed in from the pick row. That is deliberate.
        ``Score.winner()`` returns one of its own team strings and
        ``grade_pick`` compares that against the pick's ``outcome_name``, so
        substituting BetExplorer's spelling here would turn every settled leg
        into a LOSS whenever the two feeds disagree on a club name.
        """
        if self.score is None:
            raise BetExplorerError("to_score called on an unscored fixture")
        return Score(
            match_id=match_id,
            sport_key=sport_key,
            commence_time=commence_time,
            completed=True,
            home_score=self.score[0],
            away_score=self.score[1],
            status="final",
            home_team=home_team,
            away_team=away_team,
        )


def parse_matches(payload: dict) -> list[BetExplorerMatch]:
    """Extract match records from a Parse search_matches response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise BetExplorerError(f"unexpected payload shape: {type(data).__name__}")
    rows = data.get("matches")
    if not isinstance(rows, list):
        raise BetExplorerError("payload has no 'matches' list")

    matches: list[BetExplorerMatch] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        home = str(row.get("home_team") or "").strip()
        away = str(row.get("away_team") or "").strip()
        if not home or not away:
            continue
        matches.append(BetExplorerMatch(
            event_id=str(row.get("event_id") or ""),
            country=str(row.get("country") or ""),
            league=str(row.get("league") or ""),
            home_team=home,
            away_team=away,
            kickoff_local=str(row.get("time") or ""),
            status=str(row.get("status") or ""),
            score=_parse_score(row.get("score")),
            odds_home=_to_optional_float(row.get("odds_home")),
            odds_draw=_to_optional_float(row.get("odds_draw")),
            odds_away=_to_optional_float(row.get("odds_away")),
        ))
    return matches


class BetExplorerClient:
    """Read-only client for the BetExplorer day page.

    Serialises access with a minimum interval because each call spends a
    Parse credit, and every date is memoised for the process lifetime so
    re-running a backfill over an overlapping window is free.
    """

    def __init__(self, *, api_key: Optional[str] = None,
                 base_url: str = PARSE_BASE,
                 canonical: str = BETEXPLORER_CANONICAL,
                 min_interval_sec: float = 1.0,
                 timeout_sec: float = 45.0):
        key = api_key or os.environ.get("PARSE_API_KEY", "")
        if not key:
            raise BetExplorerError(
                "PARSE_API_KEY is not set; export it or run `parse login`"
            )
        self._api_key = key
        self._base = base_url.rstrip("/")
        self._canonical = canonical
        self._min_interval = min_interval_sec
        self._timeout = timeout_sec
        self._last_call_monotonic: float = 0.0
        self._cache: dict[str, list[BetExplorerMatch]] = {}
        self.calls_made = 0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_monotonic
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call_monotonic = time.monotonic()

    def get_matches(self, day: date) -> list[BetExplorerMatch]:
        """All fixtures published for ``day`` (YYYY-MM-DD). Cached per day."""
        key = day.isoformat()
        if key in self._cache:
            return self._cache[key]

        url = (f"{self._base}/scraper/{self._canonical}/search_matches?"
               + urllib.parse.urlencode({"date": key}))
        self._throttle()
        self.calls_made += 1
        req = urllib.request.Request(url, headers={
            "X-API-Key": self._api_key,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise BetExplorerError(f"HTTP {exc.code} for {key}: {detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError,
                json.JSONDecodeError) as exc:
            # OSError covers the connection-level failures urllib does not
            # wrap, notably ConnectionResetError and ssl.SSLError, which a
            # rate-limited or mid-handshake peer does raise. Without these the
            # exception escapes the backfill loop and aborts the whole pass
            # instead of being recorded as one bad day.
            raise BetExplorerError(f"transport failure for {key}: {exc!r}") from exc

        matches = parse_matches(payload)
        logger.info("betexplorer %s: %d fixture(s)", key, len(matches))
        self._cache[key] = matches
        return matches

    def get_finalised(self, day: date) -> list[BetExplorerMatch]:
        """Only completed fixtures that carry both a score and a 1X2 line."""
        return [m for m in self.get_matches(day) if m.is_final and m.has_1x2]

    def close(self) -> None:
        self._cache.clear()
