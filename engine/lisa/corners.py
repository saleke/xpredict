"""Opponent-adjusted, shrunk corner-total baseline, independent of goal scores.

A negative binomial models excess count variance; the Poisson limit is used
when residual variance does not support overdispersion. This baseline needs
walk-forward validation before any profitability claim. No same-game goal/corner
independence assumption is made.
"""
import math
from collections import defaultdict
from datetime import timedelta


def count_pmf(mean, alpha=0., *, tolerance=1e-10, limit=1000):
    if not math.isfinite(mean) or mean <= 0 or not math.isfinite(alpha) or alpha < 0:
        raise ValueError('positive finite mean and nonnegative dispersion required')
    if alpha <= 1e-8:
        probability = math.exp(-mean)
        ratio = lambda k: mean / (k + 1)
    else:
        size = 1 / alpha
        q = mean / (mean + size)
        probability = math.exp(-size * math.log1p(mean / size))
        ratio = lambda k: (k + size) / (k + 1) * q
    probabilities = [probability]
    total = probability
    for k in range(limit - 1):
        probability *= ratio(k)
        probabilities.append(probability)
        total += probability
        if k + 1 > mean and 1 - total <= tolerance:
            return tuple(p / total for p in probabilities)
    raise ValueError('count distribution tail exceeds numerical budget')


class CornerTotalModel:
    def __init__(self, *, shrinkage=12., xi=.55):
        self.shrinkage, self.xi = shrinkage, xi
        self.stats = {}
        self.games = {}
        self.home_mean = self.away_mean = 0.
        self.alpha = 0.
        self.matches = 0

    def fit(self, rows, *, as_of):
        self.stats, self.games = {}, {}
        usable = []
        for row in rows:
            if not row.get('completed') or any(type(row.get(k)) is not int or not 0 <= row[k] <= 60
                    for k in ('home_corners', 'away_corners')):
                continue
            from datetime import datetime
            kickoff = datetime.fromisoformat(row['kickoff'])
            if kickoff + timedelta(hours=3) >= as_of:
                continue
            weight = math.exp(-self.xi * (as_of - kickoff).total_seconds() / 31557600)
            usable.append((row, weight))
        self.matches = len(usable)
        if not usable:
            return self
        weight = sum(w for _, w in usable)
        self.home_mean = sum(r['home_corners'] * w for r, w in usable) / weight
        self.away_mean = sum(r['away_corners'] * w for r, w in usable) / weight
        # Tiny means indicate corrupt/no-corner coverage; do not invent a model.
        if min(self.home_mean, self.away_mean) <= .1:
            self.matches = 0
            return self
        values = defaultdict(lambda: [0., 0., 0.])
        counts = defaultdict(int)
        for r, w in usable:
            h, a = r['home_team'], r['away_team']
            hc, ac = r['home_corners'], r['away_corners']
            for team, venue, scored, allowed in ((h, 'home', hc, ac), (a, 'away', ac, hc)):
                s = values[(team, venue)]
                s[0] += w * scored
                s[1] += w * allowed
                s[2] += w
                counts[team] += 1
        self.stats, self.games = dict(values), dict(counts)
        # Residual moment estimate removes predictable between-team variation.
        numerator = denominator = 0.
        for r, w in usable:
            mean = self.expected(r['home_team'], r['away_team'])
            y = r['home_corners'] + r['away_corners']
            numerator += w * ((y - mean) ** 2 - y)
            denominator += w * mean ** 2
        self.alpha = max(0., min(2., numerator / denominator if denominator else 0.))
        return self

    def expected(self, home, away):
        def rates(team, venue, attack_prior, conceded_prior):
            scored, allowed, n = self.stats.get((team, venue), (0, 0, 0))
            return ((scored + self.shrinkage * attack_prior) / (n + self.shrinkage),
                    (allowed + self.shrinkage * conceded_prior) / (n + self.shrinkage))
        hfor, hagainst = rates(home, 'home', self.home_mean, self.away_mean)
        afor, aagainst = rates(away, 'away', self.away_mean, self.home_mean)
        return max(.2, min(40., hfor * aagainst / self.home_mean + afor * hagainst / self.away_mean))

    def predict(self, home, away):
        if self.matches < 50 or min(self.games.get(home, 0), self.games.get(away, 0)) < 10:
            return None
        mean = self.expected(home, away)
        pmf = count_pmf(mean, self.alpha)
        cumulative = 0.
        over = {}
        for k, p in enumerate(pmf):
            cumulative += p
            if 4 <= k <= 15:
                over[f'{k}.5'] = max(0., min(1., 1 - cumulative))
        return {'expected_corners': mean, 'dispersion': self.alpha,
                'distribution': pmf, 'over': over, 'matches_used': self.matches,
                'minimum_team_games': min(self.games[home], self.games[away]),
                'validation': 'research_baseline'}
