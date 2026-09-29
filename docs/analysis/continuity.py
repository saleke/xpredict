"""Continuity check + league-quality profile of Ftipster's picks."""
import re
from collections import Counter
from datetime import date, timedelta

rows = []
for line in open("docs/analysis/botd_raw.md", encoding="utf-8"):
    if not line.strip().startswith("|"): continue
    c = [x.replace("\\", "").strip() for x in line.strip().strip("|").split("|")]
    if len(c) != 5 or c[0] == "DATE" or set(c[0]) <= set("-: "): continue
    if not re.match(r"^\d{2}\.\d{2}\.\d{2}$", c[0]): continue
    d, m, y = c[0].split(".")
    rows.append((date(2000 + int(y), int(m), int(d)), c[1], c[3]))

rows.sort()
print(f"SPAN: {rows[0][0]} -> {rows[-1][0]}   rows={len(rows)}")
span = (rows[-1][0] - rows[0][0]).days + 1
print(f"calendar days in span={span}   rows/day={len(rows)/span:.3f}  (expect ~1.00 for a daily service)")
dups = [d for d, k in Counter(r[0] for r in rows).items() if k > 1]
print(f"duplicate dates: {len(dups)}")
present = {r[0] for r in rows}
gaps = []
cur = rows[0][0]
while cur <= rows[-1][0]:
    if cur not in present:
        gaps.append(cur)
    cur += timedelta(days=1)
print(f"MISSING DATES (no tip published): {len(gaps)}")
for g in gaps:
    print(f"   {g} ({g.strftime('%a')})")
print()

# ---- how deep / how obscure is the league mix? ----
BIG5 = """Real Madrid Barcelona Atletico Atletico Madrid Sevilla Liverpool Manchester United Manchester City
Chelsea Arsenal Tottenham Newcastle Aston Villa West Ham Brighton Aston Villa Fulham Crystal Palace Everton
Nottingham Forest Bournemouth Brentford Wolves Leicester Wolves Southampton Ipswich Sheffield United Brentford
Bayern Leverkusen Dortmund RB Leipzig Stuttgart Frankfurt Wolfsburg Freiburg Hoffenheim Gladbach Union Berlin
Mönchengladbach Monchengladbach Augsburg Werder Bremen
Inter Milan AC Milan Juventus Napoli Roma Lazio Atalanta Fiorentina Bologna Torino
PSG Monaco Marseille Lyon Lille Nice
Ajax PSV Feyenoord AZ
Benfica Porto Sporting Celtic Rangers Galatasaray Fenerbahce Besiktas Olympiacos
""".split("\n")
BIG5 = {t.strip().lower() for line in BIG5 for t in line.split()}
hits = [(d, h, a) for d, h, a in rows if h.lower() in BIG5 or a.lower() in BIG5]
print(f"picks containing a Big-5 / Champions-league-club name: {len(hits)} / {len(rows)} = {len(hits)/len(rows)*100:.1f}%")
print("  (site FAQ claims: \"We only focus on the major football leagues\")")
print()
print("  sample of that subset:")
for d, h, a in hits[:12]:
    print(f"    {d}  {h} vs {a}")
print()

once = Counter()
names = [n for _, h, a in rows for n in (h, a)]
for n in names: once[n] += 1
uniq = sum(1 for n in once if once[n] == 1)
print(f"distinct club names across the archive: {len(once)}")
print(f"  clubs appearing exactly once: {uniq} ({uniq/len(once)*100:.1f}%)")
print("  -> an archive with almost no repeat fixtures across 27 months is a")
print("     rolling scan of low-liquidity leagues, not a coverage of major ones.")
print()
print("MOST-REPEATED clubs (proxy for league focus):")
for n, k in once.most_common(12):
    print(f"    {k:>3}  {n}")
