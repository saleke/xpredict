"""Feed diversity, current publication, payout ranking and tier isolation."""
import copy
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from lisa.config import Settings
from lisa.dashboard import build_dashboard
from lisa.daily_service import DailyService
from lisa.board import Board, Opportunity
from lisa.feed import FeedReport, ProviderSet
from lisa.model_policy import MODEL_VERSION
from lisa.pick_feed import publication_feed, project_board_access, select_headlines, tier_feed
from lisa.server import LISAProductionHandler
from lisa.storage import SqliteStorage

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(autouse=True)
def observation_clock(monkeypatch):
    # Full-suite auth checks can take longer than a quote TTL between module
    # collection and execution. Establish observations when each test starts.
    monkeypatch.setattr(sys.modules[__name__], 'NOW', datetime.now(timezone.utc).replace(microsecond=0))


def opportunity(match='m0', **overrides):
    row = dict(match_id=match, sport_key='soccer_epl', home=match + ' Home', away=match + ' Away',
        kickoff=(NOW + timedelta(hours=2)).isoformat(), market='totals', selection='Under', line=3.5,
        p_model=.8, fair_odds=1.25, best_odds=1.5, ev=.2, priced=True,
        best_book='Test Book', best_source='test', basis='model_vs_market', stake_fraction=0,
        price_updated_at=NOW.isoformat(), price_valid_until=(NOW + timedelta(minutes=5)).isoformat())
    row.update(overrides)
    return row


def publication(rows):
    return dict(generated_at=NOW.isoformat(), published_at=NOW.isoformat(), paper_mode=True,
        board=dict(unproven=True, research=copy.deepcopy(rows), winning=copy.deepcopy(rows[:1]),
                   earning=copy.deepcopy(rows[:25]), micro_bets=copy.deepcopy(rows[:40]),
                   accumulators=[], coverage={'fixtures_modelled': len({row['match_id'] for row in rows})}),
        forecast={'matches': [], 'count': 0})


def feed(rows, **kwargs):
    return publication_feed(publication(rows), Settings(**kwargs), now=NOW)


def test_diversity_happens_before_limit_not_after_earliest_match_flood():
    rows = [opportunity('earliest', market='asian_handicap', selection='Home', line=.5 + index, p_model=.9) for index in range(17)]
    rows += [opportunity('match' + str(index), kickoff=(NOW + timedelta(hours=3 + index)).isoformat()) for index in range(12)]
    selected, info = feed(rows, pick_feed_limit=8)
    assert len(selected) == len({row['match_id'] for row in selected}) == 8
    assert sum(row['match_id'] == 'earliest' for row in selected) == 1
    assert info['qualifying_matches'] == 13 and info['candidate_count'] == 29
    assert [row['rank'] for row in selected] == list(range(1, 9))


def test_a_small_slate_is_not_padded_with_micro_bets():
    selected, info = feed([opportunity('a', market='asian_handicap', selection='Home', line=index + .5) for index in range(17)] + [opportunity('b')])
    assert len(selected) == info['selected_matches'] == 2
    assert info['alternatives_count'] == 16


def test_default_feed_keeps_every_qualifying_match_beyond_fifty():
    rows = [opportunity(str(index)) for index in range(150)]
    rows += [opportunity('0', market='btts', selection='Yes', line=None),
             opportunity('rejected', p_model=.15, best_odds=20, ev=2)]
    selected, info = feed(rows)
    assert len(selected) == len({row['match_id'] for row in selected}) == 150
    assert info['limit'] is None and info['qualifying_matches'] == 150
    assert len(tier_feed(selected, 'tier2')) == 150
    assert all(not row['is_locked'] for row in tier_feed(selected, 'tier3'))
    assert sum(not row['is_locked'] for row in tier_feed(selected, 'tier1')) == 5


def test_optional_positive_cap_and_dashboard_override_are_consistent(tmp_path):
    store = SqliteStorage(str(tmp_path / 'unlimited.db'))
    try:
        saved = publication([opportunity(str(index)) for index in range(150)])
        selected, info = publication_feed(saved, Settings(pick_feed_limit=100), now=NOW)
        assert len(selected) == 100 and info['limit'] == 100
        dashboard = build_dashboard(store, Settings(), publication=copy.deepcopy(saved))
        assert len(dashboard['active_picks']) == 150 and dashboard['pick_feed']['limit'] is None
        dashboard = build_dashboard(store, Settings(pick_feed_limit=100), publication=copy.deepcopy(saved))
        assert len(dashboard['active_picks']) == 100
        dashboard = build_dashboard(store, Settings(pick_feed_limit=100), pending_limit=20, publication=copy.deepcopy(saved))
        assert len(dashboard['active_picks']) == 20
    finally:
        store.close()


