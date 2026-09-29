"""Deeper forensic analysis of Ftipster's published BOTD archive."""
import re, math
from collections import Counter, defaultdict

rows = []
for line in open("docs/analysis/botd_raw.md", encoding="utf-8"):
    if not line.strip().startswith("|"):
        continue
    cells = [c.replace("\\", "").strip() for c in line.strip().strip("|").split("|")]
    if len(cells) != 5 or cells[0] == "DATE" or set(cells[0]) <= set("-: "):
        continue
    date, home, result, away, bet = cells
    if not re.match(r"^\d{2}\.\d{2}\.\d{2}$", date):
        continue
    m = re.match(r"^(\d+)\s*:\s*(\d+)", result)
    if not m:
        continue
    mo = re.search(r"@\s*([\d.]+)\s*$", bet)
    if not mo:
        continue
    d, mm, y = date.split(".")
    rows.append(dict(date=f"20{y}-{mm}-{d}", ym=f"20{y}-{mm}", home=home, away=away,
                     hg=int(m.group(1)), ag=int(m.group(2)),
                     mkt=bet.split("@")[0].strip(), odds=float(mo.group(1))))

def stake_won(r):
    mk, tot, diff = r["mkt"].lower(), r["hg"] + r["ag"], r["hg"] - r["ag"]
    def one(margin):
        m = round(margin, 6)
        return (0.0, 0.0) if abs(m) < 1e-9 else ((0.5, 0.5) if m > 0 else (0.5, 0.0))
    lines = []
    m = re.match(r"^over\s+([\d.]+)(?:\s*,\s*([\d.]+))?$", mk)
    if m:
        lines = [float(m.group(1))] + ([float(m.group(2))] if m.group(2) else [])
        return [one(tot - ln) for ln in lines]
    m = re.match(r"^under\s+([\d.]+)(?:\s*,\s*([\d.]+))?$", mk)
    if m:
        lines = [float(m.group(1))] + ([float(m.group(2))] if m.group(2) else [])
        return [one(ln - tot) for ln in lines]
    if mk in ("btts yes", "btts"): return [one(1 if (r["hg"] and r["ag"]) else -1)]
    if mk == "btts no":          return [one(-1 if (r["hg"] and r["ag"]) else 1)]
    if mk == "1":                 return [one(diff)]
    if mk == "2":                 return [one(-diff)]
    if mk in ("1x", "1 or x"):    return [one(diff) if diff >= 0 else one(-1)]
    if mk in ("x2", "x or 2"):    return [one(-diff) if diff <= 0 else one(-1)]
    if mk == "12":                return [one(diff) if diff != 0 else one(-1)]
    m = re.match(r"^([+\-]?[\d.]+)(?:\s*,\s*([+\-]?[\d.]+))?\s+(home|away)$", mk)
    if m:
        ls = [float(m.group(1))] + ([float(m.group(2))] if m.group(2) else [])
        s = m.group(3)
        return [one((diff if s == "home" else -diff) - ln) for ln in ls]
    return None

pnls = []
for r in rows:
    sw = stake_won(r)
    if sw is None: continue
    r["pnl"] = sum(w for _, w in sw) * r["odds"] - sum(s for s, _ in sw)
    r["stake"] = sum(s for s, _ in sw)
    pnls.append(r["pnl"])

n = len(pnls)
mean = sum(pnls) / n
sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (n - 1))
se = sd / math.sqrt(n)
t = mean / se
print(f"n={n}  mean_units={mean:+.4f}  sd={sd:.3f}  se={se:.4f}  t={t:.2f}")
print(f"  95% CI on edge: [{mean-1.96*se:+.4f}, {mean+1.96*se:+.4f}] units/bet")
print(f"  breakeven win-rate needed at avg odds: "
      f"{sum(1/(r['odds']) for r in rows if 'pnl' in r)/n*100:.1f}%")
print()

# ---- odds ladder: the price-construction fingerprint ----
print("ODDS LADDER (published price distribution)")
c = Counter(r["odds"] for r in rows if "pnl" in r)
tot = sum(c.values())
for o, k in sorted(c.items(), key=lambda x: -x[1]):
    print(f"  {o:.2f}  {k:>4}  {k/tot*100:5.1f}%  {'#'*int(k/3)}")
print(f"\n  distinct prices used: {len(c)}   modal price: {c.most_common(1)[0]}")
print(f"  share priced in 1.53-1.62: {sum(k for o,k in c.items() if 1.53<=o<=1.62)/tot*100:.1f}%")
print()

# ---- market mix ----
print("MARKET MIX")
mk = Counter()
for r in rows:
    if "pnl" not in r: continue
    m = re.match(r"^([+\-]?[\d.]+)(?:\s*,\s*[+\-]?[\d.]+)?\s+(home|away)$", r["mkt"].lower())
    mk["Asian Handicap" if m else r["mkt"]] += 1
for k, v in mk.most_common():
    print(f"  {k:<24} {v:>4}  {v/tot*100:5.1f}%")
print()

# ---- selection bias: goal-total distribution of matches they pick ----
print("GOAL-TOTAL DISTRIBUTION of fixtures they bet")
tot_goals = Counter(r["hg"] + r["ag"] for r in rows if "pnl" in r)
n0 = sum(tot_goals.values())
for g in sorted(tot_goals):
    print(f"  {g} goals: {tot_goals[g]:>4}  {tot_goals[g]/n0*100:5.1f}%  {'#'*int(tot_goals[g]/2)}")
print()

# ---- streak / drawdown on a flat-1-unit bankroll ----
eq, peak, dd, eqs = 0.0, 0.0, 0.0, []
for p in pnls:
    eq += p; eqs.append(eq)
    peak = max(peak, eq); dd = min(dd, eq - peak)
losing = streak = max_streak = 0
for p in pnls:
    losing = losing + 1 if p < -0.001 else 0
    max_streak = max(max_streak, losing)
print(f"FLAT 1-UNIT: final={eq:+.2f}u  peak={max(eqs):+.2f}u  max drawdown={dd:.2f}u  "
      f"max losing streak={max_streak}")
print(f"  return per unit staked: {eq/sum(r['stake'] for r in rows if 'pnl' in r)*100:+.2f}%")
worst = min(enumerate(pnls), key=lambda x: x[1])
print(f"  worst single bet: {worst[1]:+.2f}u (index {worst[0]})")
print()

# ---- bankroll survival: how deep before ruin at 1% flat ----
def survival(kelly_frac):
    bank = 1.0
    for p in pnls:
        bank *= (1 + kelly_frac * p)
        if bank <= 0: return False, len(pnls)
    return True, bank
for f in (0.005, 0.01, 0.02, 0.05):
    ok, b = survival(f)
    print(f"  flat {f*100:>4.1f}% of bankroll: {'ALIVE' if ok else 'BUSTED'}  final bank={b:.3f}x")
