"""Automated settlement — Stage 4's grading cron.

Three hours (configurable) after kickoff the official result is pulled from
the scores endpoint and the ledger row is stamped WIN/LOSS. Matches that
never completed go VOID once they are well past their grace window.
Settlement is idempotent: already-terminal rows are never rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import config as cfg
from .odds import utcnow
from .parsing import parse_scores_payload
from .storage import Storage

_NEVER_COMPLETED = {"postponed", "cancelled", "suspended", "abandoned"}


@dataclass
class SettlementReport:
    pending: int = 0
    settled: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    skipped_no_scores: int = 0
    skipped_not_due: int = 0
    errors: list[str] = field(default_factory=list)
    settled_picks: list[dict] = field(default_factory=list)


def _parse_dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run_settlement(client, storage: Storage, settings: cfg.Settings,
                   *, now: Optional[datetime] = None) -> SettlementReport:
    report = SettlementReport()
    pending = storage.list_pending_picks()
    report.pending = len(pending)
    now = now or utcnow()
    if not pending:
        return report

    by_sport: dict[str, list[dict]] = {}
    for row in pending:
        by_sport.setdefault(row["sport_key"], []).append(row)

    scores_by_match: dict[str, object] = {}
    for sport in by_sport:
        try:
            payload = client.get_scores(sport, days_from=settings.settle_scores_days)
        except Exception as exc:  # transport failure: rows stay pending
            report.errors.append(f"{sport}: {exc!r}")
            continue
        for score in parse_scores_payload(payload):
            scores_by_match[score.match_id] = score

    for row in pending:
        dedupe = row["dedupe_key"]
        score = scores_by_match.get(row["match_id"])
        if score is None:
            report.skipped_no_scores += 1
            continue

        if not score.completed:
            if (score.status in _NEVER_COMPLETED
                    and _hours_since(row["commence_time"], now)
                    >= settings.settle_after_hours + settings.settle_grace_hours):
                if storage.settle_pick(dedupe, "VOID", now, state="VOID"):
                    report.void += 1
                    report.settled_picks.append({
                        "dedupe_key": dedupe,
                        "match_id": row.get("match_id"),
                        "sport_key": row.get("sport_key"),
                        "market": row.get("market"),
                        "outcome_name": row.get("outcome_name"),
                        "p_true": row.get("p_true"),
                        "line": row.get("line"),
                        "result": "VOID",
                        "state": "VOID",
                    })
            else:
                report.skipped_not_due += 1
            continue

        grade = score.grade_pick(row["market"], row["outcome_name"], row.get("line"))
        if grade is None:
            report.skipped_no_scores += 1
            continue

        if grade == "VOID":
            if storage.settle_pick(dedupe, "VOID", now, state="VOID"):
                report.void += 1
                report.settled_picks.append({
                    "dedupe_key": dedupe,
                    "match_id": row.get("match_id"),
                    "sport_key": row.get("sport_key"),
                    "market": row.get("market"),
                    "outcome_name": row.get("outcome_name"),
                    "p_true": row.get("p_true"),
                    "line": row.get("line"),
                    "result": "VOID",
                    "state": "VOID",
                })
        else:
            if storage.settle_pick(dedupe, grade, now, state="SETTLED"):
                report.settled += 1
                if grade == "WIN":
                    report.won += 1
                else:
                    report.lost += 1
                report.settled_picks.append({
                    "dedupe_key": dedupe,
                    "match_id": row.get("match_id"),
                    "sport_key": row.get("sport_key"),
                    "market": row.get("market"),
                    "outcome_name": row.get("outcome_name"),
                    "p_true": row.get("p_true"),
                    "line": row.get("line"),
                    "result": grade,
                    "state": "SETTLED",
                })

    return report


def _hours_since(iso_commence: str, now: datetime) -> float:
    commence = _parse_dt(iso_commence)
    if commence is None:
        return float("inf")
    return (now - commence).total_seconds() / 3600.0