def test_operator_can_remove_the_cap_or_set_a_limit_above_fifty():
    from lisa.runtime import RuntimeConfig, OverrideError
    runtime = RuntimeConfig(Settings())
    for limit in (100, 501, 0):
        runtime.apply({'pick_feed_limit': limit})
        assert runtime.settings().pick_feed_limit == limit
    description = next(row for row in runtime.describe() if row['name'] == 'pick_feed_limit')
    assert description['min'] == 0 and description['max'] is None
    with pytest.raises(OverrideError):
        runtime.apply({'pick_feed_limit': -1})
    assert runtime.settings().pick_feed_limit == 0


def test_kickoff_controls_display_while_quality_controls_access_and_optional_cap():
    selected, _ = feed([opportunity('early', p_model=.6, best_odds=1.8, ev=.08),
                       opportunity('later', kickoff=(NOW + timedelta(hours=10)).isoformat())])
    assert [row['match_id'] for row in selected] == ['early', 'later']
    assert [row['rank'] for row in selected] == [2, 1]
    view = tier_feed(selected, 'free')
    assert [row['match_id'] for row in view] == ['early', 'later']
    assert [row['is_locked'] for row in view] == [True, False]
    assert view[0]['best_odds'] is None
    capped, info = feed([opportunity('early', p_model=.6, best_odds=1.8, ev=.08),
                        opportunity('later', kickoff=(NOW + timedelta(hours=10)).isoformat())], pick_feed_limit=1)
    assert [row['match_id'] for row in capped] == ['later'] and info['limit'] == 1


def test_kickoff_order_compares_instants_across_offsets_and_keeps_best_market_per_match():
    early = (NOW + timedelta(days=3)).replace(hour=22, minute=30, second=0)
    later = early + timedelta(minutes=15)
    # The early fixture's text has tomorrow's date, but its instant is earlier.
    early_text = early.astimezone(timezone(timedelta(hours=2))).isoformat()
    selected, info = feed([
        opportunity('later', kickoff=later.isoformat(), p_model=.9),
        opportunity('early', kickoff=early_text, p_model=.7),
        opportunity('early', kickoff=early_text, market='btts', selection='Yes', line=None)])
    assert [row['match_id'] for row in selected] == ['early', 'later']
    assert selected[0]['market'] == 'btts' and selected[0]['p_true'] == .8
    assert info['display_order'] == 'kickoff_asc'


def test_all_board_match_lists_and_accumulator_legs_follow_the_schedule():
    rows = [opportunity(str(index), kickoff=(NOW + timedelta(days=index + 1)).isoformat(),
                        p_model=.85 + .01 * index) for index in range(4)]
    rows += [dict(row, market='btts', selection='Yes', line=None, p_model=.8) for row in rows]
    saved = publication(list(reversed(rows)))
    saved['board']['accumulators'] = [
        {'legs': [rows[3], rows[2]], 'p_adjusted': .8},
        {'legs': [rows[1], rows[0]], 'p_adjusted': .6},
        {'legs': [rows[3], rows[0]], 'p_adjusted': .7}]
    result = project_board_access(saved, Settings(), 'tier3', now=NOW)
    assert [row['match_id'] for row in result['active_picks']] == ['0', '1', '2', '3']
    for name in ('earning', 'winning', 'research', 'micro_bets'):
        assert [row['match_id'] for row in result['board'][name]] == ['0', '1', '2', '3']
    assert [[leg['match_id'] for leg in acca['legs']] for acca in result['board']['accumulators']] == [
        ['0', '3'], ['0', '1'], ['2', '3']]


def test_greater_odds_alone_do_not_win_the_ranking():
    selected, _ = feed([opportunity('a', p_model=.6, best_odds=1.75, ev=.05), opportunity('b')])
    assert selected[0]['match_id'] == 'b'
    selected, _ = feed([opportunity('longshot', p_model=.15, best_odds=20, ev=2)])
    assert selected == []


