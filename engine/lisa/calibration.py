"""Quantitative calibration engine — Brier score, ECE, and reliability curves.

Evaluates how well LISA's consensus true probabilities (P_true) match settled
real-world outcomes (WIN/LOSS) across the audited ledger.

Metrics implemented:
  * Brier Score (mean squared error of probability forecasts)
  * Murphy (1973) Brier Score Decomposition:
      BS = Reliability - Resolution + Uncertainty
  * Expected Calibration Error (ECE) & Maximum Calibration Error (MCE)
  * Log Loss (Binary Cross-Entropy)
  * Multi-market breakdown (h2h, totals, spreads)
  * Pushes/Voids (VOID) strictly tracked and excluded from binary scoring
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence



DEFAULT_GATE_BINS: list[tuple[float, float]] = [
    (0.70, 0.75),
    (0.75, 0.80),
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.95),
    (0.95, 1.00),
]

DECIMAL_DECILE_BINS: list[tuple[float, float]] = [
    (round(i * 0.1, 1), round((i + 1) * 0.1, 1)) for i in range(10)
]


@dataclass(frozen=True)
class CalibrationBin:
    """Statistics for an empirical probability bin [lower, upper)."""
    lower: float
    upper: float
    count: int
    pred_mean: Optional[float]
    win_rate: Optional[float]
    bias: Optional[float]      # win_rate - pred_mean (+ underconfident, - overconfident)
    error: Optional[float]     # abs(win_rate - pred_mean)


@dataclass(frozen=True)
class CalibrationReport:
    """Comprehensive statistical calibration report."""
    total_evaluated: int
    n_won: int
    n_lost: int
    n_void: int
    win_rate: Optional[float]
    pred_mean: Optional[float]
    brier_score: Optional[float]
    ece: Optional[float]
    mce: Optional[float]
    log_loss: Optional[float]
    reliability: Optional[float]
    resolution: Optional[float]
    uncertainty: Optional[float]
    bins: list[CalibrationBin] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_evaluated": self.total_evaluated,
            "n_won": self.n_won,
            "n_lost": self.n_lost,
            "n_void": self.n_void,
            "win_rate": self.win_rate,
            "pred_mean": self.pred_mean,
            "brier_score": self.brier_score,
            "ece": self.ece,
            "mce": self.mce,
            "log_loss": self.log_loss,
            "reliability": self.reliability,
            "resolution": self.resolution,
            "uncertainty": self.uncertainty,
            "bins": [
                {
                    "lower": b.lower,
                    "upper": b.upper,
                    "count": b.count,
                    "pred_mean": b.pred_mean,
                    "win_rate": b.win_rate,
                    "bias": b.bias,
                    "error": b.error,
                }
                for b in self.bins
            ],
        }


def compute_brier_score(predictions: Sequence[float],
                        outcomes: Sequence[float]) -> float:
    """Mean squared error of binary probability forecasts.

    BS = (1/N) * sum((p_i - y_i)^2). Range [0, 1]. Lower is better.
    """
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"Length mismatch: {len(predictions)} preds vs {len(outcomes)} outcomes")
    if not predictions:
        raise ValueError("Cannot compute Brier score on empty sequences")
    return sum((p - y) ** 2 for p, y in zip(predictions, outcomes)) / len(predictions)


def compute_log_loss(predictions: Sequence[float],
                     outcomes: Sequence[float],
                     eps: float = 1e-15) -> float:
    """Binary cross-entropy with defensive numerical clamping."""
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"Length mismatch: {len(predictions)} preds vs {len(outcomes)} outcomes")
    if not predictions:
        raise ValueError("Cannot compute log loss on empty sequences")

    total = 0.0
    for p, y in zip(predictions, outcomes):
        p_clamped = min(max(p, eps), 1.0 - eps)
        total += -(y * math.log(p_clamped) + (1.0 - y) * math.log(1.0 - p_clamped))
    return total / len(predictions)


def _resolve_bins(predictions: Sequence[float],
                  bin_edges: Optional[Sequence[tuple[float, float]]]) -> list[tuple[float, float]]:
    if bin_edges is not None:
        return list(bin_edges)
    # If any prediction is below 0.70, use 10 standard deciles
    if any(p < 0.70 for p in predictions):
        return DECIMAL_DECILE_BINS
    return DEFAULT_GATE_BINS


def compute_ece_mce(predictions: Sequence[float],
                    outcomes: Sequence[float],
                    bin_edges: Optional[Sequence[tuple[float, float]]] = None
                    ) -> tuple[float, float, list[CalibrationBin]]:
    """Compute Expected Calibration Error (ECE), Maximum Calibration Error (MCE),
    and per-bin summary statistics."""
    n = len(predictions)
    if n == 0:
        return 0.0, 0.0, []

    edges = _resolve_bins(predictions, bin_edges)
    bin_items: list[list[tuple[float, float]]] = [[] for _ in edges]

    for p, y in zip(predictions, outcomes):
        placed = False
        for idx, (lo, hi) in enumerate(edges):
            # Last bin includes upper boundary 1.0
            is_last = (idx == len(edges) - 1)
            if (lo <= p < hi) or (is_last and lo <= p <= hi):
                bin_items[idx].append((p, y))
                placed = True
                break
        if not placed:
            # Fallback if prediction is slightly out of edge bounds
            if p < edges[0][0]:
                bin_items[0].append((p, y))
            else:
                bin_items[-1].append((p, y))

    cal_bins: list[CalibrationBin] = []
    ece = 0.0
    mce = 0.0

    for (lo, hi), items in zip(edges, bin_items):
        count = len(items)
        if count == 0:
            cal_bins.append(CalibrationBin(
                lower=lo, upper=hi, count=0,
                pred_mean=None, win_rate=None, bias=None, error=None,
            ))
            continue

        p_mean = sum(p for p, _ in items) / count
        w_rate = sum(y for _, y in items) / count
        bias = w_rate - p_mean
        err = abs(bias)

        ece += (count / n) * err
        if err > mce:
            mce = err

        cal_bins.append(CalibrationBin(
            lower=lo, upper=hi, count=count,
            pred_mean=p_mean, win_rate=w_rate, bias=bias, error=err,
        ))

    return ece, mce, cal_bins


def evaluate_calibration(records: Iterable[dict],
                         bin_edges: Optional[Sequence[tuple[float, float]]] = None
                         ) -> CalibrationReport:
    """Evaluate full calibration statistics over a collection of settled pick rows.

    Expected fields per record:
      - 'p_true': float (predicted consensus true probability)
      - 'result': 'WIN', 'LOSS', or 'VOID' (or binary 1/0)
    """
    preds: list[float] = []
    outs: list[float] = []
    n_won = 0
    n_lost = 0
    n_void = 0

    from .contracts import is_binary_contract
    for rec in records:
        if not is_binary_contract(rec.get("market"), rec.get("line")):
            continue
        res = rec.get("result")
        if res == "VOID" or rec.get("state") == "VOID":
            n_void += 1
            continue

        p_val = rec.get("p_true")
        if p_val is None:
            continue
        try:
            p = float(p_val)
        except (ValueError, TypeError):
            continue

        if not (0.0 <= p <= 1.0):
            continue

        if res in ("WIN", 1, 1.0, "1", True):
            outs.append(1.0)
            preds.append(p)
            n_won += 1
        elif res in ("LOSS", 0, 0.0, "0", False):
            outs.append(0.0)
            preds.append(p)
            n_lost += 1
        else:
            # Unsettled or unknown status: skip
            continue

    n = len(preds)
    if n == 0:
        return CalibrationReport(
            total_evaluated=0,
            n_won=0,
            n_lost=0,
            n_void=n_void,
            win_rate=None,
            pred_mean=None,
            brier_score=None,
            ece=None,
            mce=None,
            log_loss=None,
            reliability=None,
            resolution=None,
            uncertainty=None,
            bins=[],
        )

    base_rate = sum(outs) / n
    p_mean = sum(preds) / n
    bs = compute_brier_score(preds, outs)
    ll = compute_log_loss(preds, outs)
    ece, mce, bins = compute_ece_mce(preds, outs, bin_edges=bin_edges)

    # Murphy (1973) decomposition across empirical bins
    uncertainty = base_rate * (1.0 - base_rate)
    reliability = 0.0
    resolution = 0.0
    for b in bins:
        if b.count > 0 and b.pred_mean is not None and b.win_rate is not None:
            weight = b.count / n
            reliability += weight * ((b.pred_mean - b.win_rate) ** 2)
            resolution += weight * ((b.win_rate - base_rate) ** 2)

    return CalibrationReport(
        total_evaluated=n,
        n_won=n_won,
        n_lost=n_lost,
        n_void=n_void,
        win_rate=base_rate,
        pred_mean=p_mean,
        brier_score=bs,
        ece=ece,
        mce=mce,
        log_loss=ll,
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
        bins=bins,
    )


def evaluate_by_market(records: Iterable[dict],
                       bin_edges: Optional[Sequence[tuple[float, float]]] = None
                       ) -> dict[str, CalibrationReport]:
    """Segment settled picks by market ('h2h', 'totals', 'spreads') and evaluate
    calibration for each segment, plus 'overall'."""
    by_market: dict[str, list[dict]] = {}
    all_records: list[dict] = []

    for r in records:
        all_records.append(r)
        m = str(r.get("market") or "unknown")
        by_market.setdefault(m, []).append(r)

    out: dict[str, CalibrationReport] = {
        "overall": evaluate_calibration(all_records, bin_edges=bin_edges)
    }
    for m, recs in sorted(by_market.items()):
        out[m] = evaluate_calibration(recs, bin_edges=bin_edges)

    return out


def format_calibration_report(report: CalibrationReport,
                              title: str = "LISA Calibration Report",
                              clv_report: Optional["CLVReport"] = None) -> str:
    """Format calibration metrics into a clean, human-readable terminal table."""
    lines: list[str] = [
        f"=== {title} ===",
        f"Settled Picks Evaluated: {report.total_evaluated}  (Won: {report.n_won}, Lost: {report.n_lost}, Void: {report.n_void})",
    ]
    if report.total_evaluated == 0:
        lines.append("No settled non-void picks available to evaluate.")
        return "\n".join(lines)

    wr = f"{report.win_rate * 100:.1f}%" if report.win_rate is not None else "N/A"
    pm = f"{report.pred_mean * 100:.1f}%" if report.pred_mean is not None else "N/A"
    bs = f"{report.brier_score:.4f}" if report.brier_score is not None else "N/A"
    ece = f"{report.ece * 100:.2f}%" if report.ece is not None else "N/A"
    mce = f"{report.mce * 100:.2f}%" if report.mce is not None else "N/A"
    ll = f"{report.log_loss:.4f}" if report.log_loss is not None else "N/A"

    rel = f"{report.reliability:.4f}" if report.reliability is not None else "N/A"
    res = f"{report.resolution:.4f}" if report.resolution is not None else "N/A"
    unc = f"{report.uncertainty:.4f}" if report.uncertainty is not None else "N/A"

    lines.append(f"Win Rate:        {wr:>8}  |  Avg Predicted Prob: {pm:>8}")
    lines.append(f"Brier Score:     {bs:>8}  |  Log Loss:           {ll:>8}")
    lines.append(f"ECE:             {ece:>8}  |  MCE:                {mce:>8}")
    lines.append(f"Murphy Decomp:   Rel={rel}  Res={res}  Unc={unc}")
    lines.append("")
    lines.append("Bin Range      | Count | Pred Mean | Win Rate | Bias   | Error")
    lines.append("---------------------------------------------------------------")

    for b in report.bins:
        rng = f"[{b.lower:.2f}, {b.upper:.2f})"
        if b.count == 0:
            lines.append(f"{rng:<14} | {b.count:>5} |      --   |      --  |    --  |    --")
        else:
            p_str = f"{b.pred_mean * 100:.1f}%" if b.pred_mean is not None else "--"
            w_str = f"{b.win_rate * 100:.1f}%" if b.win_rate is not None else "--"
            bias_str = f"{b.bias * 100:+.1f}%" if b.bias is not None else "--"
            err_str = f"{b.error * 100:.1f}%" if b.error is not None else "--"
            lines.append(f"{rng:<14} | {b.count:>5} | {p_str:>9} | {w_str:>8} | {bias_str:>6} | {err_str:>6}")

    if clv_report is not None and clv_report.count > 0:
        lines.append("")
        lines.append(format_clv_report(clv_report))

    return "\n".join(lines)


@dataclass(frozen=True)
class CLVReport:
    """Quantitative Closing Line Value (CLV) analytics report."""
    count: int
    mean_clv: Optional[float]
    median_clv: Optional[float]
    positive_clv_share: Optional[float]
    min_clv: Optional[float]
    max_clv: Optional[float]
    win_rate_positive_clv: Optional[float]
    win_rate_negative_clv: Optional[float]

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "mean_clv": self.mean_clv,
            "median_clv": self.median_clv,
            "positive_clv_share": self.positive_clv_share,
            "min_clv": self.min_clv,
            "max_clv": self.max_clv,
            "win_rate_positive_clv": self.win_rate_positive_clv,
            "win_rate_negative_clv": self.win_rate_negative_clv,
        }


def compute_clv_metrics(records: Iterable[dict]) -> CLVReport:
    """Compute summary Closing Line Value metrics over picks with recorded CLV.

    Expected fields per record:
      - 'clv': float (e.g. (best_odds / closing_odds) - 1.0)
      - 'result': 'WIN', 'LOSS', or 'VOID' (optional, for correlation)
    """
    clvs: list[float] = []
    pos_res: list[bool] = []
    neg_res: list[bool] = []

    for r in records:
        clv_val = r.get("clv")
        if clv_val is None:
            continue
        try:
            val = float(clv_val)
        except (ValueError, TypeError):
            continue
        clvs.append(val)

        res = r.get("result")
        if res in ("WIN", "LOSS"):
            is_win = (res == "WIN")
            if val > 0:
                pos_res.append(is_win)
            else:
                neg_res.append(is_win)

    n = len(clvs)
    if n == 0:
        return CLVReport(
            count=0,
            mean_clv=None,
            median_clv=None,
            positive_clv_share=None,
            min_clv=None,
            max_clv=None,
            win_rate_positive_clv=None,
            win_rate_negative_clv=None,
        )

    return CLVReport(
        count=n,
        mean_clv=sum(clvs) / n,
        median_clv=statistics.median(clvs),
        positive_clv_share=sum(1 for v in clvs if v > 0) / n,
        min_clv=min(clvs),
        max_clv=max(clvs),
        win_rate_positive_clv=(sum(pos_res) / len(pos_res)) if pos_res else None,
        win_rate_negative_clv=(sum(neg_res) / len(neg_res)) if neg_res else None,
    )


def evaluate_clv_by_market(records: Iterable[dict]) -> dict[str, CLVReport]:
    """Segment CLV metrics by market ('h2h', 'totals', 'spreads') and 'overall'."""
    by_market: dict[str, list[dict]] = {}
    all_records: list[dict] = []

    for r in records:
        all_records.append(r)
        m = str(r.get("market") or "unknown")
        by_market.setdefault(m, []).append(r)

    out: dict[str, CLVReport] = {
        "overall": compute_clv_metrics(all_records)
    }
    for m, recs in sorted(by_market.items()):
        out[m] = compute_clv_metrics(recs)

    return out


def format_clv_report(report: CLVReport,
                      title: str = "LISA Closing Line Value (CLV) Performance") -> str:
    """Format CLV metrics into a clean, human-readable terminal table."""
    lines: list[str] = [
        f"--- {title} ---",
        f"Picks Evaluated: {report.count}",
    ]
    if report.count == 0:
        lines.append("No picks with CLV metrics available to evaluate.")
        return "\n".join(lines)

    mean_s = f"{report.mean_clv * 100:+.2f}%" if report.mean_clv is not None else "N/A"
    med_s = f"{report.median_clv * 100:+.2f}%" if report.median_clv is not None else "N/A"
    share_s = f"{report.positive_clv_share * 100:.1f}%" if report.positive_clv_share is not None else "N/A"
    min_s = f"{report.min_clv * 100:+.2f}%" if report.min_clv is not None else "N/A"
    max_s = f"{report.max_clv * 100:+.2f}%" if report.max_clv is not None else "N/A"

    wr_pos = f"{report.win_rate_positive_clv * 100:.1f}%" if report.win_rate_positive_clv is not None else "N/A"
    wr_neg = f"{report.win_rate_negative_clv * 100:.1f}%" if report.win_rate_negative_clv is not None else "N/A"

    lines.append(f"Mean CLV:        {mean_s:>8}  |  Median CLV:         {med_s:>8}")
    lines.append(f"Beat-Close Share:{share_s:>8}  |  CLV Range: [{min_s} .. {max_s}]")
    lines.append(f"Win Rate (+CLV): {wr_pos:>8}  |  Win Rate (<=0 CLV): {wr_neg:>8}")
    return "\n".join(lines)

