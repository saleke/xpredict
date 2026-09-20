"""Normalisation: map The Odds API JSON payloads onto domain types.

Defensive by design — a single malformed game must never take the whole
cycle down; bad records are skipped, good records survive.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .odds import Book, Match, Score, SportInfo

H2H = "h2h"
MIN_DECIMAL_ODDS = 1.01
MAX_DECIMAL_ODDS = 1000.0


def validate_raw_game(game: Any) -> tuple[bool, Optional[str]]:
    """Validate top-level game schema from raw B2B odds feeds."""
    if not isinstance(game, dict):
        return False, "payload_not_dict"
    for field in ("id", "sport_key", "home_team", "away_team"):
        val = game.get(field)
        if not val or not isinstance(val, str) or not val.strip():
            return False, f"missing_or_empty_{field}"
    commence = _parse_iso(game.get("commence_time"))
    if commence is None:
        return False, "invalid_or_missing_commence_time"
    bms = game.get("bookmakers")
    if not isinstance(bms, list):
        return False, "bookmakers_not_list"
    return True, None


def validate_market_outcomes(market_key: str, home_team: str,
                             outcomes_raw: Any) -> tuple[bool, dict[str, float], Optional[float], Optional[str]]:
    """Validate and normalize bookmaker market outcomes.

    Filters out American odds (negative prices), degenerate prices (<= 1.0 or > 1000.0),
    and non-numeric or malformed point lines.
    """
    if not isinstance(outcomes_raw, list):
        return False, {}, None, "outcomes_not_list"

    clean_outcomes: dict[str, float] = {}
    points: set[float] = set()

    for oc in outcomes_raw:
        if not isinstance(oc, dict):
            continue
        name = oc.get("name")
        price = oc.get("price")
        point = oc.get("point")

        if not name or not isinstance(name, str):
            continue

        if not isinstance(price, (int, float)) or not math.isfinite(price):
            continue
        price_f = float(price)
        if price_f < MIN_DECIMAL_ODDS or price_f > MAX_DECIMAL_ODDS:
            continue

        clean_outcomes[str(name)] = price_f

        if isinstance(point, (int, float)) and math.isfinite(point):
            points.add(float(point))

    if not clean_outcomes:
        return False, {}, None, "no_valid_outcomes"

    book_line: Optional[float] = None
    if market_key == "spreads":
        for oc in outcomes_raw:
            if isinstance(oc, dict) and oc.get("name") == home_team:
                pt = oc.get("point")
                if isinstance(pt, (int, float)) and math.isfinite(pt):
                    book_line = float(pt)
                    break
        if book_line is None and len(points) == 1:
            book_line = points.pop()
    elif len(points) == 1:
        book_line = points.pop()

    return True, clean_outcomes, book_line, None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_sports(payload: Iterable[dict[str, Any]]) -> list[SportInfo]:
    out: list[SportInfo] = []
    for item in payload:
        try:
            key = item["key"]
        except (KeyError, TypeError):
            continue
        out.append(SportInfo(
            key=key,
            active=bool(item.get("active", True)),
            title=str(item.get("title", key)),
        ))
    return out


def parse_odds_payload(payload: Iterable[dict[str, Any]],
                       *, market_keys: Iterable[str] = (H2H,)) -> list[Match]:
    wanted = set(market_keys)
    matches: list[Match] = []
    for game in payload:
        ok, _ = validate_raw_game(game)
        if not ok:
            continue

        match_id = str(game["id"])
        sport_key = str(game["sport_key"])
        home = str(game["home_team"])
        away = str(game["away_team"])
        commence = _parse_iso(game.get("commence_time"))
        if commence is None:
            continue

        by_market: dict[str, list[Book]] = {key: [] for key in wanted}
        for bm in game.get("bookmakers", []) or []:
            key = str(bm.get("key", ""))
            if not key:
                continue
            for mkt in bm.get("markets", []) or []:
                mkey = mkt.get("key")
                if mkey not in wanted:
                    continue
                valid, clean_outcomes, book_line, _ = validate_market_outcomes(
                    str(mkey), home, mkt.get("outcomes")
                )
                if not valid:
                    continue

                by_market[mkey].append(Book(
                    key=key,
                    title=str(bm.get("title", key)),
                    last_update=_parse_iso(bm.get("last_update")),
                    outcomes=clean_outcomes,
                    line=book_line,
                ))

        for mkey, books in by_market.items():
            if not books:
                continue
            matches.append(Match(
                id=match_id,
                sport_key=sport_key,
                commence_time=commence,
                home_team=home,
                away_team=away,
                completed=bool(game.get("completed", False)),
                market=mkey,
                bookmakers=tuple(books),
            ))
    return matches


def parse_scores_payload(payload: Iterable[dict[str, Any]]) -> list[Score]:
    out: list[Score] = []
    for game in payload:
        try:
            match_id = str(game["id"])
            sport_key = str(game["sport_key"])
            home = str(game["home_team"])
            away = str(game["away_team"])
        except (KeyError, TypeError):
            continue
        commence = _parse_iso(game.get("commence_time"))
        if commence is None:
            continue

        home_score: Optional[int] = None
        away_score: Optional[int] = None
        for sc in game.get("scores", []) or []:
            if sc.get("name") == home:
                home_score = _to_int(sc.get("score"))
            elif sc.get("name") == away:
                away_score = _to_int(sc.get("score"))

        out.append(Score(
            match_id=match_id,
            sport_key=sport_key,
            commence_time=commence,
            completed=bool(game.get("completed", False)),
            home_score=home_score,
            away_score=away_score,
            status=str(game.get("score_status", "live")).lower(),
            home_team=home,
            away_team=away,
        ))
    return out


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None