"""Normalisation: map The Odds API JSON payloads onto domain types.

Defensive by design — a single malformed game must never take the whole
cycle down; bad records are skipped, good records survive.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .odds import Book, Match, Score, SportInfo

H2H = "h2h"


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
                       *, market_key: str = H2H) -> list[Match]:
    matches: list[Match] = []
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

        books: list[Book] = []
        for bm in game.get("bookmakers", []) or []:
            key = str(bm.get("key", ""))
            if not key:
                continue
            outcomes: dict[str, float] = {}
            for mkt in bm.get("markets", []) or []:
                if mkt.get("key") != market_key:
                    continue
                for oc in mkt.get("outcomes", []) or []:
                    name = oc.get("name")
                    price = oc.get("price")
                    if name and isinstance(price, (int, float)):
                        outcomes[str(name)] = float(price)
            if outcomes:
                books.append(Book(
                    key=key,
                    title=str(bm.get("title", key)),
                    last_update=_parse_iso(bm.get("last_update")),
                    outcomes=outcomes,
                ))

        matches.append(Match(
            id=match_id,
            sport_key=sport_key,
            commence_time=commence,
            home_team=home,
            away_team=away,
            completed=bool(game.get("completed", False)),
            market=market_key,
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