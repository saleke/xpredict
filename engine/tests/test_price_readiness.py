"""Clock, suspension and publication regressions; all prices are artificial."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from lisa import config, feed
from lisa.board import Accumulator, Board, BoardCoverage, Fixture, MarketPrice, OpportunityBoard
from lisa.daily_service import BOARD_KEY, DailyService
from lisa.model_policy import configuration_hash
from lisa.providers.scalper import ScalperProvider
from lisa.scalper.browser_sources import SportyBetBrowserSource
from lisa.scalper.repository import ScalperRepository
from lisa.storage import SqliteStorage
from test_daily_service_unittest import NOW, opportunity
from test_scalper_browser import sporty


@pytest.fixture
def published(tmp_path):
    store = SqliteStorage(str(tmp_path / 'prices.db'))
    repo = ScalperRepository(store)
    def save(*, now=NOW, price='1.95', active=1):
        capture = sporty(now=now, kickoff=NOW + timedelta(hours=2),
                         changed=NOW - timedelta(days=1), price=price, active=active)
        batch = SportyBetBrowserSource().parse(capture, now=now)
        repo.accept(batch, resource='browser:landing', payload=capture, now=now, ttl=0)
    save()
    provider = ScalperProvider(leagues=('soccer_epl',), clock=lambda: NOW)
    provider.storage = store
    fixture = Fixture('canonical-fixture', 'soccer_epl', NOW + timedelta(hours=2), 'Arsenal', 'Chelsea')
    prices = provider.match(provider.fetch(sport_keys=('soccer_epl',), now=NOW).quotes, [fixture]).prices
    quote = next(q for q in prices[fixture.match_id] if q.selection == 'Over')
    model = Mock(spec=['strength'])
    model.strength.return_value.games = 100
    row = OpportunityBoard(model)._opportunity(fixture, 'totals', 'Over', 2.5, .7, quote)
    row = replace(row, stake_fraction=.02)
    acc = Accumulator((row, replace(row, match_id='second-fixture')), .49, .45, 1/.45,
                      best_odds=3.8, best_book='Artificial book', ev=.2, stake_fraction=.01, priced=True)
    board = Board(NOW, 24, False, {}, winning=(row,), earning=(row,), micro_bets=(row,),
                  accumulators=(acc,), coverage=BoardCoverage(24, 1, 1, 1, False, 12))
    report = feed.FeedReport(began=NOW, board=board,
        forecast={'matches': [{'match_id': fixture.match_id, 'commence_at': fixture.kickoff.isoformat()}], 'count': 1})
    store.set_telemetry(BOARD_KEY, dict(feed_report=1, **{
        'generated_at': NOW.isoformat(), 'board': board.to_dict(), 'forecast': report.forecast}))
    settings = config.Settings(board_leagues=('soccer_epl',), enable_sharpapi=False, enable_sportsdb=False)
    service = DailyService(store, settings, runner=Mock(return_value=report), providers=feed.ProviderSet())
    yield store, repo, service, save, report
    store.close()


def test_native_price_deadline_and_local_identity_survive_publication(published):
    store, repo, service, save, report = published
    row = service.read(now=NOW)['board']['winning'][0]
    assert row['priced']
    assert row['price_book_key'] == 'sportybet'
    assert row['price_source_event_id'] == 'sr:match:123'
    assert len(row['price_quote_identity']) == 64
    assert row['price_valid_until'] == (NOW + timedelta(seconds=300)).isoformat()


def test_read_expiry_removes_financial_claims_preserves_forecasts_and_saved_snapshot(published):
    store, repo, service, save, report = published
    original = deepcopy(store.get_telemetry(BOARD_KEY))
    assert service.read(now=NOW + timedelta(seconds=299))['board']['earning']
    value = service.read(now=NOW + timedelta(seconds=300))
    row = value['board']['winning'][0]
    assert row['price_state'] == 'expired'
    assert not row['priced'] and row['ev'] is None and row['best_odds'] is None
    assert row['stake_fraction'] == 0
    assert not value['board']['earning']
    assert value['forecast']['count'] == 1
    assert value['board']['coverage']['fixtures_priced'] == 0
    assert value['board']['coverage']['fixtures_priced_at_generation'] == 1
    acc = value['board']['accumulators'][0]
    assert not acc['priced'] and acc['best_odds'] is None and acc['ev'] is None
    assert acc['stake_fraction'] == 0
    assert store.get_telemetry(BOARD_KEY) == original


def test_unchanged_new_capture_does_not_renew_a_published_deadline(published):
    store, repo, service, save, report = published
    save(now=NOW + timedelta(seconds=240))
    assert not service.read(now=NOW + timedelta(seconds=301))['board']['earning']


@pytest.mark.parametrize('active,price,state', [(0, '1.95', 'suspended'), (1, '2.05', 'price_changed')])
def test_new_capture_invalidates_an_old_offer_immediately(published, active, price, state):
    store, repo, service, save, report = published
    save(now=NOW + timedelta(seconds=1), active=active, price=price)
    value = service.read(now=NOW + timedelta(seconds=2))
    assert value['board']['winning'][0]['price_state'] == state
    assert not value['board']['earning']
    assert not value['price_readiness']['ready']


def test_removed_offer_is_unpriced_before_the_publication_deadline(published):
    store, repo, service, save, report = published
    with store._tx() as conn:
        conn.execute('DELETE FROM scalper_quotes WHERE source=?', ('sportybet_browser',))
    value = service.read(now=NOW + timedelta(seconds=1))
    assert value['board']['winning'][0]['price_state'] == 'removed'
    assert value['price_readiness']['fresh_earning_selections'] == 0


def test_source_failure_grace_expires_separately_from_board_readiness(published):
    store, repo, service, save, report = published
    repo.failure('sportybet_browser', 'browser:landing', NOW, 600, 'transport_error', host=True)
    value = service.read(now=NOW + timedelta(seconds=29))
    assert value['price_readiness']['ready']
    assert value['price_readiness']['collection_state'] == 'degraded'
    value = service.read(now=NOW + timedelta(seconds=31))
    assert value['board']['winning'][0]['price_state'] == 'disconnected'
    assert value['price_readiness']['collection_state'] == 'unavailable'
    assert not value['board']['earning']


def test_unavailable_local_offer_check_fails_closed_without_migrations(published, monkeypatch):
    store, repo, service, save, report = published
    from lisa import price_readiness
    monkeypatch.setattr(price_readiness, '_current_offers', lambda *args: ({}, True))
    monkeypatch.setattr(ScalperRepository, '__init__', lambda *args: pytest.fail('read must not migrate'))
    value = service.read(now=NOW)
    assert value['board']['winning'][0]['price_state'] == 'offer_state_unavailable'
    service.runner.assert_not_called()


@pytest.mark.parametrize('updates,state', [
    ({'price_confirmed_at': None}, 'unverified_timestamp'),
    ({'price_confirmed_at': (NOW + timedelta(minutes=2)).isoformat()}, 'unverified_timestamp'),
    ({'price_valid_until': '2026-10-05T10:00:00'}, 'unverified_expiry'),
    ({'price_quote_identity': None}, 'unverified_offer'),
    ({'best_odds': 1.17}, 'below_offer_floor'),
])
def test_incomplete_or_invalid_price_metadata_cannot_claim_execution(published, updates, state):
    store, repo, service, save, report = published
    value = store.get_telemetry(BOARD_KEY)
    for name in ('winning', 'earning', 'micro_bets'):
        for row in value['board'][name]:
            row.update(updates)
    store.set_telemetry(BOARD_KEY, value)
    projected = service.read(now=NOW)
    assert projected['board']['winning'][0]['price_state'] == state
    assert not projected['board']['earning']


def test_publication_cannot_save_an_expired_price_as_a_recommendation(published):
    store, repo, service, save, report = published
    now = NOW + timedelta(seconds=301)
    assert service._claim(now)
    service._publish(report, now)
    rows = store.list_pending_picks()
    assert rows
    assert all(row['best_odds'] is None and row['best_ev'] is None for row in rows)
    assert all(not row['is_recommendation'] and row['recommended_stake_pct'] == 0 for row in rows)
    assert not store.get_telemetry(BOARD_KEY)['price_readiness']['ready']


def test_worker_heartbeat_does_not_claim_current_prices(published):
    store, repo, service, save, report = published
    store.set_telemetry('daily:status', {'error': None})
    status = service.status(now=NOW + timedelta(seconds=301))
    assert status['ready']
    assert not status['price_ready']
    assert status['price_readiness']['state'] == 'expired'


def test_offer_floor_does_not_exclude_high_probability_in_any_ladder():
    row = opportunity(p_model=.9, fair_odds=1/.9, best_odds=1.22, ev=.098)
    board = OpportunityBoard(None, min_fair_odds=10)
    assert board._winning_ladder([row]) == (row,)
    assert board._earning_ladder([row]) == (row,)
    micro = replace(row, market='btts', selection='Yes')
    assert board._micro_bets([micro], set()) == (micro,)
    assert not board._earning_ladder([replace(row, best_odds=1.17)])


def test_high_probability_staking_keeps_the_evidence_gate():
    model = Mock(spec=['strength'])
    model.strength.return_value.games = 100
    fixture = Fixture('test', 'soccer_epl', NOW + timedelta(hours=2), 'Arsenal', 'Chelsea')
    quote = MarketPrice('test', 'Home', 1.22, 'testbook', updated_at=NOW)
    board = OpportunityBoard(model, min_fair_odds=10)
    assert board._opportunity(fixture, 'h2h', 'Home', None, .9, quote).stake_fraction == 0
    gate = Mock()
    gate.margin.return_value = .01
    approved = OpportunityBoard(model, evidence_gate=gate, min_fair_odds=10)
    assert approved._opportunity(fixture, 'h2h', 'Home', None, .9, quote).stake_fraction > 0


def test_price_policy_changes_invalidate_previous_validation_fingerprint():
    settings = config.Settings()
    assert configuration_hash(settings) != configuration_hash(replace(settings, board_min_offer_odds=1.25))
    assert configuration_hash(settings) == configuration_hash(replace(settings, board_min_fair_odds=10))


def test_multi_source_price_diagnostics_sum_quotes_and_count_shared_fixtures_once():
    from lisa.providers.sharpapi import SharpQuote, SharpSnapshot, MatchOutcome
    fixture = Fixture('test', 'soccer_epl', NOW + timedelta(hours=2), 'Arsenal', 'Chelsea')
    sources = []
    for name in ('first', 'second'):
        source = Mock(spec=['name', 'is_available', 'fetch', 'match'])
        source.name = name
        source.is_available.return_value = True
        source.fetch.return_value = SharpSnapshot(quotes=(SharpQuote('event', 'h2h', 'Home', 2., name),), rows=1)
        price = MarketPrice('test', 'Home', 2., name, source=name, updated_at=NOW)
        source.match.return_value = MatchOutcome({'test': (price,)}, 1, 1, int(name == 'first'), 0)
        sources.append(source)
    report = feed.FeedReport(began=NOW)
    prices = feed.fetch_prices(feed.ProviderSet(prices=sources), [fixture], config.Settings(),
                               report, window=24, now=NOW)
    assert len(prices['test']) == 2
    assert report.prices['quotes'] == 2
    assert report.price_match['matched_fixtures'] == 1
    assert report.price_match['matched_events'] == 2
    assert report.price_match['unmatched_events'] == 1
    assert set(report.price_match['sources']) == {'first', 'second'}
