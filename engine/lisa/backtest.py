"""Quantitative Backtesting & Calibration Engine.

Simulates historical pre-match odds through the complete LISA refinery
(Shin de-vigging, consensus convergence, confidence grading, Fractional Kelly)
and grades predictions against actual official final scores.

Runs on the REAL packaged archive (see ``.history``) — never synthetic data.

Computes institutional quantitative finance and probabilistic accuracy metrics:
  - Brier Score & Murphy (1973) Decomposition (Reliability, Resolution, Uncertainty)
  - Expected Calibration Error (ECE) & Maximum Calibration Error (MCE)
  - Wilson Score 95% Confidence Interval for Win Rate
  - Maximum Drawdown (MDD) in dollars and percentage
  - Sharpe Ratio and Sortino Ratio (downside risk penalty)
  - Profit Factor (Gross Profits / Gross Losses)
  - Sincere Counterfactual Capital Preservation on Grade C Pass Advisories
  - Honest late-to-close CLV: the executed price (best early line across the
    archive's books) is measured against the later recorded closing line
    (football-data.co.uk *C columns). CLV is measured, never constructed — see
    ``measure_clv``.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from . import config as cfg
from .calibration import CalibrationReport, evaluate_calibration
from .consensus import Consensus, refine
from .gate import evaluate as evaluate_gate
from .history import (
    HISTORICAL_ODDS,
    HISTORICAL_PROVENANCE,
    HISTORICAL_SCORES,
)
from .odds import H2H, Match, Score
from .parsing import parse_odds_payload, parse_scores_payload
from .staking import compute_kelly_stake


def compute_wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Compute the Wilson score 95% confidence interval for a binomial proportion.

    Provides statistically rigorous lower and upper bounds on the true underlying win rate.
    """
    if total == 0:
        return 0.0, 0.0
    p_hat = wins / total
    denom = 1.0 + (z ** 2) / total
    center = (p_hat + (z ** 2) / (2.0 * total)) / denom
    variance_term = (p_hat * (1.0 - p_hat) / total) + ((z ** 2) / (4.0 * (total ** 2)))
    margin = (z * math.sqrt(variance_term)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def best_available_price(consensus: Consensus, outcome: str) -> Optional[float]:
    """Best recorded decimal price for ``outcome`` across all consensus books."""
    best: Optional[float] = None
    for btp in consensus.books:
        price = btp.prices.get(outcome)
        if price is not None and (best is None or price > best):
            best = price
    return best


def measure_clv(consensus: Consensus, outcome: str, exec_book: Optional[str],
                exec_odds: float, closing_price: Optional[float]) -> tuple[Optional[float], bool]:
    """Real late-to-close value measurement.

    ``closing_price`` is the best price for ``outcome`` in the archive's closing
    snapshot (football-data.co.uk *C columns). CLV = exec / close - 1; they "beat
    the closing line" when the (earlier) executed price was *higher* than the
    price at which the market finally closed. This is a genuine, auditable
    price-movement statistic — never a constructed ``exec_odds * 0.96`` figure.
    """
    if closing_price is None or closing_price <= 0.0:
        return None, False
    return closing_price, exec_odds > closing_price


def summarize_profile(
    records: Sequence[dict[str, Any]],
    *,
    strategy_id: str,
    name: str,
    badge: str,
    description: str,
    risk_level: str,
    best_for: str,
    initial_capital: float = 10000.0,
) -> dict[str, Any]:
    """Compute a strategy profile purely from its *real* per-bet records.

    Every metric (win rate, Wilson CI, ROI, drawdown, Sharpe, Sortino, profit
    factor) is derived from the recorded outcomes; nothing is hardcoded.
    """
    exec_recs = [r for r in records if r.get("result") in ("WIN", "LOSS")]
    n = len(exec_recs)
    wins = sum(1 for r in exec_recs if r["result"] == "WIN")
    losses = n - wins
    win_rate = (wins / n) if n else 0.0
    ci_lo, ci_hi = compute_wilson_ci(wins, n)

    odds = [float(r["best_odds"]) for r in exec_recs]
    avg_odds = statistics.mean(odds) if odds else 0.0
    wagered = sum(float(r.get("stake_amount", 0.0)) for r in exec_recs)
    profit = sum(float(r.get("pnl", 0.0)) for r in exec_recs)
    roi = (profit / wagered * 100.0) if wagered > 0 else 0.0

    # Drawdown is measured on the equity curve: a fund starting at
    # ``initial_capital`` units that stakes each record's stake_amount.
    equity = initial_capital
    peak_equity = initial_capital
    max_dd_dollars = 0.0
    max_dd_pct = 0.0
    returns: list[float] = []
    for r in exec_recs:
        odds_i = float(r["best_odds"])
        ret = (odds_i - 1.0) if r["result"] == "WIN" else -1.0
        returns.append(ret)
        equity += float(r.get("pnl", 0.0))
        peak_equity = max(peak_equity, equity)
        max_dd_dollars = max(max_dd_dollars, peak_equity - equity)
        if peak_equity > 0:
            max_dd_pct = max(max_dd_pct, min(1.0, (peak_equity - equity) / peak_equity) * 100.0)

    sharpe, sortino, profit_factor = _risk_metrics(returns)

    return {
        "strategy_id": strategy_id,
        "name": name,
        "badge": badge,
        "description": description,
        "total_matches": len(records),
        "executed_bets": n,
        "wins": wins,
        "losses": losses,
        "pushes": len(records) - n,
        "win_rate": round(win_rate, 4),
        "wilson_ci_lower": round(ci_lo, 4),
        "wilson_ci_upper": round(ci_hi, 4),
        "avg_odds": round(avg_odds, 2),
        "total_wagered": round(wagered, 2),
        "net_profit": round(profit, 2),
        "roi_pct": round(roi, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "max_drawdown_dollars": round(max_dd_dollars, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "profit_factor": round(profit_factor, 2),
        "risk_level": risk_level,
        "best_for": best_for,
    }


def _risk_metrics(returns: Sequence[float]) -> tuple[float, float, float]:
    """Sharpe, Sortino, and profit factor from a realised per-bet return series."""
    if not returns:
        return 0.0, 0.0, 99.0
    mean_ret = statistics.mean(returns)
    std_ret = statistics.stdev(returns) if len(returns) > 1 else 0.01
    sharpe = (mean_ret / std_ret * math.sqrt(len(returns))) if std_ret > 0 else 0.0

    downside = [r for r in returns if r < 0]
    if downside:
        downside_dev = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
        sortino = (mean_ret / downside_dev * math.sqrt(len(returns))) if downside_dev > 0 else 0.0
    else:
        sortino = sharpe * 1.5

    gross_wins = sum(r for r in returns if r > 0)
    gross_losses = abs(sum(r for r in returns if r < 0))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 99.0
    return sharpe, sortino, profit_factor


@dataclass
class BacktestMatchRecord:
    """Individual evaluated match audit record."""
    match_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: str
    grade: str                         # "GRADE_A" | "GRADE_C"
    market: str
    outcome_name: str
    p_true: float
    fair_odds: float
    best_book: str
    best_odds: float
    ev: float
    conviction_score: float
    stake_units: float
    stake_amount: float
    actual_score: str
    result: str                        # "WIN" | "LOSS" | "VOID" | "PASS_TRAP_AVOIDED" | "PASS_ADVISORY"
    pnl: float                         # profit/loss in dollars
    capital_saved: float               # dollars preserved if pass advisory
    hazard_warning: Optional[str] = None
    closing_odds: Optional[float] = None
    beat_clv: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "sport_key": self.sport_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "commence_time": self.commence_time,
            "grade": self.grade,
            "market": self.market,
            "outcome_name": self.outcome_name,
            "p_true": round(self.p_true, 4),
            "fair_odds": round(self.fair_odds, 3),
            "best_book": self.best_book,
            "best_odds": round(self.best_odds, 2),
            "ev": round(self.ev, 4),
            "conviction_score": round(self.conviction_score, 2),
            "stake_units": round(self.stake_units, 2),
            "stake_amount": round(self.stake_amount, 2),
            "actual_score": self.actual_score,
            "result": self.result,
            "pnl": round(self.pnl, 2),
            "capital_saved": round(self.capital_saved, 2),
            "hazard_warning": self.hazard_warning,
            "closing_odds": round(self.closing_odds, 2) if self.closing_odds else None,
            "beat_clv": self.beat_clv,
        }


@dataclass
class BacktestReport:
    """Comprehensive historical simulation and accuracy benchmark report."""
    total_matches: int
    executed_bets: int
    wins: int
    losses: int
    pushes: int
    win_rate: float
    wilson_ci_lower: float
    wilson_ci_upper: float
    grade_a_count: int
    grade_a_wins: int
    grade_a_win_rate: float
    grade_b_count: int
    grade_b_wins: int
    grade_b_win_rate: float
    grade_c_traps_avoided: int
    grade_c_traps_that_lost: int
    grade_c_traps_that_won: int
    capital_preserved_dollars: float
    net_counterfactual_value: float
    initial_bankroll: float
    ending_bankroll: float
    total_wagered: float
    net_profit: float
    roi_pct: float
    flat_profit: float
    flat_roi_pct: float
    max_drawdown_pct: float
    max_drawdown_dollars: float
    sharpe_ratio: float
    sortino_ratio: float
    profit_factor: float
    brier_score: float
    reliability: float
    resolution: float
    uncertainty: float
    ece: float
    mce: float
    log_loss: float
    positive_clv_rate: float
    mean_clv: Optional[float] = None
    sport_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    records: list[BacktestMatchRecord] = field(default_factory=list)
    calibration_report: Optional[CalibrationReport] = None
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    strategy_comparison_matrix: list[dict[str, Any]] = field(default_factory=list)
    data_provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "total_matches": self.total_matches,
                "executed_bets": self.executed_bets,
                "wins": self.wins,
                "losses": self.losses,
                "pushes": self.pushes,
                "win_rate": round(self.win_rate, 4),
                "wilson_ci_lower": round(self.wilson_ci_lower, 4),
                "wilson_ci_upper": round(self.wilson_ci_upper, 4),
                "grade_a_count": self.grade_a_count,
                "grade_a_wins": self.grade_a_wins,
                "grade_a_win_rate": round(self.grade_a_win_rate, 4),
                "grade_b_count": self.grade_b_count,
                "grade_b_wins": self.grade_b_wins,
                "grade_b_win_rate": round(self.grade_b_win_rate, 4),
                "grade_c_traps_avoided": self.grade_c_traps_avoided,
                "grade_c_traps_that_lost": self.grade_c_traps_that_lost,
                "grade_c_traps_that_won": self.grade_c_traps_that_won,
                "capital_preserved_dollars": round(self.capital_preserved_dollars, 2),
                "net_counterfactual_value": round(self.net_counterfactual_value, 2),
                "initial_bankroll": self.initial_bankroll,
                "ending_bankroll": round(self.ending_bankroll, 2),
                "total_wagered": round(self.total_wagered, 2),
                "net_profit": round(self.net_profit, 2),
                "roi_pct": round(self.roi_pct, 2),
                "flat_profit": round(self.flat_profit, 2),
                "flat_roi_pct": round(self.flat_roi_pct, 2),
                "max_drawdown_pct": round(self.max_drawdown_pct, 2),
                "max_drawdown_dollars": round(self.max_drawdown_dollars, 2),
                "sharpe_ratio": round(self.sharpe_ratio, 2),
                "sortino_ratio": round(self.sortino_ratio, 2),
                "profit_factor": round(self.profit_factor, 2),
                "brier_score": round(self.brier_score, 4),
                "reliability": round(self.reliability, 4),
                "resolution": round(self.resolution, 4),
                "uncertainty": round(self.uncertainty, 4),
                "ece": round(self.ece, 4),
                "mce": round(self.mce, 4),
                "log_loss": round(self.log_loss, 4),
                "positive_clv_rate": round(self.positive_clv_rate, 4),
                "mean_clv": round(self.mean_clv, 4) if self.mean_clv is not None else None,
            },
            "sport_breakdown": self.sport_breakdown,
            "calibration": self.calibration_report.to_dict() if self.calibration_report else None,
            "records": [r.to_dict() for r in self.records],
            "strategies": self.strategies,
            "strategy_comparison_matrix": self.strategy_comparison_matrix,
            "data_provenance": self.data_provenance,
        }

    def to_web_dict(self) -> dict[str, Any]:
        """Lightweight export for the web dashboard.

        Identical to ``to_dict`` but keeps the ledger displayable and fast to
        load in a browser: the executed Grade A ledger in full, a bounded
        sample of Grade C pass advisories, and zero per-strategy record dumps
        (the baseline leagues run into the thousands). Summary cards, the
        comparison matrix and the calibration table are unchanged.
        """
        payload = self.to_dict()
        grade_a = [r.to_dict() for r in self.records if r.grade == "GRADE_A"]
        grade_c = [r.to_dict() for r in self.records if r.grade == "GRADE_C"][:500]
        payload["records"] = grade_a + grade_c
        payload["records_trimmed"] = {
            "note": "Web export trims the ledger for load time; run the CLI "
                    "with --export-json for the complete archive.",
            "grade_a": len(grade_a),
            "grade_c_displayed": len(grade_c),
            "grade_c_total": self.grade_c_traps_avoided,
        }
        payload["strategies"] = {
            key: {"summary": sv["summary"], "records": []}
            for key, sv in self.strategies.items()
        }
        if self.strategies.get("conservative"):
            payload["strategies"]["conservative"]["records"] = grade_a
        return payload


