"""Flat-stake comparison of the two gate policies on the real packaged archive.

``BacktestMatchRecord.pnl`` is a *bankroll delta* under the strategy's own
sizing, so it cannot be averaged across bets to get a per-bet edge. This
recomputes a flat 1-unit P&L per bet directly from the stored price and result,
which is the number that is comparable between the two policies.
"""
import math
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from lisa import config as cfg
from lisa.backtest import BacktestEngine
from lisa.history import LEAGUE_BOOKS

SPORTS = sorted({sk for sk, _ in LEAGUE_BOOKS.values()})


def wilson(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def flat_pnls(rep):
    """+odds-1 on a win, -1 on a loss, 0 on a push/void. 1 unit per bet."""
    out = []
    for r in rep.records:
        res = getattr(r, "result", None)
        if res not in ("WIN", "LOSS", "PUSH", "VOID"):
            continue
        odds = getattr(r, "best_odds", None)
        if not odds:
            continue
        out.append((r, (odds - 1.0) if res == "WIN" else (-1.0 if res == "LOSS" else 0.0)))
    return out


def ttest(v):
    n = len(v)
    if n < 2:
        return 0.0, 0.0, 0.0
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    se = sd / math.sqrt(n)
    return m, sd, (m / se if se else 0.0)


def curve(v, frac=0.02):
    b, peak, dd = 1.0, 1.0, 0.0
    for x in v:
        b *= 1 + frac * x
        peak = max(peak, b)
        dd = min(dd, b / peak - 1)
    return b, dd


for flag in (False, True):
    rep = BacktestEngine(cfg.Settings(require_positive_ev=flag)).run(sport_keys=SPORTS)
    pairs = flat_pnls(rep)
    v = [x for _, x in pairs]
    n = len(v)
    wins = sum(1 for x in v if x > 0)
    lo, hi = wilson(wins, n)
    m, sd, t = ttest(v)
    avg_odds = sum(r.best_odds for r, _ in pairs) / n
    be = sum(1.0 / r.best_odds for r, _ in pairs) / n
    print("=" * 72)
    print(f"require_positive_ev = {flag}")
    print("=" * 72)
    print(f"  settled bets        {n}")
    print(f"  win rate            {wins/n*100:.2f}%   95% CI [{lo*100:.2f}, {hi*100:.2f}]")
    print(f"  avg price           {avg_odds:.3f}   breakeven win rate {be*100:.2f}%")
    print(f"  flat ROI            {sum(v)/n*100:+.2f}%   ({sum(v):+.2f} units)")
    print(f"  mean per bet        {m:+.4f} u  (sd {sd:.3f}, t = {t:.2f})")
    b2, dd2 = curve(v, 0.02)
    print(f"  2% flat bankroll    {b2:.3f}x   max drawdown {dd2*100:.1f}%")
    clv = [getattr(r, "clv", None) for r, _ in pairs]
    clv = [c for c in clv if c is not None]
    if clv:
        pos = sum(1 for c in clv if c > 0)
        print(f"  mean CLV            {sum(clv)/len(clv)*100:+.3f}%  positive {pos}/{len(clv)}")
    print()

print("=" * 72)
print("LEAGUE BREAKDOWN, edge-required gate (flat 1 unit)")
print("=" * 72)
rep = BacktestEngine(cfg.Settings(require_positive_ev=True)).run(sport_keys=SPORTS)
by = defaultdict(list)
for r, x in flat_pnls(rep):
    by[getattr(r, "sport_key", "?")].append((r, x))
for k in sorted(by):
    g = by[k]
    w = sum(1 for _, x in g if x > 0)
    u = sum(x for _, x in g)
    o = sum(r.best_odds for r, _ in g) / len(g)
    print(f"  {k:<32} n={len(g):<3} win%={w/len(g)*100:5.1f}  avg={o:.2f}  "
          f"units={u:+6.2f}  ROI={u/len(g)*100:+7.2f}%")
