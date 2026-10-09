"""Local projection of published forecasts and currently usable prices.

No network calls, collection, migrations or writes occur here. Publication
deadlines never renew merely because a visitor reads a saved board.
"""
from collections import Counter
from datetime import datetime, timedelta
import math

BROWSER_SOURCES = {'pinnacle_browser', 'sportybet_browser'}


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _reference(row):
    values = tuple(row.get(key) for key in ('best_source', 'price_source_event_id',
                                           'price_book_key', 'price_quote_identity'))
    return values if all(isinstance(value, str) and value for value in values) else None


def _current_offers(storage, references):
    import json
    references = list(references)
    found = {}
    if not references:
        return found, False
    try:
        with storage._tx() as conn:
            for start in range(0, len(references), 100):
                group = references[start:start + 100]
                records = conn.execute('SELECT q.source, q.event_id, q.book_key, q.identity, '
                    'q.payload, e.payload AS event_payload, s.next_attempt '
                    'FROM scalper_quotes q JOIN scalper_events e '
                    'ON e.source=q.source AND e.event_id=q.event_id '
                    'LEFT JOIN scalper_sources s ON s.source=q.source '
                    'WHERE (q.source,q.event_id,q.book_key,q.identity) IN ('
                    + ','.join('(?,?,?,?)' for _ in group) + ')',
                    tuple(value for reference in group for value in reference)).fetchall()
                for record in records:
                    key = tuple(record[name] for name in ('source', 'event_id', 'book_key', 'identity'))
                    quote, event = json.loads(record['payload']), json.loads(record['event_payload'])
                    if not isinstance(quote, dict) or not isinstance(event, dict):
                        return {}, True
                    found[key] = (quote, event, record['next_attempt'] or 0)
    except Exception:
        # A missing staging schema or failed database check cannot prove an offer.
        return {}, True
    return found, False


def _eligibility(row, offers, unavailable, settings, now):
    source = row.get('best_source')
    browser = source in BROWSER_SOURCES
    seen = _time(row.get('price_confirmed_at') if browser else row.get('price_updated_at'))
    age_limit = settings.scalper_quote_max_age_sec if (
        browser or row.get('price_source_event_id')) else 1800
    if seen is None or seen > now + timedelta(seconds=60):
        return 'unverified_timestamp', None
    if browser and row.get('price_freshness_basis') != 'publisher_snapshot':
        return 'unverified_timestamp', None
    deadline = seen + timedelta(seconds=age_limit)
    if row.get('price_valid_until') is not None:
        explicit = _time(row['price_valid_until'])
        if explicit is None:
            return 'unverified_expiry', None
        deadline = min(deadline, explicit)
    kickoff = _time(row.get('kickoff'))
    if kickoff:
        deadline = min(deadline, kickoff)
    if now >= deadline:
        return 'expired', deadline
    price = row.get('best_odds')
    if (isinstance(price, bool) or not isinstance(price, (float, int))
            or not math.isfinite(price) or price < settings.board_min_offer_odds):
        return 'below_offer_floor', deadline
    reference = _reference(row)
    if browser or row.get('price_source_event_id'):
        if reference is None:
            return 'unverified_offer', deadline
        if unavailable:
            return 'offer_state_unavailable', deadline
        current = offers.get(reference)
        if current is None:
            return 'removed', deadline
        quote, event, next_attempt = current
        if quote.get('active') is not True or event.get('status') not in ('SCHEDULED', 'UNKNOWN'):
            return 'suspended', deadline
        latest_price = quote.get('odds')
        if (not isinstance(latest_price, (int, float)) or isinstance(latest_price, bool)
                or not math.isclose(price, latest_price, rel_tol=0, abs_tol=1e-10)):
            return 'price_changed', deadline
        event_kickoff = _time(event.get('kickoff'))
        if (event.get('sport_key') != row.get('sport_key') or event_kickoff is None
                or kickoff is None or abs((event_kickoff - kickoff).total_seconds()) > 900):
            return 'fixture_changed', deadline
        current_seen = _time(quote.get('confirmed_at') if browser else quote.get('updated_at'))
        if (current_seen is None or current_seen > now + timedelta(seconds=60)
                or (now - current_seen).total_seconds() > age_limit):
            return 'expired', deadline
        if browser and next_attempt > now.timestamp():
            deadline = min(deadline, current_seen + timedelta(seconds=30))
            if now >= deadline:
                return 'disconnected', deadline
    return 'fresh', deadline


