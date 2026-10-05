"""Small source adapters; each knows only verified fields and fixed routes."""
import re
from urllib.parse import urlencode, urlsplit

from ..providers.base import ParseError
from .contracts import Batch, count, normalized

ESPN_LEAGUES = {
    'soccer_epl': 'eng.1', 'soccer_england_championship': 'eng.2',
    'soccer_germany_bundesliga': 'ger.1', 'soccer_germany_2_bundesliga': 'ger.2',
    'soccer_italy_serie_a': 'ita.1', 'soccer_spain_la_liga': 'esp.1',
    'soccer_france_ligue_one': 'fra.1', 'soccer_netherlands_eredivisie': 'ned.1',
    'soccer_portugal_primeira_liga': 'por.1', 'soccer_uefa_champions_league': 'uefa.champions',
    'soccer_scotland_premiership': 'sco.1', 'soccer_belgium_first_division': 'bel.1',
    'soccer_greece_superleague': 'gre.1', 'soccer_brazil_serie_a': 'bra.1',
}
STAT_MAP = {
    'wonCorners': 'corners', 'foulsCommitted': 'fouls', 'yellowCards': 'yellow_cards',
    'redCards': 'red_cards', 'totalShots': 'shots', 'shotsOnTarget': 'shots_on_target',
    'offsides': 'offsides', 'totalPasses': 'passes', 'totalTackles': 'tackles',
    'saves': 'saves', 'throwIns': 'throw_ins',
}