def test_legacy_dashboard_keeps_quality_access_when_displayed_by_kickoff():
    from lisa.pick_feed import opportunity_card
    rows = [opportunity_card(opportunity('early', p_model=.6)),
            opportunity_card(opportunity('later', p_model=.8, kickoff=(NOW + timedelta(days=1)).isoformat()))]
    for row in rows:
        row['source'] = 'legacy'
    before = copy.deepcopy(rows)
    storage = SimpleNamespace(list_pending_picks=lambda: rows, list_settled_picks=lambda: [])
    dashboard = build_dashboard(storage, Settings())
    assert [row['match_id'] for row in dashboard['active_picks']] == ['early', 'later']
    assert [row['is_locked'] for row in tier_feed(dashboard['active_picks'], 'free')] == [True, False]
    assert rows == before


def test_telegram_tier_labels_use_quality_rank_after_chronological_reordering():
    from lisa.telegram_bot import format_active_top_picks_contract
    selected, _ = feed([opportunity('early', p_model=.6, best_odds=1.8, ev=.08),
                       opportunity('later', kickoff=(NOW + timedelta(days=1)).isoformat())])
    text, _ = format_active_top_picks_contract(selected, user_tier='tier2')
    assert text.index('early Home') < text.index('later Home')
    assert 'Match #1 [✈️ TELEGRAM UNLOCKED]' in text
    assert 'Match #2 [🆓 FREE]' in text


@pytest.mark.parametrize('market,line', [('away_team_totals', 7.5), ('home_team_totals', 4.5), ('totals', 7.5)])
def test_extreme_goal_lines_cannot_be_headlines_even_with_a_price(market, line):
    selected, info = feed([opportunity(market=market, line=line, p_model=.9999)])
    assert selected == [] and info['filtered']['extreme_or_invalid_goal_line'] == 1


@pytest.mark.parametrize('change,reason', [
    ({'priced': False}, 'no_current_price'),
    ({'price_valid_until': NOW.isoformat()}, 'no_current_price'),
    ({'price_valid_until': None}, 'no_current_price'),
    ({'price_valid_until': '2026-10-06T12:00:00'}, 'no_current_price'),
    ({'best_odds': 1.17}, 'odds_below_floor'),
    ({'ev': .029}, 'edge_below_threshold'),
    ({'p_model': float('nan')}, 'probability_below_threshold'),
    ({'p_model': True}, 'probability_below_threshold'),
    ({'kickoff': NOW.isoformat()}, 'not_upcoming'),
])
def test_headlines_require_upcoming_fresh_priced_qualifying_rows(change, reason):
    rows, info = feed([opportunity(**change)])
    assert rows == [] and info['filtered'][reason] == 1


def test_floor_boundary_and_configurable_probability_threshold():
    selected, _ = feed([opportunity(p_model=.95, best_odds=1.18, ev=.121)])
    assert len(selected) == 1
    selected, _ = feed([opportunity()], pick_feed_min_probability=.81)
    assert selected == []


def test_push_probability_uses_exact_payout_rather_than_binary_loss():
    selected, _ = feed([opportunity('refund', market='draw_no_bet', selection='Home', line=None,
        p_model=.55, best_odds=1.4, ev=.12, payout_probabilities={'WIN': .55, 'VOID': .35, 'LOSS': .1})])
    assert len(selected) == 1  # binary .55 * 1.4 - 1 would wrongly exclude it
    assert selected[0]['selection_score'] > 0


def test_malformed_payout_is_rejected_not_ranked_as_value():
    selected, info = feed([opportunity(payout_probabilities={'WIN': .8, 'LOSS': .8})])
    assert selected == [] and info['filtered']['invalid_or_unattractive_payout'] == 1


@pytest.mark.parametrize('tier,verified,count', [('free', False, 1), ('free', True, 2),
    ('tier1', False, 5), ('tier2', False, 8), ('tier3', False, 8), ('admin', False, 8), ('bad', False, 1)])
def test_same_ranked_quality_is_inherited_by_higher_tiers(tier, verified, count):
    selected, _ = feed([opportunity(str(index)) for index in range(8)])
    view = tier_feed(selected, tier, telegram_verified=verified)
    unlocked = [row for row in view if not row['is_locked']]
    assert len(unlocked) == count
    assert [row['dedupe_key'] for row in unlocked] == [row['dedupe_key'] for row in selected[:count]]
    for row in view[count:]:
        assert row['p_true'] is None and row['best_odds'] is None and row['fair_odds'] is None
        assert not {'line', 'selection', 'market', 'payout_probabilities', 'dedupe_key', 'price_quote_identity', 'quotes', 'reason'} & row.keys()


