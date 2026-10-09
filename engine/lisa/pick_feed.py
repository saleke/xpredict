"""Select qualifying match headlines from the model's research population.

Selection runs on a price-checked publication, never on old ledger prices.
Every match contributes at most one headline. Subscription depth changes access,
not the quality threshold or the underlying ranking.
"""
from __future__ import annotations

from collections import Counter
import copy
from datetime import datetime, timezone
import math

from .contracts import MarketContract, unit_profit
from .model_policy import MODEL_VERSION
from .market_policy import prominent_goal_line
from .storage import pick_key


def _number(value):
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def kickoff_order(row):
    """Order fixtures by their actual instant; unknown kickoffs come last."""
    return (_time(row.get('commence_time') or row.get('kickoff') or row.get('commence_at'))
            or datetime.max.replace(tzinfo=timezone.utc))


def opportunity_card(row, *, paper=False, unproven=False):
    """Keep the feed's existing HTTP shape while carrying current price proof."""
    card = copy.deepcopy(row)
    card.update(home_team=row.get('home'), away_team=row.get('away'),
        commence_time=row.get('kickoff'), outcome_name=row.get('selection'),
        p_true=row.get('p_model'), best_ev=row.get('ev'), source=MODEL_VERSION,
        model_forecast=True, dedupe_key=pick_key(row['match_id'], row['market'], row['selection'], row.get('line')),
        is_recommendation=bool(not paper and not unproven and row.get('stake_fraction', 0) > 0),
        recommended_stake_pct=0 if paper or unproven else row.get('stake_fraction', 0) * 100,
        recommended_units=0, n_books=1 if row.get('priced') else 0,
        freshness='FRESH' if row.get('priced') else 'UNPRICED')
    return card


def _quality(row):
    """Probability-weighted expected log growth at a fixed 2% comparison stake.

    This comparison stake is only a ranking device, never staking advice. Split
    wins/refunds use the full payout distribution instead of binary arithmetic.
    """
    p, odds = row['p_true'], row['best_odds']
    distribution = row.get('payout_probabilities') or {'WIN': p, 'LOSS': 1 - p}
    try:
        weights = [_number(value) for value in distribution.values()]
        if any(value is None or value < 0 for value in weights) or not math.isclose(sum(weights), 1, abs_tol=.001):
            return None
        growth = sum(float(probability) * math.log1p(.02 * unit_profit(grade, odds))
                     for grade, probability in distribution.items())
    except (ValueError, TypeError, KeyError):
        return None
    return p * growth if growth > 0 else None