class EspnSource:
    name = 'espn'
    base = 'https://site.api.espn.com/apis/site/v2/sports/soccer'
    leagues = tuple(ESPN_LEAGUES)

    def scoreboard_url(self, league, start, end=None):
        dates = start.strftime('%Y%m%d')
        if end and end != start:
            raise ValueError('ESPN soccer scoreboard accepts one date per request')
        return f'{self.base}/{ESPN_LEAGUES[league]}/scoreboard?' + urlencode({'dates': dates, 'limit': 1000})

    def summary_url(self, league, event_id):
        if not isinstance(event_id, str) or not re.fullmatch(r'\d{1,15}', event_id):
            raise ParseError('espn: invalid event id', provider=self.name)
        return f'{self.base}/{ESPN_LEAGUES[league]}/summary?' + urlencode({'event': event_id})

    def _event(self, event, league, now):
        if not isinstance(event, dict) or not isinstance(event.get('competitions'), list) or len(event['competitions']) != 1:
            raise ParseError('espn: unexpected competition shape', provider=self.name)
        comp = event['competitions'][0]
        if not isinstance(comp, dict):
            raise ParseError('espn: invalid competition', provider=self.name)
        people = comp.get('competitors')
        if not isinstance(people, list) or len(people) != 2 or any(not isinstance(c, dict) for c in people):
            raise ParseError('espn: unexpected competitors', provider=self.name)
        sides = {c.get('homeAway'): c for c in people}
        if set(sides) != {'home', 'away'}:
            raise ParseError('espn: ambiguous home/away', provider=self.name)
        status = comp.get('status', event.get('status', {}))
        kind = status.get('type', {})
        name, state = kind.get('name', ''), kind.get('state')
        if name in ('STATUS_CANCELED', 'STATUS_CANCELLED'):
            normalized_status = 'CANCELED'
        elif name == 'STATUS_POSTPONED':
            normalized_status = 'POSTPONED'
        elif name in ('STATUS_SUSPENDED', 'STATUS_ABANDONED'):
            normalized_status = name.removeprefix('STATUS_')
        elif (kind.get('completed') is True and state == 'post' and
              (name in ('STATUS_FULL_TIME', 'STATUS_FINAL') or
               any(token in name for token in ('EXTRA', 'AET', 'PEN', 'SHOOTOUT')))):
            normalized_status = 'FINISHED'
        elif state == 'in':
            normalized_status = 'HT' if name == 'STATUS_HALFTIME' else 'LIVE'
        elif state == 'pre' and name in ('STATUS_SCHEDULED', 'STATUS_TIMED'):
            normalized_status = 'SCHEDULED'
        else:
            normalized_status = 'UNKNOWN'
        eid = str(event.get('id', comp.get('id', '')))
        if not re.fullmatch(r'\d{1,15}', eid) or str(comp.get('id')) != eid:
            raise ParseError('espn: inconsistent event identity', provider=self.name)
        ended_extra = (name in ('STATUS_END_OF_EXTRA_TIME', 'STATUS_FINAL_PEN', 'STATUS_FINAL_AET')
                       or 'PEN' in name or 'EXTRA' in name or 'AET' in name or 'SHOOTOUT' in name
                       or status.get('period', 0) in (3, 4, 5))
        raw = dict(event_id=eid, sport_key=league,
            kickoff=comp.get('date', event.get('date')),
            home_team=sides['home'].get('team', {}).get('displayName'),
            away_team=sides['away'].get('team', {}).get('displayName'),
            status=normalized_status, score_period='regulation', statistics_period='regulation')
        for side, competitor in sides.items():
            if normalized_status in ('FINISHED', 'LIVE', 'HT') and competitor.get('score') is not None:
                score = competitor['score']
                if isinstance(score, dict):
                    score = score.get('displayValue', score.get('value'))
                raw[side + '_score'] = count(score, 30)
            periods = competitor.get('linescores', [])
            if not isinstance(periods, list):
                raise ParseError('espn: malformed period scores', provider=self.name)
            if periods:
                # ESPN's inspected soccer response uses ordered period entries
                # without a period number. Explicit numbers, when present, must
                # agree; shootout entries are never counted in regulation.
                def period_score(index):
                    p = periods[index]
                    if not isinstance(p, dict) or p.get('period', index + 1) != index + 1:
                        raise ParseError('espn: ambiguous period scores', provider=self.name)
                    return count(p.get('displayValue', p.get('value')), 30)
                if normalized_status == 'FINISHED' or normalized_status == 'HT' or status.get('period', 0) >= 2:
                    raw[side + '_first_half_score'] = period_score(0)
                if ended_extra and len(periods) >= 2:
                    raw[side + '_score'] = period_score(0) + period_score(1)
                elif ended_extra:
                    raw['score_period'] = 'including_extra_time'
            elif ended_extra:
                raw['score_period'] = 'including_extra_time'
            statistics = competitor.get('statistics', [])
            if not isinstance(statistics, list):
                raise ParseError('espn: malformed statistics', provider=self.name)
            for stat in statistics:
                if not isinstance(stat, dict):
                    raise ParseError('espn: malformed statistic', provider=self.name)
                field = STAT_MAP.get(stat.get('name'))
                if field and stat.get('displayValue') not in (None, '', '--'):
                    raw[side + '_' + field] = stat['displayValue']
        if ended_extra:
            raw['statistics_period'] = 'including_extra_time'
        row = normalized({'schema_version': 1, 'collected_at': now.isoformat(), 'fixtures': [raw]}, self.name, now=now).fixtures[0]
        row['source_team_ids'] = {side: str(c.get('team', {}).get('id', '')) for side, c in sides.items()}
        if event.get('timeValid', comp.get('timeValid')) is False or normalized_status == 'UNKNOWN':
            row.update(kickoff_time_known=False, discovery_only=True, settlement_eligible=False)
        return row

    def parse_scoreboard(self, payload, league, *, now):
        if (not isinstance(payload, dict) or not isinstance(payload.get('events'), list)
                or len(payload['events']) >= 1000 or not isinstance(payload.get('leagues'), list)
                or not any(isinstance(v, dict) and v.get('slug') == ESPN_LEAGUES[league] for v in payload['leagues'])):
            raise ParseError('espn: missing/wrong league or truncated scoreboard', provider=self.name)
        rows = {}
        for event in payload['events']:
            row = self._event(event, league, now)
            identity = row['source_event_id']
            if identity in rows and rows[identity] != row:
                raise ParseError('espn: conflicting event duplicates', provider=self.name)
            rows[identity] = row
        return Batch(self.name, tuple(rows.values()))

    def parse_summary(self, payload, league, event_id, *, now):
        if not isinstance(payload, dict) or not isinstance(payload.get('header'), dict):
            raise ParseError('espn: missing summary header', provider=self.name)
        header = payload['header']
        if str(header.get('id')) != event_id:
            raise ParseError('espn: summary event mismatch', provider=self.name)
        row = self._event(header, league, now)
        teams = payload.get('boxscore', {}).get('teams', [])
        if not isinstance(teams, list) or len(teams) > 2:
            raise ParseError('espn: invalid boxscore teams', provider=self.name)
        seen = set()
        for team in teams:
            sid = str(team.get('team', {}).get('id', ''))
            matching = [side for side, eid in row['source_team_ids'].items() if sid == eid]
            if len(matching) != 1 or sid in seen:
                raise ParseError('espn: boxscore team mismatch', provider=self.name)
            seen.add(sid)
            side = matching[0]
            for stat in team.get('statistics', []):
                field = STAT_MAP.get(stat.get('name'))
                if field and stat.get('displayValue') not in (None, '', '--'):
                    from .contracts import STATISTICS
                    row[side + '_' + field] = count(stat['displayValue'], STATISTICS[field])
        if row.get('ended_after_extra_time'):
            row.pop('home_corners', None)
            row.pop('away_corners', None)
        if isinstance(payload.get('rosters'), list):
            row['source_player_statistics'] = payload['rosters']
        row['statistics_observed_at'] = now.isoformat()
        return Batch(self.name, (row,))


