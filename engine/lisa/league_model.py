"""Independent league fits: disconnected leagues never share a strength scale."""
import hashlib
import json
import math
from dataclasses import asdict
from collections import defaultdict
from .dixon_coles import DixonColesModel, FitReport, ScoredMatch, RHO_BOUNDS


class LeagueGoalModel:
    def __init__(self, **parameters):
        self.parameters = parameters
        self.models = {}
        self.report = None
        self.cache_storage = None

    def _restore(self, league, fingerprint, model):
        if self.cache_storage is None:
            return False
        data = self.cache_storage.get_telemetry('model:league_fit:'+league)
        if not isinstance(data,dict) or data.get('fingerprint') != fingerprint:
            return False
        try:
            attack, defence, games = data['attack'],data['defence'],data['games']
            if not all(isinstance(v,dict) for v in (attack,defence,games)) or not 1 <= len(attack) <= 1000:
                return False
            if set(attack) != set(defence) or set(attack) != set(games):
                return False
            if any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>30
                   for table in (attack,defence) for v in table.values()):
                return False
            if any(type(v) is not int or not 0 <= v <= 100000 for v in games.values()):
                return False
            values = [data[k] for k in ('base_mu','home_adv','rho')]
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
                return False
            if not .2 <= values[0] <= 4 or abs(values[1])>5 or not RHO_BOUNDS[0]<=values[2]<=RHO_BOUNDS[1]:
                return False
            report = FitReport(**data['report'])
            numeric = [v for k,v in asdict(report).items() if k!='converged']
            if (type(report.converged) is not bool or type(report.teams) is not int
                    or report.teams != len(attack) or type(report.matches_used) is not int
                    or report.matches_used < 2 or type(report.iterations) is not int
                    or not 0 <= report.iterations <= model.max_iterations
                    or not 0 <= report.prior_dominance <= 1
                    or any(type(v) not in (int,float) or not math.isfinite(v)
                           for v in numeric)
                    or sum(games.values()) != report.matches_used*2
                    or abs(report.mean_games_behind-sum(games.values())/len(attack))>1e-8
                    or report.rho != values[2]):
                return False
            model._attack,model._defence,model._games = attack,defence,games
            model.base_mu,model.home_adv,model.rho = values
            model.report = report
            model._training_fingerprint = fingerprint
            return True
        except (KeyError,TypeError,ValueError,AttributeError):
            return False

    def _save(self, league, fingerprint, model):
        if self.cache_storage is not None and model.report and model.report.matches_used >= 2:
            self.cache_storage.set_telemetry('model:league_fit:'+league, {
                'fingerprint':fingerprint,'attack':model._attack,'defence':model._defence,
                'games':model._games,'base_mu':model.base_mu,'home_adv':model.home_adv,
                'rho':model.rho,'report':asdict(model.report)})

    def for_league(self, league):
        if league in self.models:
            return self.models[league]
        return DixonColesModel(normalise_identities=True, **self.parameters)

    def fit(self, matches, *, as_of):
        groups = defaultdict(list)
        for match in matches:
            if match.kickoff < as_of:
                groups[match.league].append(match)
        self.models = {key: value for key, value in self.models.items() if key in groups}
        reports = []
        for league, rows in sorted(groups.items()):
            # Daily refit for time decay; unchanged five-minute scans reuse fits.
            key = hashlib.sha256(json.dumps(['league-fit-cache-v2-identities',self.parameters,as_of.date().isoformat(),
                sorted((m.kickoff.isoformat(), m.home, m.away, m.home_score, m.away_score) for m in rows)],
                separators=(',', ':')).encode()).hexdigest()
            model = self.models.setdefault(league, DixonColesModel(normalise_identities=True, **self.parameters))
            if getattr(model, '_training_fingerprint', None) != key:
                self._restore(league,key,model)
            if getattr(model, '_training_fingerprint', None) != key:
                # League scoring/home baselines are estimated only from prior results,
                # shrunk towards configured priors with 40 pseudo-matches.
                weights = [math.exp(-model.xi * max(0, (as_of - m.kickoff).total_seconds()) / 31557600) for m in rows]
                total = sum(weights)
                prior_away = self.parameters.get('base_mu', 1.35)
                prior_home = prior_away * math.exp(self.parameters.get('home_adv', .24))
                away = (sum(w * m.away_score for w, m in zip(weights, rows)) + 40 * prior_away) / (total + 40)
                home = (sum(w * m.home_score for w, m in zip(weights, rows)) + 40 * prior_home) / (total + 40)
                model.base_mu = max(.2, min(4., away))
                model.home_adv = math.log(max(.2, min(4., home)) / model.base_mu)
                model.fit([ScoredMatch(m.league, m.kickoff, m.home, m.away, m.home_score, m.away_score) for m in rows], as_of=as_of)
                model._training_fingerprint = key
                self._save(league,key,model)
            reports.append(model.report)
        reports = [r for r in reports if r is not None]
        teams = sum(r.teams for r in reports)
        mean = sum(r.mean_games_behind * r.teams for r in reports) / teams if teams else 0
        self.report = FitReport(sum(r.matches_used for r in reports), teams,
            sum(r.objective for r in reports), sum(r.iterations for r in reports),
            bool(reports) and all(r.converged for r in reports), mean, 0,
            sum(r.prior_dominance * r.teams for r in reports) / teams if teams else 1)
        return self.report

    def diagnostics(self):
        return {key: dict(model.report.to_dict(), base_mu=model.base_mu, home_adv=model.home_adv)
                for key, model in self.models.items() if model.report}
