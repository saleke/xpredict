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
        self._seen_settled: set[str] = set()
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
        for p in report.settled_picks:
            key = p["dedupe_key"]
            if key in self._seen_settled:
                continue
            self._seen_settled.add(key)
            self._append({
                "kind": "settled_pick",
                "dedupe_key": key,
                "match_id": p.get("match_id"),
                "sport": p.get("sport_key"),
                "market": p.get("market"),
                "outcome": p.get("outcome_name"),
                "p_true": p.get("p_true"),
                "line": p.get("line"),
                "result": p.get("result"),
                "state": p.get("state"),
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

    def record_settled_picks(self, ts: Optional[datetime] = None) -> None:
        """Snapshot every newly-seen settled pick once (deduped by key)."""
        if self.storage is None:
            return
        for row in self.storage.list_settled_picks():
            key = row["dedupe_key"]
            if key in self._seen_settled:
                continue
            self._seen_settled.add(key)
            self._append({
                "kind": "settled_pick",
                "dedupe_key": key,
                "match_id": row["match_id"],
                "sport": row["sport_key"],
                "market": row.get("market"),
                "outcome": row["outcome_name"],
                "p_true": row["p_true"],
                "line": row.get("line"),
                "result": row.get("result"),
                "state": row.get("state"),
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
            "calibration": None,
            "calibration_by_market": {},
            "span_hours": 0.0,
            "picks_per_week": None,
        }
        times: list[datetime] = []
        evs: list[float] = []
        p_trues: list[float] = []
        settled_records: list[dict] = []

        if not self.path.exists():
            if self.storage is not None:
                settled_from_store = self.storage.list_settled_picks()
                if settled_from_store:
                    from .calibration import evaluate_calibration, evaluate_by_market
                    out["calibration"] = evaluate_calibration(settled_from_store).to_dict()
                    out["calibration_by_market"] = {
                        m: rep.to_dict()
                        for m, rep in evaluate_by_market(settled_from_store).items()
                    }
            return out

        seen_pick_keys: set[str] = set()
        seen_settled_keys: set[str] = set()
        seen_suppression: set[tuple[str, str]] = set()

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
                        # dedupe by (match, reason): a restarted daemon re-sees
                        # the same matches without a persistent seen-set
                        match_id, _, reason = why.partition(":")
                        if (match_id, reason) in seen_suppression:
                            continue
                        seen_suppression.add((match_id, reason))
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
                elif kind == "settled_pick":
                    key = rec.get("dedupe_key")
                    if key in seen_settled_keys:
                        continue
                    seen_settled_keys.add(key)
                    settled_records.append(rec)
                elif kind == "pick":
                    key = rec.get("dedupe_key")
                    if key in seen_pick_keys:
                        continue  # same pick re-recorded after a restart
                    seen_pick_keys.add(key)
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

        from .calibration import evaluate_calibration, evaluate_by_market
        if settled_records:
            out["calibration"] = evaluate_calibration(settled_records).to_dict()
            out["calibration_by_market"] = {
                m: rep.to_dict()
                for m, rep in evaluate_by_market(settled_records).items()
            }
        elif self.storage is not None:
            settled_from_store = self.storage.list_settled_picks()
            if settled_from_store:
                out["calibration"] = evaluate_calibration(settled_from_store).to_dict()
                out["calibration_by_market"] = {
                    m: rep.to_dict()
                    for m, rep in evaluate_by_market(settled_from_store).items()
                }
        return out