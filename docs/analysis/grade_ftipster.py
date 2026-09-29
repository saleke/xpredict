"""Grade Ftipster's published Bet-Of-The-Day archive against their own claims.

Handles Asian lines (quarter/half balls, pushes) so the P&L is honest.
"""
import re, sys
from collections import defaultdict

rows = []
for line in open("docs/analysis/botd_raw.md", encoding="utf-8"):
    if not line.strip().startswith("|"):
        continue
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) != 5:
        continue
    date, home, result, away, bet = (c.replace("\\", "") for c in cells)
    # skip header/separator
    if date in ("DATE",) or set(date) <= set("-: "):
        continue
    if not re.match(r"^\d{2}\.\d{2}\.\d{2}$", date):
        continue
    m = re.match(r"^(\d+)\s*:\s*(\d+)", result.replace("\\", ""))
    if not m:
        rows.append((date, home, away, bet, None, result))
        continue
    rows.append((date, home, away, bet, (int(m.group(1)), int(m.group(2))), result))

def parse_bet(bet):
    """Return (kind, line, side) or None if we cannot grade it."""
    b = bet.replace("\\", "").strip()
    # split "MARKET @ odds"
    parts = b.split("@")
    mkt = parts[0].strip()
    m = re.search(r"@\s*([\d.]+)\s*$", b)
    odds = float(m.group(1)) if m else None
    return mkt, odds

def grade(mkt, hg, ag):
    """Return (stake_at_risk_fraction, won_fraction) or None if ungradable."""
    tot = hg + ag
    diff = hg - ag
    mk = mkt.lower()

    # --- Over/Under with Asian split (e.g. "Over 2.5,3.0") ---
    m = re.match(r"^over\s+([\d.]+)(?:\s*,\s*([\d.]+))?$", mk)
    if m:
        lines = [float(m.group(1))] + ([float(m.group(2))] if m.group(2) else [])
        return _grade_total(lines, tot, over=True)
    m = re.match(r"^under\s+([\d.]+)(?:\s*,\s*([\d.]+))?$", mk)
    if m:
        lines = [float(m.group(1))] + ([float(m.group(2))] if m.group(2) else [])
        return _grade_total(lines, tot, over=False)

    if mk in ("btts yes", "btts"):
        return (1.0, 1.0) if (hg > 0 and ag > 0) else (1.0, 0.0)
    if mk == "btts no":
        return (1.0, 0.0) if (hg > 0 and ag > 0) else (1.0, 1.0)

    # --- 1X2 singles ---
    if mk == "1":
        return (1.0, 1.0) if diff > 0 else (1.0, 0.0)
    if mk == "2":
        return (1.0, 1.0) if diff < 0 else (1.0, 0.0)
    if mk == "x":
        return (1.0, 1.0) if diff == 0 else (1.0, 0.0)
    if mk in ("1x", "12", "x2", "1 or x", "x or 2", "1 or 2"):
        if mk == "1x":  ok = diff >= 0
        elif mk == "12": ok = diff != 0
        elif mk == "x2": ok = diff <= 0
        elif mk == "1 or x": ok = diff >= 0
        elif mk == "x or 2": ok = diff <= 0
        else: ok = True
        return (1.0, 1.0) if ok else (1.0, 0.0)

    # --- Asian Handicap: "-0.5 Home", "0.0 Home", "+1.5 Away", "0.0, +0.5 Home" ---
    m = re.match(r"^([+\-]?[\d.]+)(?:\s*,\s*([+\-]?[\d.]+))?\s+(home|away)$", mk)
    if m:
        l1 = float(m.group(1))
        lines = [l1] + ([float(m.group(2))] if m.group(2) else [])
        side = m.group(3)
        return _grade_ah(lines, diff, side)
    m = re.match(r"^([+\-]?[\d.]+)\s+corners$", mk)
    if m:
        return None  # no corner data in the archive

    # qualify / cup-winner style
    if "qualify" in mk or "cup winner" in mk:
        return None
    return None


