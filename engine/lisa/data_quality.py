"""Cross-source result checks before training or grading a prediction."""
from .providers.calendar import fixture_groups


def reconcile_results(rows):
    accepted, conflicts = [], []
    for values in fixture_groups(rows):
        finished = [r for r in values if r.get('completed') and str(r.get('status')).upper() not in ('CANCELED', 'CANCELLED') and
            all(type(r.get(k)) is int and r[k] >= 0 for k in ('home_score', 'away_score'))]
        scores = {(r['home_score'], r['away_score']) for r in finished}
        canceled = [r for r in values if str(r.get('status')).upper() in ('CANCELED', 'CANCELLED')]
        # Different final scores, or a final/cancellation disagreement, stay
        # unresolved. Do not turn provider ordering or majority voting into truth.
        if len(scores) > 1 or (finished and canceled):
            conflicts.append({'sport_key': values[0]['sport_key'], 'home': values[0]['home_team'], 'away': values[0]['away_team'],
                              'kind': 'final_score', 'sources': sorted({str(r.get('provider')) for r in values})})
            continue
        corner_pairs = {(r['home_corners'], r['away_corners']) for r in finished
                        if all(type(r.get(k)) is int and r[k] >= 0 for k in ('home_corners', 'away_corners'))}
        for row in values:
            copy = dict(row)
            if len(corner_pairs) > 1:
                copy.pop('home_corners', None)
                copy.pop('away_corners', None)
                copy['corner_conflict'] = True
            accepted.append(copy)
        if len(corner_pairs) > 1:
            conflicts.append({'sport_key': values[0]['sport_key'], 'home': values[0]['home_team'], 'away': values[0]['away_team'],
                              'kind': 'corners', 'sources': sorted({str(r.get('provider')) for r in values})})
    return accepted, conflicts