def test_tier_projection_does_not_mutate_the_publication_or_expose_research():
    rows = [opportunity(str(index)) for index in range(8)]
    rows.append(opportunity('0', market='btts', selection='Yes', line=None, p_model=.6, best_odds=2, ev=.2))
    saved = publication(rows)
    before = copy.deepcopy(saved)
    for tier, count in [('free', 1), ('tier1', 5), ('tier2', 8), ('tier3', 8)]:
        value = project_board_access(copy.deepcopy(saved), Settings(), tier, now=NOW)
        assert len(value['board']['earning']) == count
        assert bool(value['board']['research']) is (tier == 'tier3')
        assert bool(value['board']['micro_bets']) is (tier == 'tier3')
    assert saved == before


def test_accumulators_require_tier_two_even_when_first_pick_is_free():
    saved = publication([opportunity('a'), opportunity('b')])
    saved['board']['accumulators'] = [{'legs': [opportunity('a'), opportunity('b')], 'p_adjusted': .6, 'stake_fraction': 0}]
    for tier, allowed in [('free', False), ('tier1', False), ('tier2', True), ('tier3', True)]:
        value = project_board_access(copy.deepcopy(saved), Settings(), tier, now=NOW)
        assert bool(value['board']['accumulators']) is allowed


def test_every_user_facing_list_rejects_bad_candidates_including_premium_research():
    good = opportunity('a')
    bad = [opportunity('b', priced=False), opportunity('c', p_model=.15, best_odds=20, ev=2),
           opportunity('d', ev=-.1), opportunity('a', market='away_team_totals', line=7.5, p_model=.9999),
           opportunity('e', price_valid_until=NOW.isoformat()), opportunity('f', market='cards', line=3.5)]
    saved = publication([good] + bad)
    for tier in ('free', 'tier1', 'tier2', 'tier3'):
        result = project_board_access(copy.deepcopy(saved), Settings(), tier, now=NOW)
        assert result['board']['winning'] == result['board']['earning'] == [good]
        assert result['board']['research'] == result['board']['micro_bets'] == []


def test_premium_alternatives_are_bounded_and_do_not_repeat_headline_market_lines():
    rows = [opportunity('a', market='totals', line=line) for line in (3.5, 4.5, 5.5)]
    rows += [opportunity('a', market=market, selection=selection, line=line)
             for market, selection, line in [('btts', 'Yes', None), ('double_chance', '1X', None),
                                            ('asian_handicap', 'Home', .5), ('home_team_totals', 'Under', 2.5)]]
    value = project_board_access(publication(rows), Settings(), 'tier3', now=NOW)
    assert len(value['board']['earning']) == 1
    assert len(value['board']['research']) == 2
    markets = [r['market'] for r in value['board']['earning'] + value['board']['research']]
    assert len(markets) == len(set(markets))
    assert value['board']['micro_bets'] == value['board']['research']


@pytest.mark.parametrize('change', [{'p_adjusted': .05}, {'legs': [opportunity('a'), opportunity('a')]},
    {'legs': [opportunity('a'), opportunity('b', ev=-.1)]}])
def test_accumulator_analysis_cannot_reintroduce_rejected_legs_or_longshot_combos(change):
    saved = publication([opportunity('a'), opportunity('b', ev=-.1)])
    saved['board']['accumulators'] = [dict(legs=[opportunity('a'), opportunity('b')], p_adjusted=.6, **{})]
    saved['board']['accumulators'][0].update(change)
    result = project_board_access(saved, Settings(), 'tier3', now=NOW)
    assert result['board']['accumulators'] == []


