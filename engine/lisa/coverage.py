"""Deterministic request envelopes; provider reservations remain authoritative."""
import math
from datetime import timezone


def api_football_plan(limit, leagues):
    if type(limit) is not int or limit < 1:
        raise ValueError('Daily request limit must be positive')
    leagues = tuple(dict.fromkeys(leagues))
    ordinary = max(1, int(limit * .8))
    # Allow one catalog lookup per league plus room for bounded enrichment or
    # backfill. Catalog costs remain explicit even on an unusually cold start.
    overhead = min(ordinary, len(leagues) + max(1, ordinary // 10))
    available = max(0, ordinary - overhead)
    calendar_budget = available // 2
    odds_budget = available - calendar_budget
    count = len(leagues)
    rounds = min(calendar_budget, odds_budget) // count if count else 0
    interval = math.ceil(86400 / rounds) if rounds else 86400
    return {'provider': 'api_football', 'configured_daily_limit': limit,
        'ordinary_ceiling': ordinary, 'settlement_reserve': limit - ordinary,
        'catalog_and_enrichment_allowance': overhead,
        'calendar_allowance': calendar_budget, 'odds_allowance': odds_budget,
        'leagues': list(leagues), 'refresh_interval_sec': interval,
        'full_refreshes_per_day': rounds, 'odds_pages_per_league': 1,
        'state': 'budgeted' if rounds else 'insufficient_for_full_daily_coverage',
        'limitations': ['Limits are configured, not authenticated account entitlements.',
            'One odds page per league may leave fixtures or bookmakers uncovered.',
            'History and other workers share the same hard request ledger.',
            'Scheduled collection does not establish executable price freshness.']}


def apply_coverage(providers, settings, storage, now):
    """Configure existing adapters without dropping cached league results."""
    from . import feed
    leagues = settings.board_leagues or tuple(feed._default_leagues(settings))
    plans = []
    routes = feed.calendar_routes(providers.calendar, leagues)
    bulk = providers.named('openfootball')
    scalper = providers.named('scalper')
    if scalper is not None:
        from .scalper.repository import ScalperRepository
        plans.append({'provider': 'scalper', 'mode': settings.scalper_mode,
            'leagues': list(scalper.leagues()), 'role': 'stored fixtures, statistics and observed bookmaker quotes',
            'fixture_max_age_sec': scalper.fixture_max_age, 'quote_max_age_sec': scalper.quote_max_age,
            'collector': ScalperRepository(storage).health(now=now),
            'limitations': ['Independent collector must share this relational database.',
                'Public endpoint availability is not guaranteed.',
                'Executable quotes require provider updates or verified publisher confirmation.']})
    if bulk is not None:
        plans.append({'provider': bulk.name, 'leagues': [key for key in leagues if key in bulk.leagues()],
            'role': 'goal history and provisional fixture discovery', 'api_key_required': False,
            'current_file_cache_sec': bulk.cache_sec, 'completed_file_cache_sec': 30*86400,
            'cold_files_per_league': 2, 'settlement_requests': 0,
            'limitations': ['CC0 files do not guarantee current upstream updates.',
                'Date anchors are not kickoffs; file rows cannot become bets or settle predictions.']})
    api = providers.named('api_football')
    if api is not None:
        supported = [league for league in leagues if league in api.leagues()]
        plan = api_football_plan(settings.api_football_daily_limit, supported)
        plan['calendar_primary_leagues'] = [key for key in supported if routes.get(key, [])[:1] == [api.name]]
        plan['calendar_fallback_leagues'] = [key for key in supported if api.name in routes.get(key, [])[1:]]
        plan['calendar_estimate_scope'] = 'Conservative allowance; fallback calls occur only when earlier routes fail.'
        api.calendar_ttl = plan['refresh_interval_sec']
        api.odds_ttl = plan['refresh_interval_sec']
        api.odds_max_pages = plan['odds_pages_per_league']
        # Rotate both catalog/fixture and odds priority daily. Hard reservations
        # cap actual use even if requests fail or metadata changes mid-period.
        api.coverage_rotation = now.astimezone(timezone.utc).date().toordinal()
        with storage._tx() as conn:
            row = conn.execute('SELECT requests FROM provider_daily_usage WHERE provider=? AND day=?',
                ('api_football', now.astimezone(timezone.utc).date().isoformat())).fetchone()
        plan['locally_reserved_today'] = row['requests'] if row else 0
        plans.append(plan)
    supporting = providers.named('allsports')
    if supporting is not None:
        supported = [league for league in leagues if league in supporting.leagues()]
        plans.append({'provider': 'allsports', 'leagues': supported,
            'calendar_primary_leagues': [key for key in supported if routes.get(key, [])[:1] == [supporting.name]],
            'calendar_fallback_leagues': [key for key in supported if supporting.name in routes.get(key, [])[1:]],
            'configured_hourly_limit': supporting.hourly_limit,
            'ordinary_ceiling': max(1, int(supporting.hourly_limit * .8)),
            'refresh_interval_sec': 900,
            'estimated_calendar_requests_per_hour': len(supported) * 4,
            'calendar_estimate_scope': 'Upper bound if every supported league needs this route; healthy primaries skip it.',
            'odds_enabled': supporting.odds_enabled,
            'limitations': ['Subscription and league entitlements remain unverified.',
                'Odds without bookmaker timestamps remain research only.']})
    odds = next((p for p in providers.prices if p.name == 'oddspapi'), None)
    if odds is not None:
        plans.append({'provider': 'oddspapi',
            'leagues': [league for league in leagues if league in odds.leagues()],
            'configured_monthly_limit': odds.monthly_limit, 'reserve': odds.reserve,
            'refresh_interval_sec': odds.poll_interval_sec,
            'estimated_odds_requests_per_31_days': math.ceil(31 * 86400 / odds.poll_interval_sec),
            'estimated_catalog_requests_per_31_days': 15,
            'limitations': ['Estimated costs exclude failures and forced cache misses.',
                'Account reservations override this estimate; subscription renewal requires validation.']})
    odds = next((p for p in providers.prices if p.name == 'the_odds_api'), None)
    if odds is not None:
        cost = len(odds.regions) * len(odds.markets)
        plans.append({'provider': 'the_odds_api', 'leagues': list(leagues),
            'configured_monthly_credits': odds.monthly_limit, 'reserve': odds.reserve,
            'daily_credit_ceiling': odds.daily_limit, 'credits_per_league_request': cost,
            'maximum_paid_league_requests_per_day': odds.daily_limit // cost,
            'cache_interval_sec': odds.ttl,
            'limitations': ['League catalog and account quota must be verified live.',
                'Cached prices retain bookmaker timestamps and may fail freshness gates.',
                'Least-recently-collected leagues take priority when the budget is partial.',
                'Billing-period renewal is conservative and requires validation.']})
    snapshot = {'updated_at': now.isoformat(), 'requested_leagues': list(leagues),
        'plans': plans, 'calendar_routes': routes,
        'calendar_strategy': 'one verified source per league; fallback on failure or empty response',
        'scope': 'Bulk history, verified calendar routes and existing provider budgets',
        'automatic_key_rotation': False}
    storage.set_telemetry('coverage:plan', snapshot)
    return snapshot
