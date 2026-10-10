#!/usr/bin/env python3
"""Blocked chronological evaluation of current goal/corner models on real CSVs.

Models refit at UTC month boundaries. No result from the evaluated month enters
its fit. Archive odds have no executable timestamp; returns/production approval
are deliberately not inferred from them. No tuning occurs on the test period.
"""
import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from import_history import read_csv
from lisa.feed import to_scored
from lisa.league_model import LeagueGoalModel
from lisa.corners import CornerTotalModel, count_pmf
from lisa.model_policy import MODEL_VERSION


def metrics(rows):
    if not rows:
        return {'samples': 0}
    n = len(rows)
    p = [r[0] for r in rows]
    y = [r[1] for r in rows]
    brier = sum((prob - result) ** 2 for prob, result in rows) / n
    log_loss = -sum(result * math.log(max(1e-12, prob)) + (1 - result) * math.log(max(1e-12, 1 - prob))
                    for prob, result in rows) / n
    bins = defaultdict(list)
    for prob, result in rows:
        bins[min(9, int(prob * 10))].append((prob, result))
    ece = sum(abs(sum(prob - result for prob, result in group)) for group in bins.values()) / n
    return {'samples': n, 'brier': round(brier, 6), 'log_loss': round(log_loss, 6), 'ece': round(ece, 6),
            'calibration_bins': [{'bin': index, 'n': len(group),
                'predicted': round(sum(p for p, _ in group) / len(group), 5),
                'observed': round(sum(y for _, y in group) / len(group), 5)} for index, group in sorted(bins.items())]}


def evaluate(rows, test_from):
    by_league = defaultdict(list)
    for row in rows:
        by_league[row['sport_key']].append(row)
    output = {}
    for league, observations in sorted(by_league.items()):
        observations.sort(key=lambda r: r['kickoff'])
        scores = defaultdict(list)
        months = sorted({r['kickoff'][:7] for r in observations if r['kickoff'] >= test_from.isoformat()})
        skipped = 0
        for month in months:
            cutoff = datetime.fromisoformat(month + '-01T00:00:00+00:00')
            history = [r for r in observations if datetime.fromisoformat(r['kickoff']) + timedelta(hours=3) < cutoff
                       and datetime.fromisoformat(r['kickoff']) >= cutoff - timedelta(days=365.25 * 3)]
            if len(history) < 100:
                continue
            goal = LeagueGoalModel()
            goal.fit(to_scored(history, league), as_of=cutoff)
            model = goal.for_league(league)
            corner = CornerTotalModel().fit(history, as_of=cutoff)
            total_mean = sum(r['home_corners'] + r['away_corners'] for r in history
                             if type(r.get('home_corners')) is int and type(r.get('away_corners')) is int)
            corner_n = sum(type(r.get('home_corners')) is int and type(r.get('away_corners')) is int for r in history)
            corner_baseline = count_pmf(total_mean / corner_n) if corner_n and total_mean > 0 else None
            for row in observations:
                if row['kickoff'][:7] != month or row['kickoff'] < test_from.isoformat():
                    continue
                home, away = row['home_team'], row['away_team']
                if not model.knows(home) or not model.knows(away):
                    skipped += 1
                    continue
                prediction = model.predict(home, away)
                h, a = row['home_score'], row['away_score']
                events = {'h2h_home': (prediction['p_home'], h > a),
                          'double_chance_1x': (prediction['double_chance']['1X'], h >= a),
                          'btts_yes': (prediction['p_btts'], h > 0 and a > 0),
                          'goals_over_2_5': (prediction['over']['2.5'], h + a > 2.5)}
                for market, (prob, result) in events.items():
                    scores[market].append((prob, int(result)))
                odds = row['archive_odds']
                for family, side, outcome, label in (('h2h', 'Home', h > a, 'market_home'),
                                                     ('totals_2_5', 'Over', h + a > 2.5, 'market_goals_over_2_5')):
                    prices = odds.get(family, {})
                    required = ('Home', 'Draw', 'Away') if family == 'h2h' else ('Over', 'Under')
                    if all(k in prices for k in required):
                        fair = (1 / prices[side]) / sum(1 / prices[k] for k in required)
                        scores[label].append((fair, int(outcome)))
                        comparison = events['h2h_home' if family == 'h2h' else 'goals_over_2_5'][0]
                        scores[label + '_model_paired'].append((comparison, int(outcome)))
                corners = corner.predict(home, away)
                if corners and type(row.get('home_corners')) is int and type(row.get('away_corners')) is int:
                    actual = row['home_corners'] + row['away_corners'] > 9.5
                    scores['corners_over_9_5'].append((corners['over']['9.5'], int(actual)))
                    if corner_baseline:
                        scores['corners_league_poisson_baseline'].append((sum(corner_baseline[10:]), int(actual)))
        output[league] = {'unknown_team_matches_skipped': skipped,
                          'markets': {market: metrics(values) for market, values in sorted(scores.items())}}
        print(f'Evaluated {league}: {len(months)} chronological blocks', flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-from', default='2024-07-01')
    parser.add_argument('--output', default='data/reports/current-model-evaluation.json')
    parser.add_argument('--league')
    parser.add_argument('files', nargs='*')
    args = parser.parse_args()
    files = [Path(path) for path in args.files] or sorted(Path('engine/lisa/historical').glob('*.csv'))
    rows = [row for path in files for row in read_csv(path)]
    if args.league:
        rows = [r for r in rows if r['sport_key'] == args.league]
    start = datetime.fromisoformat(args.test_from).replace(tzinfo=timezone.utc)
    output = {'model_version': MODEL_VERSION, 'evaluation': 'untuned blocked chronological test',
        'test_from': start.isoformat(), 'source_matches': len(rows),
        'source_hashes': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
        'limitations': ['Retrospective archive; publication times and subsequent data corrections unavailable',
            'Monthly fit freezes all features before each evaluated month; unknown teams are skipped',
            'Market baseline is normalized archive Bet365 odds; quote time unavailable',
            'No historical corner odds, so no corner ROI or executable-price validation',
            'No tuning/calibration on this evaluation period; no profitability approval artifact generated'],
        'leagues': evaluate(rows, start), 'approved_for_staking': False}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2, allow_nan=False) + '\n')
    print(f'Saved evaluation to {args.output}')


if __name__ == '__main__':
    main()