def project_prices(payload, storage, settings, now):
    """Update a private copy of a publication; return public readiness telemetry."""
    board = payload.get('board') or {}
    collections = ('winning', 'earning', 'micro_bets')
    rows = [row for name in collections for row in board.get(name, [])]
    rows += [row for acc in board.get('accumulators', []) for row in acc.get('legs', [])]
    rows += board.get('research', [])
    offers, unavailable = _current_offers(storage, {_reference(row) for row in rows
        if row.get('priced') and _reference(row) is not None})
    selections = {}
    for row in rows:
        state = row.get('price_state', 'model_only')
        if row.get('priced'):
            state, deadline = _eligibility(row, offers, unavailable, settings, now)
            row['price_valid_until'] = deadline.isoformat() if deadline else None
            if state != 'fresh':
                row.update(priced=False, best_odds=None, best_book=None, best_source=None,
                           ev=None, stake_fraction=0.0, basis='model_only',
                           reason='Price unavailable: ' + state.replace('_', ' ') + '; model forecast retained')
        if not row.get('priced'):
            row.update(best_odds=None, best_book=None, best_source=None, ev=None,
                       stake_fraction=0.0, basis='model_only')
        row['price_state'] = state
        key = (row.get('match_id'), row.get('market'), row.get('line'), row.get('selection'))
        selections.setdefault(key, row)
    board['earning'] = [row for row in board.get('earning', []) if row.get('priced')]
    invalid_accumulators = 0
    for acc in board.get('accumulators', []):
        if any(not row.get('priced') for row in acc.get('legs', [])):
            invalid_accumulators += 1
            acc.update(priced=False, best_odds=None, best_book=None, ev=None, stake_fraction=0.0)
            warning = 'One or more legs lack a current price; this combination is a model forecast.'
            if warning not in acc.setdefault('warnings', []):
                acc['warnings'].append(warning)
    counts = Counter(row['price_state'] for row in selections.values())
    fixtures_priced = len({row.get('match_id') for row in selections.values() if row.get('priced')})
    coverage = board.get('coverage')
    scope = 'published_model_candidates' if board.get('research') else 'published_selections'
    if isinstance(coverage, dict):
        coverage.setdefault('fixtures_priced_at_generation', coverage.get('fixtures_priced', 0))
        coverage['fixtures_priced'] = fixtures_priced
        coverage['price_readiness_scope'] = scope
    failed_sources = {value.get('name', 'unknown') for value in payload.get('providers', [])
                      if value.get('error')}
    failed_sources.update(reference[0] for reference, current in offers.items()
                          if current[2] > now.timestamp())
    failed_sources = sorted(failed_sources)
    if counts.get('disconnected') or counts.get('offer_state_unavailable'):
        collection_state = 'unavailable'
    else:
        collection_state = 'degraded' if failed_sources or payload.get('errors') else (
            'ok' if payload.get('providers') else 'not_observed')
    state = 'ready' if fixtures_priced else 'unavailable' if collection_state == 'unavailable' else (
        'expired' if counts.get('expired') else 'awaiting_prices' if selections else 'no_selections')
    return dict(observed_at=now.isoformat(), ready=bool(fixtures_priced), state=state,
                scope=scope, fixtures_priced=fixtures_priced,
                selections=dict(counts), fresh_earning_selections=len(board['earning']),
                opportunity_state='qualifying_earning' if board['earning'] else 'no_qualifying_earning',
                accumulators_without_current_leg_prices=invalid_accumulators,
                collection_state=collection_state, failed_sources=failed_sources)