def select_headlines(rows, *, now, min_probability=.55, min_odds=1.18, min_ev=.03, limit=0,
                     max_total_line=5.5, max_team_total_line=3.5, qualified_rows=None):
    """Filter and choose one winner per match. Zero/None disables a count cap."""
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError('pick feed limit must be a nonnegative integer or None')
    rejected = Counter()
    unique = {}
    for original in rows:
        row = copy.deepcopy(original)
        kickoff, deadline = _time(row.get('commence_time')), _time(row.get('price_valid_until'))
        p, odds, ev = (_number(row.get(key)) for key in ('p_true', 'best_odds', 'best_ev'))
        reason = None
        if not row.get('match_id') or kickoff is None or kickoff <= now:
            reason = 'not_upcoming'
        elif not prominent_goal_line(row.get('market'), row.get('line'), match_max=max_total_line, team_max=max_team_total_line):
            reason = 'extreme_or_invalid_goal_line'
        elif p is None or not min_probability <= p <= 1:
            reason = 'probability_below_threshold'
        elif not row.get('priced') or deadline is None or deadline <= now:
            reason = 'no_current_price'
        elif odds is None or odds < min_odds:
            reason = 'odds_below_floor'
        elif ev is None or ev < min_ev:
            reason = 'edge_below_threshold'
        if reason:
            rejected[reason] += 1
            continue
        try:
            if row['market'] in ('h2h', 'double_chance', 'btts', 'draw_no_bet', 'correct_score') and row.get('line') is not None:
                raise ValueError('this market has no numeric line')
            MarketContract(row['market'], row['outcome_name'], row.get('line'),
                period=row.get('period', 'regulation')).grade(0, 0, home_corners=0, away_corners=0)
        except (ValueError, TypeError, KeyError, AttributeError):
            rejected['unsupported_contract'] += 1
            continue
        row.update(p_true=p, best_odds=odds, best_ev=ev)
        score = _quality(row)
        if score is None:
            rejected['invalid_or_unattractive_payout'] += 1
            continue
        row['selection_score'] = score
        row['selection_reason'] = 'Best qualifying probability and price value for this match'
        key = row.get('dedupe_key') or pick_key(row['match_id'], row['market'], row['outcome_name'], row.get('line'))
        row['dedupe_key'] = key
        unique[key] = row

    def order(row):
        return (-bool(row.get('is_recommendation')), -row['selection_score'],
                -row['p_true'], -row['best_ev'], _time(row['commence_time']), row['dedupe_key'])

    ranked = sorted(unique.values(), key=order)
    if qualified_rows is not None:
        qualified_rows.extend(copy.deepcopy(ranked))
    winners = {}
    for row in ranked:
        winners.setdefault(row['match_id'], row)
    selected = sorted(winners.values(), key=order)
    if limit:
        selected = selected[:limit]
    for rank, row in enumerate(selected, 1):
        row['rank'] = rank
    # Quality determines the best market, optional cap and subscription access.
    # Presentation follows the schedule, independently of that quality rank.
    selected.sort(key=lambda row: (kickoff_order(row), row['rank']))
    return selected, {'policy': 'one-selection-per-match-v1', 'state': 'ready' if selected else 'no_qualifying_prices',
        'candidate_count': len(rows), 'qualifying_candidates': len(unique),
        'qualifying_matches': len(winners), 'selected_count': len(selected),
        'selected_matches': len(selected), 'limit': limit or None, 'max_per_match': 1,
        'display_order': 'kickoff_asc',
        'min_probability': min_probability, 'min_offered_odds': min_odds, 'min_ev': min_ev,
        'filtered': dict(rejected), 'alternatives_count': len(unique) - len(selected)}


def publication_feed(publication, settings, *, now=None, limit=None, qualified_rows=None):
    now = now or datetime.now(timezone.utc)
    board = publication.get('board') or {}
    # Old publications remain readable during the first worker cycle after an
    # upgrade. New ones retain the complete research population before caps.
    rows = board.get('research') or [row for name in ('winning', 'earning', 'micro_bets') for row in board.get(name, [])]
    cards = [opportunity_card(row, paper=publication.get('paper_mode', False), unproven=board.get('unproven', True)) for row in rows]
    return select_headlines(cards, now=now,
        min_probability=max(getattr(settings, 'board_min_model_prob', .12), getattr(settings, 'pick_feed_min_probability', .55)),
        min_odds=getattr(settings, 'board_min_offer_odds', 1.18),
        min_ev=getattr(settings, 'board_min_ev', .03),
        max_total_line=getattr(settings, 'board_max_total_line', 5.5),
        max_team_total_line=getattr(settings, 'board_max_team_total_line', 3.5),
        qualified_rows=qualified_rows,
        limit=getattr(settings, 'pick_feed_limit', 0) if limit is None else limit)


def tier_feed(rows, tier, *, telegram_verified=False):
    """Return allowed rows and safe teasers; locked rows reveal no prediction."""
    from .tiers import clean_tier, PICK_FEED_LIMITS
    tier = 'tier3' if tier == 'admin' else clean_tier(tier)
    allowance = PICK_FEED_LIMITS[tier]
    if tier == 'free' and telegram_verified:
        allowance = 2
    result = []
    for index, original in enumerate(rows):
        row = copy.deepcopy(original)
        rank = original.get('rank')
        if type(rank) is not int or rank < 1:
            rank = index + 1
        required = 'FREE' if rank == 1 else 'TELEGRAM_UNLOCK' if rank == 2 else 'TIER_1' if rank <= 5 else 'TIER_2'
        row.update(rank=rank, tier_level=required, is_locked=allowance is not None and rank > allowance)
        if row['is_locked']:
            # Whitelist teasers. A line, fair price, probability, key or payout
            # distribution can reveal the supposedly hidden selection.
            row = {key: row.get(key) for key in ('match_id', 'sport_key', 'home_team', 'away_team', 'commence_time', 'rank', 'tier_level')}
            label = 'Join Telegram to Unlock Pick #2' if rank == 2 else 'Tier 1 Required' if rank <= 5 else 'Tier 2 Required'
            row.update(is_locked=True, outcome_name='🔒 ' + label, best_odds=None, fair_odds=None,
                best_ev=None, p_true=None, is_recommendation=False, recommended_stake_pct=0,
                recommended_units=0, booking_codes={}, deep_links={}, gauge_text=label)
        result.append(row)
    return sorted(result, key=lambda row: (kickoff_order(row), row['rank']))