def _sub(line, diff, side, tot, is_home):
    """Grade one sub-line. Returns (stake, won) each in {0,0.5,1}."""
    if is_home:
        adj = diff - line          # home + line - away = diff ; home wins when >0
    else:
        # away receives `line`; convert to a home-perspective line
        adj = -diff + line
        adj = adj
    # AH convention: bet on the side; settle on adjusted margin
    m = round(adj, 6)
    if m > 0:
        return 0.5, 0.5   # win half stake
    if m < 0:
        return 0.5, 0.0
    return 0.0, 0.0       # push


def _grade_ah(lines, diff, side):
    is_home = side == "home"
    stake = won = 0.0
    for ln in lines:
        if is_home:
            adj = diff - ln
        else:
            adj = (-diff) - ln
        adj = round(adj, 6)
        if abs(adj) < 1e-9:
            s, w = 0.0, 0.0
        elif adj > 0:
            s, w = 0.5, 0.5
        else:
            s, w = 0.5, 0.0
        stake += s
        won += w
    return stake, won


def _grade_total(lines, tot, over=True):
    stake = won = 0.0
    for ln in lines:
        g = round(tot - ln, 6) if over else round(ln - tot, 6)
        if abs(g) < 1e-9:
            s, w = 0.0, 0.0
        elif g > 0:
            s, w = 0.5, 0.5
        else:
            s, w = 0.5, 0.0
        stake += s
        won += w
    return stake, won


stats = defaultdict(lambda: dict(n=0, wins=0, half=0, loss=0, push=0, pnl=0.0, odds=[]))
months = defaultdict(lambda: dict(n=0, wins=0, loss=0, push=0, pnl=0.0, odds=[]))
graded, skipped = 0, 0
for date, home, away, bet, score, rawres in rows:
    mkt, odds = parse_bet(bet)
    if score is None or odds is None:
        skipped += 1
        continue
    g = grade(mkt, score[0], score[1])
    if g is None:
        skipped += 1
        continue
    stake, won = g
    graded += 1
    pnl = won * odds - stake
    s = stats["ALL"]
    s["n"] += 1
    s["pnl"] += pnl
    s["odds"].append(odds)
    if pnl > 0.001: s["wins"] += 1
    elif pnl < -0.001: s["loss"] += 1
    else: s["push"] += 1
    fam = mkt.split(",")[0].strip().rsplit(" ", 1)[0] if ("Home" in mkt or "Away" in mkt) else mkt.split(",")[0].strip()
    d, mo, y = date.split(".")
    ym = f"20{y}-{mo}"
    keys = [fam, ym]
    if fam.startswith(("Over", "Under")):
        keys.append("OU: " + fam)
    for key in keys:
        f = stats[key]
        f["n"] += 1; f["pnl"] += pnl; f["odds"].append(odds)
        if pnl > 0.001: f["wins"] += 1
        elif pnl < -0.001: f["loss"] += 1
        else: f["push"] += 1

print(f"Total rows parsed: {len(rows)}   graded: {graded}   ungradable: {skipped}\n")

def report(key):
    s = stats[key]
    n = s["n"]
    if not n: return
    dec = n - s["push"]
    avg = sum(s["odds"]) / len(s["odds"])
    wr = s["wins"] / n * 100
    wrd = s["wins"] / dec * 100 if dec else 0
    roi = s["pnl"] / n * 100
    print(f"{key:<26} n={n:<5} avg_odds={avg:.3f}  win%={wr:5.1f}  win%_excl_push={wrd:5.1f}  "
          f"push={s['push']:<3} units={s['pnl']:+7.2f}  ROI={roi:+6.2f}%")

report("ALL")
print()
for k in sorted(stats):
    if k != "ALL" and not re.match(r"^\d{4}-\d{2}$", k):
        report(k)
print("\n--- monthly ---")
for k in sorted(stats):
    if re.match(r"^\d{4}-\d{2}$", k):
        report(k)