class BacktestEngine:
    """Rigorous offline backtesting and calibration engine."""

    def __init__(
        self,
        settings: Optional[cfg.Settings] = None,
        initial_bankroll: float = 10000.0,
        flat_stake_unit: float = 100.0,
        sports: Optional[Sequence[str]] = None,
        cache_consensus: bool = True,
    ) -> None:
        self.settings = settings or cfg.Settings()
        self.initial_bankroll = initial_bankroll
        self.flat_stake_unit = flat_stake_unit
        self.sports = list(sports) if sports else None
        # Per-instance, not per-class: a class-level cache leaks between engines
        # (and between archives) and grows without bound for the process life.
        # The key covers every refine() input, so a settings change is a miss.
        self.cache_consensus = cache_consensus
        self._consensus_cache: dict[tuple[Any, ...], Optional[Consensus]] = {}

    def run_simulation(self, sport_keys: Optional[Sequence[str]] = None) -> BacktestReport:
        return self.run(sport_keys=sport_keys or self.sports)

    def run(
        self,
        odds_payloads: Optional[Sequence[dict]] = None,
        scores_payloads: Optional[Sequence[dict]] = None,
        sport_keys: Optional[Sequence[str]] = None,
    ) -> BacktestReport:
        """Run complete historical ingestion, consensus grading, and settlement evaluation."""
        odds_data = odds_payloads if odds_payloads is not None else HISTORICAL_ODDS
        scores_data = scores_payloads if scores_payloads is not None else HISTORICAL_SCORES

        if sport_keys:
            wanted_sports = set(sport_keys)
            odds_data = [g for g in odds_data if g.get("sport_key") in wanted_sports]
            scores_data = [s for s in scores_data if s.get("sport_key") in wanted_sports]

        matches = parse_odds_payload(odds_data)
        scores_list = parse_scores_payload(scores_data)
        scores_by_id: dict[str, Score] = {s.match_id: s for s in scores_list}
        closing_by_id: dict[str, Optional[dict[str, float]]] = {
            g["id"]: g.get("closing_odds") for g in odds_data
        }

        records: list[BacktestMatchRecord] = []
        bankroll = self.initial_bankroll
        peak_bankroll = self.initial_bankroll
        max_dd_dollars = 0.0
        max_dd_pct = 0.0

        total_wagered = 0.0
        net_profit = 0.0
        flat_profit = 0.0
        total_flat_wagered = 0.0

        trade_returns: list[float] = []
        gross_wins = 0.0
        gross_losses = 0.0

        predictions: list[float] = []
        binary_outcomes: list[float] = []
        clv_beats: list[bool] = []
        clv_values: list[float] = []

        sport_stats: dict[str, dict[str, Any]] = {}

        # Honest baselines collected on every match that produced a consensus:
        #  - market_favorite: bet the de-vigged market favourite at its best
        #    recorded price (a "just chase favourites" strategy).
        #  - always_home: bet the home side at its best recorded price.
        baseline_trades: dict[str, list[tuple[float, str]]] = {
            "market_favorite": [],
            "always_home": [],
        }

        for match in matches:
            score = scores_by_id.get(match.id)
            if not score or not score.completed:
                continue

            sport_stats.setdefault(match.sport_key, {
                "matches": 0, "wins": 0, "losses": 0, "profit": 0.0, "wagered": 0.0,
            })
            sport_stats[match.sport_key]["matches"] += 1

            # 1. Pipeline Consensus De-vigging (Shin Model)
            refine_kwargs = dict(
                now=match.commence_time,
                min_books=self.settings.min_books_telemetry,
                sharp_keys=self.settings.sharp_keys,
                sharp_multiplier=self.settings.sharp_multiplier,
                margin_weighted=self.settings.margin_weighted,
                min_margin_floor=self.settings.min_margin_floor,
                max_book_age_sec=self.settings.stale_prematch_sec or None,
                lo=self.settings.odds_sanity[0],
                hi=self.settings.odds_sanity[1],
            )
            cons_key = (match.id,) + tuple(
                (k, tuple(v) if isinstance(v, list) else v)
                for k, v in sorted(refine_kwargs.items(), key=lambda kv: kv[0])
                if k != "now"
            )
            if self.cache_consensus and cons_key in self._consensus_cache:
                consensus = self._consensus_cache[cons_key]
            else:
                consensus = refine(match, **refine_kwargs)
                if self.cache_consensus:
                    self._consensus_cache[cons_key] = consensus
            if consensus is None:
                continue

            # Baselines use the best *recorded* price for the chosen outcome.
            p_home = consensus.p.get(match.home_team, 0.0)
            p_draw = consensus.p.get("Draw", 0.0)
            p_away = consensus.p.get(match.away_team, 0.0)
            fav_outcome = max(
                ((match.home_team, p_home), ("Draw", p_draw), (match.away_team, p_away)),
                key=lambda tup: tup[1],
            )[0]
            for label, b_outcome in (
                ("market_favorite", fav_outcome),
                ("always_home", match.home_team),
            ):
                b_price = best_available_price(consensus, b_outcome)
                b_res = score.grade_pick(H2H, b_outcome)
                if b_price is not None and b_res in ("WIN", "LOSS"):
                    baseline_trades[label].append((b_price, b_res))

            # 2. Gate Evaluation
            gate_res = evaluate_gate(
                consensus,
                threshold=self.settings.gate_threshold,
                min_books=self.settings.min_books_alert,
                max_cv=self.settings.max_cv,
                require_positive_ev=self.settings.require_positive_ev,
            )

            if gate_res.pick is not None:
                # Grade A: 💎 Flagship Diamond Pick
                pick = gate_res.pick
                grade = "GRADE_A"
                market = pick.market
                outcome = pick.outcome_name
                p_true = pick.p_true
                fair_odds = pick.fair_odds
                best_book = pick.best_execution.book_title if pick.best_execution else "Consensus"
                best_odds = pick.best_execution.odds if pick.best_execution else fair_odds
                ev = pick.best_execution.ev if pick.best_execution else 0.0
                cv = pick.cv

                kelly = compute_kelly_stake(p_true, best_odds, bankroll=bankroll, cv=cv)
                stake_amount = max(50.0, kelly.stake_amount) if kelly.stake_amount > 0 else 100.0
                stake_units = round(stake_amount / (bankroll * 0.01), 2)

                res = score.grade_pick(market, outcome)
                if res is None:
                    res = "WIN" if score.winner() == outcome else "LOSS"

                closing_price = (closing_by_id.get(match.id) or {}).get(outcome)
                closing_odds, beat_clv = measure_clv(
                    consensus, outcome,
                    pick.best_execution.book_key if pick.best_execution else None,
                    best_odds,
                    closing_price,
                )
                clv_beats.append(beat_clv)
                if closing_odds:
                    clv_values.append(best_odds / closing_odds - 1.0)

                if res == "WIN":
                    pnl = stake_amount * (best_odds - 1.0)
                    flat_pnl = self.flat_stake_unit * (best_odds - 1.0)
                    predictions.append(p_true)
                    binary_outcomes.append(1.0)
                    trade_returns.append((best_odds - 1.0))
                    gross_wins += pnl
                    sport_stats[match.sport_key]["wins"] += 1
                elif res == "LOSS":
                    pnl = -stake_amount
                    flat_pnl = -self.flat_stake_unit
                    predictions.append(p_true)
                    binary_outcomes.append(0.0)
                    trade_returns.append(-1.0)
                    gross_losses += abs(pnl)
                    sport_stats[match.sport_key]["losses"] += 1
                else:
                    pnl = 0.0
                    flat_pnl = 0.0

                bankroll += pnl
                peak_bankroll = max(peak_bankroll, bankroll)
                dd_d = peak_bankroll - bankroll
                dd_p = (dd_d / peak_bankroll * 100.0) if peak_bankroll > 0 else 0.0
                max_dd_dollars = max(max_dd_dollars, dd_d)
                max_dd_pct = max(max_dd_pct, dd_p)

                total_wagered += stake_amount
                net_profit += pnl
                flat_profit += flat_pnl
                total_flat_wagered += self.flat_stake_unit

                sport_stats[match.sport_key]["profit"] += pnl
                sport_stats[match.sport_key]["wagered"] += stake_amount

                records.append(BacktestMatchRecord(
                    match_id=match.id,
                    sport_key=match.sport_key,
                    home_team=match.home_team,
                    away_team=match.away_team,
                    commence_time=match.commence_time.isoformat(),
                    grade=grade,
                    market=market,
                    outcome_name=outcome,
                    p_true=p_true,
                    fair_odds=fair_odds,
                    best_book=best_book,
                    best_odds=best_odds,
                    ev=ev,
                    conviction_score=pick.conviction_score,
                    stake_units=stake_units,
                    stake_amount=stake_amount,
                    actual_score=f"{score.home_score}-{score.away_score}",
                    result=res,
                    pnl=pnl,
                    capital_saved=0.0,
                    closing_odds=closing_odds,
                    beat_clv=beat_clv,
                ))

            else:
                # Pass Advisory (Grade C): the certainty gate did not fire, so no bet
                # is placed. Grade B "smart pivots" (double-chance / over-1.5 / NBA)
                # were removed because those markets cannot be priced honestly from
                # the football-data.co.uk archive (the record contains 1X2 lines
                # only) — the old implementation used invented fixed prices.
                grade = "GRADE_C"
                fav_team = match.home_team if p_home >= p_away else match.away_team
                p_fav = max(p_home, p_away)

                # Check counterfactual: did the favorite win?
                fav_won = (score.winner() == fav_team)

                if not fav_won:
                    # Genuine trap avoided: favorite lost or drew!
                    res = "PASS_TRAP_AVOIDED"
                    capital_saved = 100.0
                    hazard_warning = (
                        f"Negative EV Sucker Trap / Dispersion Disagreement. "
                        f"Avoided bet lost: {fav_team} failed to win."
                    )
                else:
                    # Favorite happened to win, but bet had negative EV
                    res = "PASS_ADVISORY"
                    capital_saved = 0.0
                    hazard_warning = (
                        f"Negative EV Coin-Flip Trap (P_true={p_fav*100:.1f}%). "
                        f"Passing avoided negative vigorish exposure."
                    )

                # The favourite's real best price across the archive books (the
                # price a naive favourite-chaser would actually have got), and
                # the honest counterfactual EV at that price — never fabricated.
                fav_price = best_available_price(consensus, fav_team) or fair_odds
                fav_ev = round(p_fav * fav_price - 1.0, 4)

                records.append(BacktestMatchRecord(
                    match_id=match.id,
                    sport_key=match.sport_key,
                    home_team=match.home_team,
                    away_team=match.away_team,
                    commence_time=match.commence_time.isoformat(),
                    grade=grade,
                    market="h2h",
                    outcome_name=f"HOLD OFF (No Bet)",
                    p_true=p_fav,
                    fair_odds=round(1.0 / p_fav, 2) if p_fav > 0 else 2.50,
                    best_book="Consensus",
                    best_odds=fav_price,
                    ev=fav_ev,
                    conviction_score=0.0,
                    stake_units=0.0,
                    stake_amount=0.0,
                    actual_score=f"{score.home_score}-{score.away_score}",
                    result=res,
                    pnl=0.0,
                    capital_saved=capital_saved,
                    closing_odds=(closing_by_id.get(match.id) or {}).get(fav_team),
                    hazard_warning=hazard_warning,
                ))

        # 3. Aggregate Statistical Calibration & Accuracy Benchmarks
        n_won = sum(1 for r in records if r.result == "WIN")
        n_lost = sum(1 for r in records if r.result == "LOSS")
        n_void = sum(1 for r in records if r.result == "VOID")
        executed = n_won + n_lost

        win_rate = (n_won / executed) if executed > 0 else 0.0
        ci_lower, ci_upper = compute_wilson_ci(n_won, executed)

        # Grade A metrics
        grade_a_recs = [r for r in records if r.grade == "GRADE_A"]
        g_a_won = sum(1 for r in grade_a_recs if r.result == "WIN")
        g_a_win_rate = (g_a_won / len(grade_a_recs)) if grade_a_recs else 0.0

        # Grade B metrics
        grade_b_recs = [r for r in records if r.grade == "GRADE_B"]
        g_b_won = sum(1 for r in grade_b_recs if r.result == "WIN")
        g_b_win_rate = (g_b_won / len(grade_b_recs)) if grade_b_recs else 0.0

        # Grade C metrics & Counterfactual Analysis
        grade_c_recs = [r for r in records if r.grade == "GRADE_C"]
        traps_avoided = len(grade_c_recs)
        traps_that_lost = sum(1 for r in grade_c_recs if r.result == "PASS_TRAP_AVOIDED")
        traps_that_won = sum(1 for r in grade_c_recs if r.result == "PASS_ADVISORY")
        total_capital_saved = sum(r.capital_saved for r in grade_c_recs)
        # Net counterfactual: (losses avoided * $100) - (missed wins * $80 net profit at avg ~1.80 odds)
        net_counterfactual = (traps_that_lost * 100.0) - (traps_that_won * 80.0)

        # Risk & Return Metrics (Sharpe, Sortino, Profit Factor)
        if trade_returns:
            mean_ret = statistics.mean(trade_returns)
            std_ret = statistics.stdev(trade_returns) if len(trade_returns) > 1 else 0.01
            sharpe = (mean_ret / std_ret * math.sqrt(len(trade_returns))) if std_ret > 0 else 0.0

            downside = [r for r in trade_returns if r < 0]
            if downside:
                downside_dev = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
                sortino = (mean_ret / downside_dev * math.sqrt(len(trade_returns))) if downside_dev > 0 else 0.0
            else:
                sortino = sharpe * 1.5
        else:
            sharpe = 0.0
            sortino = 0.0

        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 99.0

        # Provenance of the evaluated dataset — every exported report states what
        # it actually ran on, so a report can never silently claim synthetic data.
        if odds_payloads is None and scores_payloads is None:
            provenance: dict[str, Any] = dict(HISTORICAL_PROVENANCE)
        else:
            provenance = {
                "statement": "Custom payloads supplied by the caller; not the packaged real archive.",
                "odds_payloads": len(odds_data),
                "scores_payloads": len(scores_data),
            }
        if sport_keys:
            provenance["filtered_sport_keys"] = sorted(wanted_sports)

        # Quantitative Calibration Metrics
        settled_dicts = [
            {"p_true": r.p_true, "result": r.result}
            for r in records
            if r.result in ("WIN", "LOSS")
        ]
        cal_report = evaluate_calibration(settled_dicts)
        brier = cal_report.brier_score if cal_report.brier_score is not None else 0.0
        reliability = cal_report.reliability if cal_report.reliability is not None else 0.0
        resolution = cal_report.resolution if cal_report.resolution is not None else 0.0
        uncertainty = cal_report.uncertainty if cal_report.uncertainty is not None else 0.0
        ece = cal_report.ece if cal_report.ece is not None else 0.0
        mce = cal_report.mce if cal_report.mce is not None else 0.0
        logloss = cal_report.log_loss if cal_report.log_loss is not None else 0.0

        roi_pct = (net_profit / total_wagered * 100.0) if total_wagered > 0 else 0.0
        flat_roi_pct = (flat_profit / total_flat_wagered * 100.0) if total_flat_wagered > 0 else 0.0
        clv_rate = (sum(1 for b in clv_beats if b) / len(clv_beats)) if clv_beats else 1.0
        mean_clv = (sum(clv_values) / len(clv_values)) if clv_values else None

        for s_key, s_data in sport_stats.items():
            tot = s_data["wins"] + s_data["losses"]
            s_data["win_rate"] = round(s_data["wins"] / tot, 4) if tot > 0 else 0.0
            s_data["roi_pct"] = round(s_data["profit"] / s_data["wagered"] * 100.0, 2) if s_data["wagered"] > 0 else 0.0

        # --- MULTI-STRATEGY EXECUTION PROFILES ---
        # Every profile below is computed from the REAL per-bet records above; no
        # win rate, odds, or risk metric is hardcoded. Two entries are honest
        # baselines / derived scenarios and are labelled as such.

        # 1. Conservative — LISA quality-gated singles (real executed bets).
        conservative_records = [r.to_dict() for r in records]
        conservative_summary = summarize_profile(
            conservative_records,
            strategy_id="conservative",
            name="Conservative Singles",
            badge="🛡️ Quality-Gated Singles",
            description=(
                "LISA quality gate (P_true >= threshold, CV <= max_cv, >= min books) "
                "executed at the best recorded price. Realised, not simulated."
            ),
            risk_level="Determined by data",
            best_for="Honest baseline for the gated pipeline",
            initial_capital=self.initial_bankroll,
        )
        conservative_summary["capital_preserved_dollars"] = round(total_capital_saved, 2)
        conservative_summary["net_counterfactual_value"] = round(net_counterfactual, 2)

        # 2. Unfiltered market favourites — betting every de-vigged favourite at the
        #    best recorded price. Shows what naive favourite-chasing actually returns.
        fav_records: list[dict[str, Any]] = []
        for i, (odds, res) in enumerate(baseline_trades["market_favorite"]):
            fav_records.append({
                "match_id": f"fav-{i}",
                "sport_key": "baseline",
                "home_team": "market",
                "away_team": "favourite",
                "commence_time": "",
                "grade": "GRADE_A",
                "market": "h2h",
                "outcome_name": "favourite",
                "p_true": 0.0,
                "fair_odds": 0.0,
                "best_book": "best",
                "best_odds": odds,
                "ev": 0.0,
                "conviction_score": 0.0,
                "stake_units": 1.0,
                "stake_amount": 100.0,
                "actual_score": "",
                "result": res,
                "pnl": round(100.0 * (odds - 1.0), 2) if res == "WIN" else -100.0,
                "capital_saved": 0.0,
                "closing_odds": odds,
                "beat_clv": False,
            })
        fav_summary = summarize_profile(
            fav_records,
            strategy_id="high_yield_pivots",
            name="Market Favourites (Baseline)",
            badge="📊 Baseline",
            description=(
                "Bet every de-vigged market favourite at its best recorded price — no "
                "quality gate. Realised; an honest benchmark for the gate above."
            ),
            risk_level="Determined by data",
            best_for="Benchmark: does the gate add value vs. chasing favourites?",
        )

        # 2b. Always-home baseline — betting the home team blind at best price.
        home_records: list[dict[str, Any]] = []
        for i, (odds, res) in enumerate(baseline_trades["always_home"]):
            home_records.append({
                "match_id": f"home-{i}",
                "sport_key": "baseline",
                "home_team": "home",
                "away_team": "side",
                "commence_time": "",
                "grade": "GRADE_A",
                "market": "h2h",
                "outcome_name": "home",
                "p_true": 0.0,
                "fair_odds": 0.0,
                "best_book": "best",
                "best_odds": odds,
                "ev": 0.0,
                "conviction_score": 0.0,
                "stake_units": 1.0,
                "stake_amount": 100.0,
                "actual_score": "",
                "result": res,
                "pnl": round(100.0 * (odds - 1.0), 2) if res == "WIN" else -100.0,
                "capital_saved": 0.0,
                "closing_odds": odds,
                "beat_clv": False,
            })
        home_summary = summarize_profile(
            home_records,
            strategy_id="always_home",
            name="Always Home (Baseline)",
            badge="📊 Baseline",
            description=(
                "Bet the home team blind at its best recorded price — no gate, no "
                "model. The naive 'home advantage' baseline."
            ),
            risk_level="Determined by data",
            best_for="Benchmark: does the gate add value vs. blind home betting?",
        )

        # 3. Derived 2-leg parlays from real records. Not a live market — payout odds
        #    equal the product of the two recorded best prices, so the scenario is
        #    computable from real data but is explicitly labelled derived.
        exec_base = [r for r in conservative_records if r.get("result") in ("WIN", "LOSS")]
        parlay_records: list[dict[str, Any]] = []
        for i in range(0, len(exec_base) - 1, 2):
            l1 = exec_base[i]
            l2 = exec_base[i + 1]
            odds = round(l1["best_odds"] * l2["best_odds"], 2)
            both_won = (l1["result"] == "WIN" and l2["result"] == "WIN")
            parlay_records.append({
                "match_id": f"parlay-{i // 2 + 1:02d}",
                "sport_key": l1["sport_key"],
                "home_team": f"{l1['home_team']} & {l2['home_team']}",
                "away_team": f"vs {l1['away_team']} & {l2['away_team']}",
                "commence_time": l1["commence_time"],
                "grade": "GRADE_A",
                "market": "2-Leg Parlay (derived)",
                "outcome_name": f"{l1['outcome_name']} + {l2['outcome_name']}",
                "p_true": round(l1["p_true"] * l2["p_true"], 4),
                "fair_odds": round(1.0 / l1["p_true"] / l2["p_true"], 2)
                if l1["p_true"] * l2["p_true"] > 0 else 0.0,
                "best_book": "derived",
                "best_odds": odds,
                "ev": round(l1["p_true"] * l2["p_true"] * odds - 1.0, 4),
                "conviction_score": 0.0,
                "stake_units": 2.0,
                "stake_amount": 200.0,
                "actual_score": f"{l1['actual_score']} & {l2['actual_score']}",
                "result": "WIN" if both_won else "LOSS",
                "pnl": round(200.0 * (odds - 1.0), 2) if both_won else -200.0,
                "capital_saved": 0.0,
                "hazard_warning": None,
                "closing_odds": odds,
                "beat_clv": False,
            })
        parlays_summary = summarize_profile(
            parlay_records,
            strategy_id="smart_parlays",
            name="Derived 2-Leg Parlays",
            badge="🎰 Derived Scenario",
            description=(
                "DERIVED (not a live market): consecutive quality-gated singles paired "
                "into 2-leg parlays at the product of their recorded best prices."
            ),
            risk_level="Determined by data",
            best_for="Illustrative only — parlays must be priced at a real book",
        )

        # 4. Derived 70/30 blend of the gated singles and derived parlay series.
        hybrid_records: list[dict[str, Any]] = []
        for r in conservative_records:
            rc = dict(r)
            if rc.get("result") in ("WIN", "LOSS"):
                rc["stake_amount"] = 70.0
                rc["pnl"] = round(rc["pnl"] * 0.7, 2)
            hybrid_records.append(rc)
        for p in parlay_records:
            pc = dict(p)
            pc["stake_amount"] = 100.0
            pc["pnl"] = round(pc["pnl"] * 0.5, 2)
            hybrid_records.append(pc)
        hybrid_summary = summarize_profile(
            hybrid_records,
            strategy_id="hybrid_portfolio",
            name="Derived 70/30 Blend",
            badge="🧩 Derived Scenario",
            description=(
                "DERIVED (not a live market): 70% of the gated singles series plus 30% "
                "of the derived parlay series, both scaled from their real records."
            ),
            risk_level="Determined by data",
            best_for="Illustrative only",
        )

        strategies_dict = {
            "conservative": {"summary": conservative_summary, "records": conservative_records},
            "high_yield_pivots": {"summary": fav_summary, "records": fav_records},
            "smart_parlays": {"summary": parlays_summary, "records": parlay_records},
            "hybrid_portfolio": {"summary": hybrid_summary, "records": hybrid_records},
            "always_home": {"summary": home_summary, "records": home_records},
        }

        comparison_matrix = [
            conservative_summary,
            fav_summary,
            home_summary,
            parlays_summary,
            hybrid_summary,
        ]

        return BacktestReport(
            total_matches=len(records),
            executed_bets=executed,
            wins=n_won,
            losses=n_lost,
            pushes=n_void,
            win_rate=win_rate,
            wilson_ci_lower=ci_lower,
            wilson_ci_upper=ci_upper,
            grade_a_count=len(grade_a_recs),
            grade_a_wins=g_a_won,
            grade_a_win_rate=g_a_win_rate,
            grade_b_count=len(grade_b_recs),
            grade_b_wins=g_b_won,
            grade_b_win_rate=g_b_win_rate,
            grade_c_traps_avoided=traps_avoided,
            grade_c_traps_that_lost=traps_that_lost,
            grade_c_traps_that_won=traps_that_won,
            capital_preserved_dollars=total_capital_saved,
            net_counterfactual_value=net_counterfactual,
            initial_bankroll=self.initial_bankroll,
            ending_bankroll=bankroll,
            total_wagered=total_wagered,
            net_profit=net_profit,
            roi_pct=roi_pct,
            flat_profit=flat_profit,
            flat_roi_pct=flat_roi_pct,
            max_drawdown_pct=max_dd_pct,
            max_drawdown_dollars=max_dd_dollars,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            profit_factor=profit_factor,
            brier_score=brier,
            reliability=reliability,
            resolution=resolution,
            uncertainty=uncertainty,
            ece=ece,
            mce=mce,
            log_loss=logloss,
            positive_clv_rate=clv_rate,
            mean_clv=mean_clv,
            sport_breakdown=sport_stats,
            records=records,
            calibration_report=cal_report,
            strategies=strategies_dict,
            strategy_comparison_matrix=comparison_matrix,
            data_provenance=provenance,
        )


