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
                       *, market_keys: Iterable[str] = (H2H,)) -> list[Match]:
    wanted = set(market_keys)
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

        by_market: dict[str, list[Book]] = {key: [] for key in wanted}
        for bm in game.get("bookmakers", []) or []:
            key = str(bm.get("key", ""))
            if not key:
                continue
            for mkt in bm.get("markets", []) or []:
                mkey = mkt.get("key")
                if mkey not in wanted:
                    continue
                outcomes: dict[str, float] = {}
                points: set[float] = set()
                for oc in mkt.get("outcomes", []) or []:
                    name = oc.get("name")
                    price = oc.get("price")
                    point = oc.get("point")
                    if name and isinstance(price, (int, float)):
                        outcomes[str(name)] = float(price)
                        if isinstance(point, (int, float)):
                            points.add(float(point))
                if outcomes:
                    by_market[mkey].append(Book(
                        key=key,
                        title=str(bm.get("title", key)),
                        last_update=_parse_iso(bm.get("last_update")),
                        outcomes=outcomes,
                        line=points.pop() if len(points) == 1 else None,
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