class SportyBetSource:
    """Optional undocumented Nigeria feed; a 403 opens a persistent circuit.

    Only exact known regulation markets are mapped. Every other market keeps
    its source ID and specifiers in the raw snapshot for later adapter work.
    No timestamp is manufactured from retrieval time.
    """
    name = 'sportybet'
    leagues = ()  # prices join to the calendars rather than asserting coverage
    url = ('https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?'
           'sportId=sr%3Asport%3A1&marketId=1%2C10%2C16%2C18%2C29&pageSize=100&todayGames=false&timeline=168')
    # Explicit geography + league names. No broad substring matching, youth,
    # women's, reserves, virtual events or "Premier League" from another nation.
    competitions = {
        ('England', 'Premier League'): 'soccer_epl',
        ('England', 'Championship'): 'soccer_england_championship',
        ('Germany', 'Bundesliga'): 'soccer_germany_bundesliga',
        ('Germany', '2. Bundesliga'): 'soccer_germany_2_bundesliga',
        ('Italy', 'Serie A'): 'soccer_italy_serie_a',
        ('Spain', 'LaLiga'): 'soccer_spain_la_liga',
        ('France', 'Ligue 1'): 'soccer_france_ligue_one',
        ('Netherlands', 'Eredivisie'): 'soccer_netherlands_eredivisie',
        ('Portugal', 'Liga Portugal'): 'soccer_portugal_primeira_liga',
        ('International Clubs', 'UEFA Champions League'): 'soccer_uefa_champions_league',
    }

    def parse(self, payload, *, now):
        if not isinstance(payload, dict) or payload.get('bizCode') != 10000 or not isinstance(payload.get('data'), dict):
            raise ParseError('sportybet: unsuccessful or changed response', provider=self.name)
        data = payload['data']
        tournaments = data.get('tournaments')
        if not isinstance(tournaments, list):
            raise ParseError('sportybet: missing tournaments', provider=self.name)
        rows, quotes, replacements, unknown = [], [], [], 0
        for tournament in tournaments:
            league = self.competitions.get((tournament.get('categoryName'), tournament.get('name')))
            if not league:
                continue
            for event in tournament.get('events', []):
                eid = event.get('eventId')
                from datetime import datetime, timezone
                from .contracts import number
                kickoff = datetime.fromtimestamp(number(event.get('estimateStartTime'),
                    minimum=946684800000, maximum=4102444800000) / 1000, timezone.utc)
                # The upcoming endpoint does not establish authoritative scores.
                rows.append(dict(event_id=eid, sport_key=league, kickoff=kickoff.isoformat(),
                    home_team=event.get('homeTeamName'), away_team=event.get('awayTeamName'),
                    status='UNKNOWN'))
                replacements.append(dict(event_id=eid, book_key='sportybet'))
                for market in event.get('markets', []):
                    mid = str(market.get('id'))
                    options = {'1': ('h2h', {'1': 'Home', '2': 'Draw', '3': 'Away'}),
                               '10': ('double_chance', {'9': '1X', '10': '12', '11': 'X2'}),
                               '29': ('btts', {'74': 'Yes', '76': 'No'}),
                               '18': ('totals', {'12': 'Over', '13': 'Under'}),
                               '16': ('asian_handicap', {'1714': 'Home', '1715': 'Away'})}
                    if mid not in options:
                        unknown += 1
                        continue
                    family, selections = options[mid]
                    spec = market.get('specifier') or ''
                    line = None
                    if mid in ('16', '18'):
                        wanted = 'hcp' if mid == '16' else 'total'
                        matched = re.fullmatch(wanted + r'=(-?\d+(?:\.\d+)?)', spec)
                        if not matched:
                            unknown += 1
                            continue
                        line = float(matched[1])
                    elif spec:
                        unknown += 1
                        continue
                    for outcome in market.get('outcomes', []):
                        selection = selections.get(str(outcome.get('id')))
                        if selection is None:
                            unknown += 1
                            continue
                        quotes.append(dict(event_id=eid, book_key='sportybet', book_title='SportyBet',
                            market=family, selection=selection, line=line, period='regulation',
                            settlement_contract='regulation', odds=outcome.get('odds'),
                            active=market.get('status') == 0 and outcome.get('isActive') == 1,
                            # Field semantics not verified live. Keep price
                            # update time unknown; source raw timestamp retained.
                            updated_at=None))
        batch = normalized(dict(schema_version=1, collected_at=now.isoformat(), fixtures=rows, quotes=quotes,
                                replace_books=replacements), self.name, now=now)
        # Bookmaker schedules are discovery only until a verified calendar joins.
        for row in batch.fixtures:
            row.update(discovery_only=True, settlement_eligible=False)
        warnings = ('SportyBet update timestamps are unverified; quotes are research only.',)
        if unknown:
            warnings += (f'{unknown} unsupported source markets/outcomes retained in raw snapshot.',)
        return Batch(batch.source, batch.fixtures, batch.quotes, batch.replace_books, warnings)