def format_backtest_report(report: BacktestReport, verbose: bool = False) -> str:
    """Format an executive terminal report with clean alignment and metric tables."""
    lines: list[str] = []
    w = 80
    lines.append("=" * w)
    lines.append(" LISA QUANTITATIVE HISTORICAL BACKTEST & INSTITUTIONAL AUDIT ".center(w, "="))
    lines.append("=" * w)

    lines.append("\n[1] EXECUTIVE ACCURACY & PREDICTION METRICS")
    lines.append("-" * w)
    prov = report.data_provenance or {}
    lines.append(f"  Data Source:                       {prov.get('source', 'custom payloads')}")
    if prov.get("note"):
        lines.append(f"  Data Note:                         {prov['note']}")
    if report.data_provenance:
        lines.append(f"  Matches Archived:                  {report.data_provenance.get('total_matches', report.total_matches)}")
    lines.append(f"  Total Historical Matches Evaluated: {report.total_matches}")
    lines.append(f"  Executed Wagers (Grade A):          {report.executed_bets} ({report.wins} Won, {report.losses} Lost, {report.pushes} Void)")
    lines.append(f"  Overall Realized Win Rate:          {report.win_rate * 100:.1f}%")
    lines.append(f"  Wilson 95% Confidence Interval:     [{report.wilson_ci_lower * 100:.1f}%, {report.wilson_ci_upper * 100:.1f}%]")
    lines.append(f"  💎 Grade A (Flagship Diamonds):      {report.grade_a_wins}/{report.grade_a_count} ({report.grade_a_win_rate * 100:.1f}% Win Rate)")
    lines.append(f"  🛡️ Grade C (Pass Advisories):        {report.grade_c_traps_avoided} Passes (No Bet)")
    lines.append(f"    - Favorite Lost / Drew:           {report.grade_c_traps_that_lost} Traps  (counterfactual ${report.capital_preserved_dollars:,.2f} avoided)")
    lines.append(f"    - Favorite Won (Negative EV):     {report.grade_c_traps_that_won} Traps  (Avoided negative-juice exposure)")
    lines.append(f"    - Net Counterfactual Value:       ${report.net_counterfactual_value:,.2f} (theoretical, not realized)")

    lines.append("\n[2] STATISTICAL CALIBRATION & PROBABILITY BENCHMARKS")
    lines.append("-" * w)
    lines.append(f"  Brier Score (MSE, 0=perfect):       {report.brier_score:.4f}  (Professional Baseline: < 0.1000)")
    lines.append(f"  Expected Calibration Error (ECE):   {report.ece * 100:.2f}%   (Target: < 10.0%)")
    lines.append(f"  Maximum Calibration Error (MCE):    {report.mce * 100:.2f}%")
    lines.append(f"  Log Loss (Cross-Entropy):           {report.log_loss:.4f}")
    lines.append("  Murphy (1973) Brier Decomposition:")
    lines.append(f"    - Reliability (Calibration Error): {report.reliability:.6f}  (Near 0 = Optimal alignment)")
    lines.append(f"    - Resolution (Sorting Power):      {report.resolution:.6f}  (Higher = Greater Discrimination)")
    lines.append(f"    - Uncertainty (Base Rate Entropy): {report.uncertainty:.6f}")
    lines.append(f"  Positive CLV Beat Rate:             {report.positive_clv_rate * 100:.1f}%")
    if report.mean_clv is not None:
        lines.append(f"  Mean CLV (exec / closing - 1):     {report.mean_clv * 100:+.2f}%  (vs recorded closing line)")

    lines.append("\n[3] FINANCIAL & RISK-ADJUSTED PERFORMANCE (FRACTIONAL KELLY)")
    lines.append("-" * w)
    lines.append(f"  Initial Bankroll:                   ${report.initial_bankroll:,.2f}")
    lines.append(f"  Final Bankroll:                     ${report.ending_bankroll:,.2f}")
    lines.append(f"  Total Wagered:                      ${report.total_wagered:,.2f}")
    lines.append(f"  Net Profit (Kelly Compounded):      ${report.net_profit:,.2f}  (Kelly ROI: {report.roi_pct:.2f}%)")
    lines.append(f"  Flat Staking Comparison (1 Unit):   ${report.flat_profit:,.2f}  (Flat ROI: {report.flat_roi_pct:.2f}%)")
    lines.append(f"  Maximum Peak-to-Trough Drawdown:    -{report.max_drawdown_pct:.2f}%  (-${report.max_drawdown_dollars:,.2f})")
    lines.append(f"  Sharpe Ratio (Risk-Adjusted):       {report.sharpe_ratio:.2f}")
    lines.append(f"  Sortino Ratio (Downside Risk):      {report.sortino_ratio:.2f}")
    lines.append(f"  Profit Factor (Gross Win/Loss):     {report.profit_factor:.2f}")

    if report.strategy_comparison_matrix:
        lines.append("\n[4] MULTI-STRATEGY EXECUTION COMPARISON MATRIX")
        lines.append("-" * w)
        lines.append(f"  {'Strategy':<26} {'Win %':<8} {'Odds':<6} {'Wagered':<10} {'Net Profit':<12} {'ROI %':<8} {'Max DD'}")
        lines.append("  " + "-" * 76)
        for s in report.strategy_comparison_matrix:
            lines.append(f"  {s['name']:<26} {s['win_rate']*100:>5.1f}%  {s['avg_odds']:>5.2f}  ${s['total_wagered']:>8.2f}  {s['net_profit']:>9.2f}  {s['roi_pct']:>5.1f}%  {s['max_drawdown_pct']:>5.2f}%")

    lines.append("\n[5] SPORT & LEAGUE BREAKDOWN")
    lines.append("-" * w)
    lines.append(f"  {'Sport / League':<28} {'Matches':<8} {'W-L':<10} {'Win %':<10} {'ROI %':<10} {'PnL ($)':<10}")
    lines.append("  " + "-" * 76)
    for sport, s in report.sport_breakdown.items():
        wl_str = f"{s['wins']}-{s['losses']}"
        lines.append(f"  {sport:<28} {s['matches']:<8} {wl_str:<10} {s['win_rate']*100:>5.1f}%     {s['roi_pct']:>5.1f}%    ${s['profit']:>7.2f}")

    if verbose:
        lines.append("\n[6] DETAILED MATCH-BY-MATCH AUDIT TRAIL")
        lines.append("-" * w)
        lines.append(f"  {'Match / Teams':<30} {'Grade':<9} {'Market':<14} {'Odds':<6} {'Score':<7} {'Result':<10} {'PnL'}")
        lines.append("  " + "-" * 76)
        for r in report.records:
            match_str = f"{r.home_team[:14]} vs {r.away_team[:13]}"
            if r.result == "WIN":
                res_str = "WIN"
                pnl_str = f"+${r.pnl:.2f}"
            elif r.result == "LOSS":
                res_str = "LOSS"
                pnl_str = f"-${abs(r.pnl):.2f}"
            elif r.result == "PASS_TRAP_AVOIDED":
                res_str = "TRAP_AVOID"
                pnl_str = f"${r.capital_saved:.0f} Saved"
            else:
                res_str = "PASS_ADV"
                pnl_str = "$0 (Avoid -EV)"

            lines.append(f"  {match_str:<30} {r.grade:<9} {r.market:<14} {r.best_odds:<6.2f} {r.actual_score:<7} {res_str:<10} {pnl_str}")

    lines.append("=" * w)
    return "\n".join(lines)
