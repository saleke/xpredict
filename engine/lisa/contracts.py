"""Full-time market contracts and exact unit-stake payout distributions.

Prematch regulation time including stoppage time; extra time and shootouts are
excluded. Period-specific contracts must use their own observations/models.
"""
import math
import re
from dataclasses import dataclass

GRADES = ('WIN', 'HALF_WIN', 'VOID', 'HALF_LOSS', 'LOSS')


def unit_profit(grade, odds):
    if not math.isfinite(odds) or odds <= 1:
        raise ValueError('finite decimal odds greater than one required')
    return {'WIN': odds - 1, 'HALF_WIN': (odds - 1) / 2,
            'VOID': 0., 'HALF_LOSS': -.5, 'LOSS': -1.}[grade]


def split_line(line):
    if line is None or not math.isfinite(line) or abs(line * 4 - round(line * 4)) > 1e-8:
        raise ValueError('a whole, half or quarter line is required')
    if round(line * 4) % 2:
        return (math.floor(line * 2) / 2, math.ceil(line * 2) / 2)
    return (line,)


def _grade_sign(value):
    return 'WIN' if value > 1e-8 else 'LOSS' if value < -1e-8 else 'VOID'


def _merge(parts):
    if len(parts) == 1 or parts[0] == parts[-1]:
        return parts[0]
    if set(parts) == {'WIN', 'VOID'}:
        return 'HALF_WIN'
    if set(parts) == {'LOSS', 'VOID'}:
        return 'HALF_LOSS'
    raise ValueError('invalid split payout')


@dataclass(frozen=True)
class MarketContract:
    market: str
    selection: str
    line: float | None = None
    period: str = 'regulation'

    def grade(self, home, away, *, home_corners=None, away_corners=None):
        if self.period != 'regulation':
            raise ValueError('only regulation-period contracts are modelled')
        if any(type(v) is not int or v < 0 for v in (home, away)):
            raise ValueError('nonnegative integer final scores required')
        side = self.selection.strip().lower()
        if self.market in ('h2h', 'double_chance', 'draw_no_bet'):
            result = 'home' if home > away else 'away' if away > home else 'draw'
            if self.market == 'double_chance':
                options = {'1x': ('home', 'draw'), 'x2': ('draw', 'away'), '12': ('home', 'away')}
                if side not in options:
                    raise ValueError('invalid double chance selection')
                return 'WIN' if result in options[side] else 'LOSS'
            if side not in ('home', 'away', 'draw') or (self.market == 'draw_no_bet' and side == 'draw'):
                raise ValueError('invalid winner selection')
            return 'VOID' if self.market == 'draw_no_bet' and result == 'draw' else 'WIN' if side == result else 'LOSS'
        if self.market == 'btts':
            if side not in ('yes', 'no'):
                raise ValueError('invalid BTTS selection')
            return 'WIN' if (home > 0 and away > 0) == (side == 'yes') else 'LOSS'
        if self.market == 'correct_score':
            score = re.fullmatch(r'(\d+)\s*[-:]\s*(\d+)', side)
            if not score:
                raise ValueError('invalid correct score')
            return 'WIN' if (home, away) == tuple(map(int, score.groups())) else 'LOSS'
        if self.market == 'asian_handicap':
            if side not in ('home', 'away'):
                raise ValueError('handicap line is from selected team perspective')
            margin = home - away if side == 'home' else away - home
            return _merge([_grade_sign(margin + line) for line in split_line(self.line)])
        if self.market in ('totals', 'home_team_totals', 'away_team_totals', 'corners'):
            if side not in ('over', 'under'):
                raise ValueError('invalid total selection')
            total = home + away
            if self.market == 'home_team_totals':
                total = home
            elif self.market == 'away_team_totals':
                total = away
            elif self.market == 'corners':
                if any(type(v) is not int or v < 0 for v in (home_corners, away_corners)):
                    raise ValueError('final regulation corner counts are required')
                total = home_corners + away_corners
            return _merge([_grade_sign((total - line) * (1 if side == 'over' else -1))
                           for line in split_line(self.line)])
        raise ValueError('unsupported market')

    def distribution(self, matrix):
        probabilities = {grade: 0. for grade in GRADES}
        total = 0.
        for home, row in enumerate(matrix):
            for away, p in enumerate(row):
                if not math.isfinite(p) or p < 0:
                    raise ValueError('invalid score probability')
                probabilities[self.grade(home, away)] += p
                total += p
        if abs(total - 1) > 1e-7:
            raise ValueError('score matrix must sum to one')
        return PayoutDistribution(probabilities)


@dataclass(frozen=True)
class PayoutDistribution:
    probabilities: dict

    def __post_init__(self):
        if set(self.probabilities) - set(GRADES) or any(not math.isfinite(p) or p < 0
                for p in self.probabilities.values()) or abs(sum(self.probabilities.values()) - 1) > 1e-7:
            raise ValueError('invalid payout probabilities')

    @property
    def positive_probability(self):
        return self.probabilities.get('WIN', 0) + self.probabilities.get('HALF_WIN', 0)

    @property
    def fair_odds(self):
        wins = self.probabilities.get('WIN', 0) + .5 * self.probabilities.get('HALF_WIN', 0)
        losses = self.probabilities.get('LOSS', 0) + .5 * self.probabilities.get('HALF_LOSS', 0)
        return 1 + losses / wins if wins > 0 else None

    def ev(self, odds):
        return sum(p * unit_profit(grade, odds) for grade, p in self.probabilities.items())

    def kelly(self, odds, *, fraction=.25, cap=.02):
        if not 0 <= fraction <= 1 or not 0 <= cap < 1:
            raise ValueError('invalid stake bounds')
        if self.ev(odds) <= 0:
            return 0.
        returns = [(p, unit_profit(g, odds)) for g, p in self.probabilities.items() if p > 0]
        # Concave expected-log wealth; its decreasing derivative has one root.
        def derivative(stake):
            return sum(p * r / (1 + stake * r) for p, r in returns)
        lower, upper = 0., 1 - 1e-12
        for _ in range(60):
            middle = (lower + upper) / 2
            if derivative(middle) > 0:
                lower = middle
            else:
                upper = middle
        return min(cap, fraction * (lower + upper) / 2)


def same_game_probability(matrix, contracts):
    """Joint full-win probability, calculated from shared goal outcomes.

    Push/split markets require combined payout accounting, not a binary joint.
    Unsupported and corner markets are rejected rather than treated independent.
    """
    if not contracts:
        raise ValueError('at least one contract required')
    probability = 0.
    for home, row in enumerate(matrix):
        for away, p in enumerate(row):
            grades = [contract.grade(home, away) for contract in contracts]
            if any(g not in ('WIN', 'LOSS') for g in grades):
                raise ValueError('push/split same-game combinations require a payout distribution')
            if all(g == 'WIN' for g in grades):
                probability += p
    return probability


def is_binary_contract(market, line=None):
    """Whether a probability can be measured by ordinary win/loss calibration."""
    if market == 'draw_no_bet':
        return False
    if market in ('totals', 'home_team_totals', 'away_team_totals', 'corners', 'asian_handicap', 'spreads'):
        try:
            return line is not None and math.isfinite(float(line)) and float(line) % 1 == .5
        except (ValueError, TypeError):
            return False
    return True