class JsonSource:
    """An explicitly configured, authorized version-1 JSON feed."""
    def __init__(self, name, url):
        from .contracts import source_name
        self.name = source_name(name)
        if self.name in ('espn', 'sportybet', 'openfootball', 'scalper', 'pinnacle_browser', 'sportybet_browser'):
            raise ValueError('Custom feeds must use a distinct source name')
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError('Custom feeds require an explicit HTTPS URL without userinfo or fragment')
        self.url = url

    def parse(self, payload, *, now):
        return normalized(payload, self.name, now=now)


class OpenFootballSource:
    """Reuse the vetted CC0 parser; preserve its unknown-time boundary."""
    name = 'openfootball'

    def __init__(self):
        from ..providers.openfootball import FILES
        self.leagues = tuple(FILES)

    def url(self, league, year, *, now):
        from ..providers.openfootball import BASE_URL, OpenFootballProvider
        provider = OpenFootballProvider(clock=lambda: now)
        return BASE_URL + '/' + provider._file(league, year)

    def parse(self, payload, league, year, *, now):
        from ..providers.openfootball import OpenFootballProvider
        rows = OpenFootballProvider(clock=lambda: now).parse(payload, league, year)
        for row in rows:
            row.update(source_event_id=row['match_id'].split(':', 1)[1], observed_at=now.isoformat(),
                       source_tier='community')
        return Batch(self.name, tuple(rows))
