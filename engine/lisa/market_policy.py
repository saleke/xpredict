"""Bounds for prominent goal-line selections, separate from model research."""
import math


def prominent_goal_line(market, line, *, match_max=5.5, team_max=3.5):
    maximum = match_max if market == 'totals' else team_max if market in ('home_team_totals', 'away_team_totals') else None
    if maximum is None:
        return True
    return isinstance(line, (int, float)) and not isinstance(line, bool) and math.isfinite(line) and 0 <= line <= maximum