def test_dashboard_uses_latest_prices_and_not_old_ledger_choices(tmp_path):
    store = SqliteStorage(str(tmp_path / 'feed.db'))
    try:
        saved = publication([opportunity('new')])
        store.set_telemetry('daily:board', saved)
        with store._tx() as conn:
            conn.execute('INSERT INTO picks (dedupe_key,match_id,sport_key,market,outcome_name,home_team,away_team,commence_time,p_true,fair_odds,n_books,state,created_at,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('old-key', 'old-match', 'soccer_epl', 'h2h', 'Home', 'Old Home', 'Old Away', opportunity()['kickoff'], .95, 1/.95, 1, 'CONFIRMED', NOW.isoformat(), MODEL_VERSION))
        before = store.list_pending_picks()
        dashboard = build_dashboard(store, Settings())
        assert [row['match_id'] for row in dashboard['active_picks']] == ['new']
        assert store.list_pending_picks() == before
        service = DailyService(store, Settings())
        expired = service.read(now=NOW + timedelta(minutes=6))
        rows, _ = publication_feed(expired, Settings(), now=NOW + timedelta(minutes=6))
        assert rows == [] and expired['board']['research'][0]['best_odds'] is None
        assert store.get_telemetry('daily:board') == saved
    finally:
        store.close()


class Handler(LISAProductionHandler):
    def __init__(self, server, user=None, path='/api/dashboard'):
        self.server, self.user, self.path = server, user, path

    def _get_current_user_and_session(self):
        return self.user, None

    def _send_json(self, data, status=200):
        self.response, self.response_status = data, status


def test_anonymous_and_members_cannot_upgrade_with_tier_query(monkeypatch):
    monkeypatch.setenv('LISA_ADMIN_EMAILS', '')
    monkeypatch.setenv('ADMIN_TELEGRAM_IDS', '')
    for user, expected in [(None, 'free'), ({'id': 'member', 'tier': 'tier1'}, 'tier1'),
                           ({'id': 'owner', 'tier': 'admin'}, 'tier3')]:
        handler = Handler(SimpleNamespace(), user, '/api/dashboard?tier=tier3')
        assert handler._feed_access()[0] == expected


def test_unproven_telegram_operator_identity_cannot_preview_paid_tiers(monkeypatch):
    monkeypatch.setenv('LISA_ADMIN_EMAILS', '')
    monkeypatch.setenv('ADMIN_TELEGRAM_IDS', '1234')
    user = {'id': 'member', 'tier': 'free', 'telegram_id': '1234', 'telegram_verified': False}
    handler = Handler(SimpleNamespace(), user, '/api/dashboard?tier=tier3')
    assert handler._feed_access()[0] == 'free'


@pytest.mark.parametrize('paper,unlocked,expected', [(False, False, 'free'), (True, False, 'free'),
    (False, True, 'free'), (True, True, 'tier3')])
def test_temporary_full_access_requires_an_explicit_paper_server(paper, unlocked, expected):
    settings = Settings(paper_mode=paper, paper_tiers_unlocked=unlocked)
    handler = Handler(SimpleNamespace(settings=settings), None, '/api/dashboard?tier=tier3')
    assert handler._feed_access()[0] == expected


def test_paper_unlock_opens_every_feed_route_without_changing_accounts_or_prices(tmp_path):
    store = SqliteStorage(str(tmp_path / 'paper-access.db'))
    try:
        rows = [opportunity(str(index), kickoff=(NOW + timedelta(days=index + 1)).isoformat(),
                            p_model=.8 + .01 * index) for index in range(8)]
        rows.append(dict(rows[0], market='btts', selection='Yes', line=None))
        store.set_telemetry('daily:board', publication(rows))
        settings = Settings(paper_mode=True, paper_tiers_unlocked=True)
        service = DailyService(store, settings)
        server = SimpleNamespace(storage=store, daily_service=service, settings=settings)
        user = {'id': 'member', 'tier': 'free'}
        before = copy.deepcopy(user)
        for account in (None, user):
            handler = Handler(server, account, '/api/dashboard?tier=free')
            handler._handle_dashboard()
            assert len(handler.response['active_picks']) == 8
            assert [row['match_id'] for row in handler.response['active_picks']] == [str(index) for index in range(8)]
            assert all(not row['is_locked'] and row['recommended_stake_pct'] == 0 for row in handler.response['active_picks'])
            assert handler.response['pick_feed']['paper_tiers_unlocked']
            handler._handle_picks(urlparse('/api/picks?tier=free'))
            assert [row['match_id'] for row in handler.response['active_picks']] == [str(index) for index in range(8)]
            assert all(not row['is_locked'] for row in handler.response['active_picks'])
            handler._handle_opportunity_board(urlparse('/api/opportunity-board?tier=free'))
            assert len(handler.response['board']['earning']) == 8
            assert [row['match_id'] for row in handler.response['board']['earning']] == [str(index) for index in range(8)]
            assert len(handler.response['board']['research']) == 1
            assert handler.response['pick_feed']['paper_tiers_unlocked']
            handler._handle_curated_picks()
            assert handler.response['count'] == 8
            assert [row['match_id'] for row in handler.response['picks']] == [str(index) for index in range(8)]
        assert user == before
        assert store.get_telemetry('daily:board') == publication(rows)
    finally:
        store.close()


def test_signed_verification_unlocks_second_pick_before_auth_row_reconciliation(tmp_path):
    store = SqliteStorage(str(tmp_path / 'verified.db'))
    try:
        store.verify_user('authenticated-member')
        user = {'id': 'authenticated-member', 'tier': 'free', 'telegram_verified': False}
        handler = Handler(SimpleNamespace(storage=store), user)
        assert handler._feed_access()[1] is True
        assert Handler(SimpleNamespace(storage=store), None, '/api/dashboard?user_id=authenticated-member')._feed_access()[1] is False
    finally:
        store.close()


def test_operator_preview_is_read_only_and_applies_to_all_feed_routes(tmp_path, monkeypatch):
    monkeypatch.setenv('LISA_ADMIN_EMAILS', 'owner@example.test')
    store = SqliteStorage(str(tmp_path / 'preview.db'))
    try:
        store.set_telemetry('daily:board', publication([opportunity(str(index)) for index in range(150)]))
        store.set_telemetry('daily:status', {'state': 'ok'})
        service = DailyService(store, Settings())
        server = SimpleNamespace(storage=store, daily_service=service, settings=Settings())
        user = {'id': 'operator', 'tier': 'free', 'email': 'owner@example.test'}
        original = copy.deepcopy(user)
        for tier, count in [('free', 1), ('tier1', 5), ('tier2', 150), ('tier3', 150)]:
            handler = Handler(server, user, '/api/dashboard?tier=' + tier)
            handler._handle_dashboard()
            assert sum(not row['is_locked'] for row in handler.response['active_picks']) == count
            handler._handle_picks(urlparse('/api/picks?tier=' + tier))
            assert sum(not row['is_locked'] for row in handler.response['active_picks']) == count
            handler._handle_opportunity_board(urlparse('/api/opportunity-board?tier=' + tier))
            assert len(handler.response['board']['earning']) == count
            handler._handle_curated_picks()
            assert handler.response['count'] == count
        assert user == original
    finally:
        store.close()


def test_worker_retains_background_candidates_and_journals_headlines_beyond_ladder_caps(tmp_path):
    from unittest.mock import Mock
    def candidate(match, **changes):
        row = dict(match_id=match, sport_key='soccer_epl', kickoff=NOW + timedelta(hours=2), home=match,
            away='Away', market='btts', selection='Yes', p_model=.8, fair_odds=1.25,
            priced=True, best_odds=1.5, best_book='Test', ev=.2, price_updated_at=NOW)
        return Opportunity(**dict(row, **changes))
    headline = candidate('outside-ladders')
    background = candidate('research-only', p_model=.2, ev=-.7)
    ledger_forecast = candidate('old-ladder', priced=False, best_odds=None, ev=None)
    board = Board(NOW, 24, True, {}, winning=(ledger_forecast,), research=(headline, background, ledger_forecast))
    report = FeedReport(began=NOW, board=board, forecast={'matches': [], 'count': 0})
    store = SqliteStorage(str(tmp_path / 'worker.db'))
    try:
        service = DailyService(store, Settings(), runner=Mock(return_value=report), providers=ProviderSet())
        service.tick(now=NOW)
        saved = store.get_telemetry('daily:board')
        assert len(saved['board']['research']) == 3
        assert {row['match_id'] for row in store.list_pending_picks()} == {'outside-ladders', 'old-ladder'}
        selected, _ = publication_feed(service.read(now=NOW), Settings(), now=NOW)
        assert [row['match_id'] for row in selected] == ['outside-ladders']
        assert service.read(now=NOW)['price_readiness']['fixtures_priced'] == 2
        assert service.read(now=NOW)['price_readiness']['scope'] == 'published_model_candidates'
    finally:
        store.close()


def test_telegram_render_handles_unrated_paper_predictions_and_bounds_message_length():
    from lisa.telegram_bot import format_active_top_picks_contract
    selected, _ = feed([opportunity(str(index)) for index in range(50)])
    text, _ = format_active_top_picks_contract(selected, user_tier='tier3')
    assert 'No stake recommended' in text and 'Under 3.5' in text
    assert 'n/a/10.0' in text and len(text) < 4096
    assert 'full curated feed' in text
