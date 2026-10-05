"""Parsers for public responses observed in the bookmaker's own browser page.

No credentials, request headers, or arbitrary URLs enter the interchange. HTTP
Date/Age establish snapshot age, never the time at which an individual price
changed. Only these verified adapters can produce publisher confirmations.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
from urllib.parse import urlsplit

from .contracts import Batch, count, fail, label, normalized, number, timestamp
from .sources import SportyBetSource

PUBLIC_HEADERS = ('date', 'age', 'cache-control', 'last-modified', 'content-type')


def publisher_confirmation(headers, received):
    """Conservative response age (RFC 9111 §4.2.3), including intermediary age."""
    try:
        date = parsedate_to_datetime(headers['date'])
        if date.tzinfo is None or date > received + timedelta(seconds=60):
            return None
        age = headers.get('age', '0')
        if not isinstance(age, str) or not re.fullmatch(r'\d{1,10}', age):
            return None
        seconds = max(0., (received - date).total_seconds(), int(age))
        confirmed = received - timedelta(seconds=seconds)
        return confirmed if confirmed.year >= 2000 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def response(capture, role, now):
    if not isinstance(capture, dict) or capture.get('capture_version') != 1:
        fail('unsupported browser capture')
    raw = capture.get('responses', {}).get(role)
    if not isinstance(raw, dict) or not isinstance(raw.get('headers'), dict):
        fail('missing browser response')
    received = timestamp(raw.get('received_at'))
    if received > now + timedelta(seconds=60):
        fail('browser capture is in the future')
    return raw.get('payload'), received, publisher_confirmation(raw['headers'], received)


def attest(batch, confirmed, *, metadata=None):
    for quote in batch.quotes:
        quote.update(confirmed_at=confirmed.isoformat() if confirmed else None,
                     freshness_basis='publisher_snapshot')
        if metadata:
            quote.update(metadata.get((quote['event_id'], quote['market'],
                                       quote['selection'], quote['line'], quote['period']), {}))
    return batch


class PinnacleBrowserSource:
    name, book = 'pinnacle_browser', 'pinnacle'
    # Source IDs and route verified through the public page, not inferred from
    # another league's name. Expand only after observing that page's responses.
    leagues = ('soccer_epl',)
    roles = ('events', 'markets')
    url = 'https://www.pinnacle.com/en/soccer/england-premier-league/matchups/'

    def role(self, url):
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname != 'guest.api.arcadia.pinnacle.com':
            return None
        return {'/0.1/leagues/1980/matchups': 'events',
                '/0.1/leagues/1980/markets/straight': 'markets'}.get(parts.path)

    def parse(self, capture, *, now):
        events, received, calendar_time = response(capture, 'events', now)
        markets, price_received, confirmed = response(capture, 'markets', now)
        if (not isinstance(events, list) or not isinstance(markets, list)
                or len(events) >= 5000 or len(markets) >= 20000):
            fail('invalid or oversized Pinnacle league snapshot')
        if abs((received - price_received).total_seconds()) > 30:
            fail('Pinnacle snapshot parts are too far apart')
        rows, roots, quotes, metadata = [], {}, [], {}
        skipped = 0
        for event in events:
            if not isinstance(event, dict):
                fail('invalid Pinnacle event')
            # Props, virtuals, corners and parent-derived specials cannot become
            # a regulation goal match simply because two labels look familiar.
            if event.get('type') != 'matchup' or event.get('parentId') is not None:
                skipped += 1
                continue
            league = event.get('league', {})
            if (league.get('id') != 1980 or league.get('name') != 'England - Premier League'
                    or league.get('sport', {}).get('id') != 29 or event.get('units') != 'Regular'):
                fail('unexpected Pinnacle competition or units')
            eid = str(count(event.get('id'), 10**12))
            people = event.get('participants')
            if not isinstance(people, list) or len(people) != 2 or any(not isinstance(p, dict) for p in people):
                fail('invalid Pinnacle participants')
            sides = {p.get('alignment'): p for p in people}
            if set(sides) != {'home', 'away'}:
                fail('ambiguous Pinnacle orientation')
            kickoff = timestamp(event.get('startTime'))
            scheduled = event.get('status') == 'pending' and event.get('isLive') is False and kickoff > received
            row = dict(event_id=eid, sport_key='soccer_epl', kickoff=kickoff.isoformat(),
                       home_team=sides['home'].get('name'), away_team=sides['away'].get('name'),
                       status='SCHEDULED' if scheduled else 'UNKNOWN')
            if eid in roots:
                fail('duplicate Pinnacle match identity')
            periods = event.get('periods')
            if not isinstance(periods, list) or any(not isinstance(p, dict) for p in periods):
                fail('invalid Pinnacle periods')
            roots[eid] = (event, {p.get('period'): p for p in periods})
            rows.append(row)
        for market in markets:
            if not isinstance(market, dict):
                fail('invalid Pinnacle market')
            eid = str(market.get('matchupId'))
            if eid not in roots:
                continue
            event, periods = roots[eid]
            kind, period = market.get('type'), market.get('period')
            if type(period) is not int or period not in (0, 1):
                skipped += 1
                continue
            family = {'moneyline': 'h2h', 'spread': 'asian_handicap', 'total': 'totals',
                      'team_total': {'home': 'home_team_totals', 'away': 'away_team_totals'}.get(market.get('side'))}.get(kind)
            if family is None:
                skipped += 1
                continue
            prices = market.get('prices')
            # Closed markets can legally omit their price array. The complete
            # event snapshot still revokes prior offers; raw evidence is kept.
            offered = (event.get('status') == 'pending' and event.get('isLive') is False
                       and event.get('hasMarkets') is True and market.get('status') == 'open'
                       and periods.get(period, {}).get('status') == 'open')
            if not offered and not prices:
                skipped += 1
                continue
            if not isinstance(prices, list) or any(not isinstance(p, dict) for p in prices):
                fail('invalid Pinnacle prices')
            sides = [p.get('designation') for p in prices]
            expected = {'home', 'away', 'draw'} if kind == 'moneyline' else {'home', 'away'} if kind == 'spread' else {'over', 'under'}
            if len(sides) != len(expected) or set(sides) != expected:
                if not offered:
                    skipped += 1
                    continue
                fail('incomplete or ambiguous Pinnacle market')
            active = offered and timestamp(market.get('cutoffAt')) > price_received
            points = [number(p.get('points'), minimum=-100, maximum=100) for p in prices] if kind != 'moneyline' else []
            if points and (abs(points[0] + points[1]) > 1e-8 if kind == 'spread' else points[0] != points[1]):
                fail('inconsistent Pinnacle market lines')
            for index, price in enumerate(prices):
                selection = price['designation'].capitalize()
                line = points[index] if points else None
                american = number(price.get('price'), minimum=-1000000, maximum=1000000) if active else None
                if american is not None and abs(american) < 100:
                    fail('invalid Pinnacle American odds')
                decimal = (1 + american / 100 if american > 0 else 1 + 100 / -american) if active else None
                scope = 'regulation' if period == 0 else 'first_half'
                quotes.append(dict(event_id=eid, book_key=self.book, book_title='Pinnacle', market=family,
                    selection=selection, line=line, period=scope, settlement_contract=scope,
                    active=active, odds=decimal, updated_at=None))
                metadata[(eid, family, selection, line, scope)] = dict(
                    publisher_market_key=label(market.get('key')),
                    publisher_version=count(market.get('version'), 10**15))
        batch = normalized(dict(schema_version=1, collected_at=price_received.isoformat(), fixtures=rows,
            quotes=quotes, replace_books=[dict(event_id=eid, book_key=self.book) for eid in roots]), self.name, now=now)
        for fixture in batch.fixtures:
            fixture['calendar_confirmed_at'] = calendar_time.isoformat() if calendar_time else None
            if fixture['status'] == 'UNKNOWN':
                fixture['discovery_only'] = True
        return attest(Batch(batch.source, batch.fixtures, batch.quotes, batch.replace_books,
                            (f'{skipped} unsupported events/markets retained in raw capture.',)), confirmed, metadata=metadata)


class SportyBetBrowserSource:
    name, book = 'sportybet_browser', 'sportybet'
    leagues = tuple(SportyBetSource.competitions.values())
    roles = ('events',)
    url = 'https://www.sportybet.com/ng/sport/football'

    def role(self, url):
        parts = urlsplit(url)
        return 'events' if parts.scheme == 'https' and parts.hostname == 'www.sportybet.com' and parts.path == '/api/ng/factsCenter/pcUpcomingEvents' else None

    def parse(self, capture, *, now):
        payload, received, confirmed = response(capture, 'events', now)
        if (not isinstance(payload, dict) or payload.get('bizCode') != 10000
                or not isinstance(payload.get('data'), dict)
                or not isinstance(payload['data'].get('tournaments'), list)):
            fail('invalid SportyBet upcoming snapshot')
        rows, quotes, metadata, skipped = [], [], {}, 0
        for tournament in payload['data']['tournaments']:
            if not isinstance(tournament, dict):
                fail('invalid SportyBet tournament')
            league = SportyBetSource.competitions.get((tournament.get('categoryName'), tournament.get('name')))
            if not league:
                continue
            events = tournament.get('events')
            if not isinstance(events, list):
                fail('invalid SportyBet events')
            for event in events:
                if not isinstance(event, dict):
                    fail('invalid SportyBet event')
                eid = label(event.get('eventId'))
                if not re.fullmatch(r'sr:match:\d{1,15}', eid):
                    fail('invalid SportyBet match identity')
                sport = event.get('sport', {})
                category = sport.get('category', {})
                if (sport.get('id') != 'sr:sport:1' or category.get('name') != tournament.get('categoryName')
                        or category.get('tournament', {}).get('id') != tournament.get('id')):
                    fail('inconsistent SportyBet competition')
                kickoff = datetime.fromtimestamp(number(event.get('estimateStartTime'),
                    minimum=946684800000, maximum=4102444800000) / 1000, timezone.utc)
                scheduled = type(event.get('status')) is int and event['status'] == 0 and event.get('matchStatus') == 'Not start' and kickoff > received
                rows.append(dict(event_id=eid, sport_key=league, kickoff=kickoff.isoformat(),
                    home_team=event.get('homeTeamName'), away_team=event.get('awayTeamName'),
                    status='SCHEDULED' if scheduled else 'UNKNOWN'))
                markets = event.get('markets')
                if not isinstance(markets, list):
                    fail('invalid SportyBet markets')
                for market in markets:
                    if not isinstance(market, dict):
                        fail('invalid SportyBet market')
                    mid = str(market.get('id'))
                    spec = market.get('specifier') or ''
                    mapping = {'1': ('h2h', {'1': 'Home', '2': 'Draw', '3': 'Away'}),
                        '10': ('double_chance', {'9': '1X', '10': '12', '11': 'X2'}),
                        '11': ('draw_no_bet', {'4': 'Home', '5': 'Away'}),
                        '29': ('btts', {'74': 'Yes', '76': 'No'}),
                        '18': ('totals', {'12': 'Over', '13': 'Under'})}.get(mid)
                    line = None
                    if mid == '18':
                        matched = re.fullmatch(r'total=(\d+(?:\.\d+)?)', spec)
                        if not matched:
                            fail('invalid SportyBet total specifier')
                        line = number(matched[1], minimum=0, maximum=100)
                    elif spec:
                        mapping = None
                    if mapping is None:
                        skipped += 1
                        continue
                    family, selections = mapping
                    outcomes = market.get('outcomes')
                    offered = (scheduled and type(market.get('status')) is int and market['status'] == 0
                               and market.get('banned') is False)
                    if not offered and not outcomes:
                        skipped += 1
                        continue
                    if not isinstance(outcomes, list) or any(not isinstance(o, dict) for o in outcomes):
                        fail('invalid SportyBet outcomes')
                    if len(outcomes) != len(selections) or {str(o.get('id')) for o in outcomes} != set(selections):
                        if not offered:
                            skipped += 1
                            continue
                        fail('incomplete SportyBet market')
                    changed = market.get('lastOddsChangeTime')
                    changed = datetime.fromtimestamp(number(changed, minimum=946684800000,
                        maximum=4102444800000) / 1000, timezone.utc).isoformat() if changed is not None else None
                    for outcome in outcomes:
                        selection = selections[str(outcome['id'])]
                        active = offered and type(outcome.get('isActive')) is int and outcome['isActive'] == 1
                        quotes.append(dict(event_id=eid, book_key=self.book, book_title='SportyBet',
                            market=family, selection=selection, line=line, period='regulation',
                            settlement_contract='regulation', active=active, odds=outcome.get('odds'), updated_at=changed))
                        metadata[(eid, family, selection, line, 'regulation')] = dict(
                            publisher_market_key=mid + ':' + spec)
        batch = normalized(dict(schema_version=1, collected_at=received.isoformat(), fixtures=rows, quotes=quotes,
            replace_books=[dict(event_id=row['event_id'], book_key=self.book) for row in rows]), self.name, now=now)
        for fixture in batch.fixtures:
            fixture['calendar_confirmed_at'] = confirmed.isoformat() if confirmed else None
        # The landing page is a partial selection, not its advertised totalNum.
        return attest(Batch(batch.source, batch.fixtures, batch.quotes, batch.replace_books,
            ('Landing-page coverage is partial; no complete-league coverage claimed.',
             f'{skipped} unsupported markets retained in raw capture.')), confirmed, metadata=metadata)


BROWSER_SOURCES = {'pinnacle': PinnacleBrowserSource, 'sportybet': SportyBetBrowserSource}