def project_board_access(publication, settings, tier, *, telegram_verified=False, now=None):
    """Apply feed entitlements to every financial/research route consistently."""
    qualified = []
    rows, info = publication_feed(publication, settings, now=now, qualified_rows=qualified)
    visible = tier_feed(rows, tier, telegram_verified=telegram_verified)
    allowed = {row['dedupe_key'] for row in visible if not row['is_locked']}
    board = publication.get('board') or {}
    population = board.get('research') or [row for name in ('winning', 'earning', 'micro_bets') for row in board.get(name, [])]
    indexed = {pick_key(row['match_id'], row['market'], row['selection'], row.get('line')): row for row in population}
    board['earning'] = [copy.deepcopy(indexed[row['dedupe_key']]) for row in rows if row['dedupe_key'] in allowed]
    from .tiers import clean_tier, tier_rank
    rank = 3 if tier == 'admin' else tier_rank(clean_tier(tier))
    board['winning'] = copy.deepcopy(board['earning'])
    # Premium depth uses the SAME acceptance floor. Suppressed calculations
    # stay private; do not sell negative-edge or unoffered lines as research.
    primary = {row['match_id']: row for row in rows}
    seen_markets = {(row['match_id'], row['market']) for row in rows}
    alternative_counts = Counter()
    alternatives = []
    for row in qualified:
        identity = (row['match_id'], row['market'])
        if row['match_id'] not in primary or identity in seen_markets or alternative_counts[row['match_id']] >= 2:
            continue
        seen_markets.add(identity)
        alternative_counts[row['match_id']] += 1
        alternatives.append(copy.deepcopy(indexed[row['dedupe_key']]))
    board['research'] = sorted(alternatives, key=kickoff_order) if rank >= 3 else []
    board['micro_bets'] = copy.deepcopy(board['research'])
    qualifying_keys = {row['dedupe_key'] for row in qualified}
    accumulators = []
    min_joint = max(getattr(settings, 'board_min_accumulator_prob', .02),
                    getattr(settings, 'pick_feed_min_accumulator_probability', .35))
    for acca in board.get('accumulators', []) if rank >= 2 else []:
        legs = acca.get('legs') or []
        joint = _number(acca.get('p_adjusted'))
        if (joint is None or not min_joint <= joint <= 1 or len(legs) < 2
                or len({leg.get('match_id') for leg in legs}) != len(legs)):
            continue
        keys = [pick_key(leg['match_id'], leg['market'], leg['selection'], leg.get('line')) for leg in legs]
        if any(key not in qualifying_keys for key in keys):
            continue
        updated = copy.deepcopy(acca)
        updated['legs'] = sorted((copy.deepcopy(indexed[key]) for key in keys), key=kickoff_order)
        # Straight prices do not confirm a bookmaker's combined offer.
        updated.update(priced=False, best_odds=None, best_book=None, ev=None, stake_fraction=0)
        accumulators.append(updated)
    board['accumulators'] = sorted(accumulators, key=lambda acca: (kickoff_order(acca['legs'][0]), -acca['p_adjusted']))
    board['execution_locked'] = rank < 1
    board['research_access'] = {'available': rank >= 3, 'minimum_tier': 'tier3',
        'candidate_count': len(alternatives), 'evaluated_candidates': len(population),
        'qualifying_candidates': info['qualifying_candidates'], 'max_alternatives_per_match': 2}
    publication['pick_feed'] = dict(info, tier=tier, unlocked_count=len(allowed))
    publication['active_picks'] = visible
    return publication
