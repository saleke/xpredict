"""Validation metrics — every cycle / settlement / pick logged as JSONL.

This is the measurement harness behind the live-fire validation run: run the
scheduler with ``--metrics data/metrics.jsonl`` for a week, then print the
weekly summary with ``lisa report``. It answers the product's open questions
without manual data collection:

  * pick volume at the gate threshold (and per-reason suppression)
  * the real EV distribution across emitted picks (+EV share)
  * settlement accuracy (won / lost / void / pending mix)
  * error rate and credit pressure
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .odds import utcnow
from .pipeline import CycleReport
from .settle import SettlementReport
from .storage import Storage


class Tracker:
    """Appends compact records to a JSONL file and summarises them."""

    def __init__(self, path: str | Path, storage: Optional[Storage] = None):
        self.path = Path(path)
        self.storage = storage
        self._seen_picks: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- recording ------------------------------------------------------------

    def _append(self, record: dict, ts: Optional[datetime] = None) -> None:
        record.setdefault("ts", (ts or utcnow()).isoformat())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def record_cycles(self, reports: Iterable[CycleReport],
                      ts: Optional[datetime] = None) -> None:
        for r in reports:
            self._append({
                "kind": "cycle",
                "sport": r.sport_key,
                "matches_seen": r.matches_seen,
                "matches_refined": r.matches_refined,
                "picks_emitted": r.picks_emitted,
                "suppressed": r.suppressed,
                "errors": r.errors,
            }, ts=ts)

    def record_settlement(self, report: SettlementReport,
                          ts: Optional[datetime] = None) -> None:
        self._append({
            "kind": "settlement",
            "pending": report.pending,
            "settled": report.settled,
            "won": report.won,
            "lost": report.lost,
            "void": report.void,
            "skipped_no_scores": report.skipped_no_scores,
            "skipped_not_due": report.skipped_not_due,
            "errors": report.errors,
        }, ts=ts)

    def record_picks(self, ts: Optional[datetime] = None) -> None:
        """Snapshot every newly-seen pending pick once (deduped by key)."""
        if self.storage is None:
            return
        for row in self.storage.list_pending_picks():
            key = row["dedupe_key"]
            if key in self._seen_picks:
                continue
            self._seen_picks.add(key)
            self._append({
                "kind": "pick",
                "dedupe_key": key,
                "match_id": row["match_id"],
                "sport": row["sport_key"],
                "outcome": row["outcome_name"],
                "p_true": row["p_true"],
                "fair_odds": row["fair_odds"],
                "cv": row["cv"],
                "best_book": row["best_book"],
                "best_ev": row["best_ev"],
            }, ts=ts)

    # -- summary --------------------------------------------------------------

    def summarize(self) -> dict:
        """Aggregate the JSONL trail into the weekly validation summary."""
        out = {
            "cycles": 0,
            "matches_seen": 0,
            "matches_refined": 0,
            "picks_emitted": 0,
            "suppression": {},
            "errors": 0,
            "picks": {
                "count": 0,
                "p_true_min": None, "p_true_max": None, "p_true_mean": None,
                "best_ev_mean": None, "positive_ev_share": None,
            },
            "settlement": {
                "runs": 0, "won": 0, "lost": 0, "void": 0,
                "settled": 0, "pending_last": 0,
            },
            "span_hours": 0.0,
            "picks_per_week": None,
        }
        times: list[datetime] = []
        evs: list[float] = []
        p_trues: list[float] = []

        if not self.path.exists():
            return out

        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                ts = rec.get("ts")
                if ts:
                    try:
                        times.append(datetime.fromisoformat(ts))
                    except ValueError:
                        pass
                kind = rec.get("kind")
                if kind == "cycle":
                    out["cycles"] += 1
                    out["matches_seen"] += rec.get("matches_seen", 0)
                    out["matches_refined"] += rec.get("matches_refined", 0)
                    out["picks_emitted"] += rec.get("picks_emitted", 0)
                    out["errors"] += len(rec.get("errors", []))
                    for why in rec.get("suppressed", []):
                        reason = why.split(":", 1)[1] if ":" in why else why
                        out["suppression"][reason] = (
                            out["suppression"].get(reason, 0) + 1)
                elif kind == "settlement":
                    s = out["settlement"]
                    s["runs"] += 1
                    s["won"] += rec.get("won", 0)
                    s["lost"] += rec.get("lost", 0)
                    s["void"] += rec.get("void", 0)
                    s["settled"] += rec.get("settled", 0)
                    s["pending_last"] = rec.get("pending", 0)
                    out["errors"] += len(rec.get("errors", []))
                elif kind == "pick":
                    p = out["picks"]
                    p["count"] += 1
                    p_trues.append(float(rec["p_true"]))
                    ev = rec.get("best_ev")
                    if ev is not None:
                        evs.append(float(ev))

        if times:
            out["span_hours"] = (max(times) - min(times)).total_seconds() / 3600.0
            weeks = out["span_hours"] / (7 * 24)
            out["picks_per_week"] = (
                round(out["picks"]["count"] / weeks, 1) if weeks > 0 else None)

        p = out["picks"]
        if p_trues:
            p["p_true_min"] = min(p_trues)
            p["p_true_max"] = max(p_trues)
            p["p_true_mean"] = sum(p_trues) / len(p_trues)
        if evs:
            p["best_ev_mean"] = sum(evs) / len(evs)
            p["positive_ev_share"] = sum(1 for e in evs if e > 0) / len(evs)
        return out