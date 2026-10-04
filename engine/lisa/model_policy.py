"""Version and evidence gate for executable model recommendations."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

MODEL_VERSION = 'league-dixon-coles-v3'
FORECAST_SOURCES = ('dixon-coles-v1', 'league-dixon-coles-v2', MODEL_VERSION)


class EvidenceGate:
    """Fail closed unless a reviewed, current out-of-sample scope is supplied.

    Artifacts are operator-reviewed research evidence, not provider probabilities.
    This gate does not make an empirical profitability result into a guarantee.
    """
    def __init__(self, path='', *, now=None):
        self.scopes = []
        self.error = 'No reviewed out-of-sample validation artifact'
        now = now or datetime.now(timezone.utc)
        if not path:
            return
        try:
            artifact = json.loads(Path(path).read_text())
            if artifact.get('schema_version') != 1 or artifact.get('model_version') != MODEL_VERSION:
                raise ValueError('Validation artifact schema/model version mismatch')
            end = datetime.fromisoformat(artifact['evaluated_until'])
            expiry = datetime.fromisoformat(artifact['expires_at'])
            if end.tzinfo is None or expiry.tzinfo is None or not end <= now < expiry:
                raise ValueError('Validation artifact dates are future, naive or expired')
            if not artifact.get('out_of_sample') or not artifact.get('untouched_test'):
                raise ValueError('Validation requires a separate untouched chronological test period')
            if not artifact.get('configuration_hash'):
                raise ValueError('Validation configuration fingerprint is missing')
            self.configuration_hash = artifact['configuration_hash']
            self.scopes = artifact['scopes']
            if not isinstance(self.scopes, list) or not all(isinstance(scope, dict) for scope in self.scopes):
                raise ValueError('Validation scopes must be a list of objects')
            self.error = None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.error = f'Validation unavailable: {exc}'

    def margin(self, league, market, selection, line, book_key, *, configuration_hash=None):
        if self.error or configuration_hash != self.configuration_hash:
            return None
        for scope in self.scopes:
            if (scope.get('league'), scope.get('market'), scope.get('selection'), scope.get('line')) != (league, market, selection, line):
                continue
            try:
                checks = (scope.get('approved') is True, scope.get('timestamped_prices') is True,
                    scope.get('multiple_testing_control') is True, int(scope['samples']) >= 500,
                    book_key in scope.get('bookmakers', []), 0 <= float(scope['ece']) <= .05,
                    float(scope['brier']) <= float(scope['baseline_brier']), float(scope['roi_lower_95']) > 0)
                margin = float(scope['probability_margin'])
                metrics = [float(scope[k]) for k in ('ece', 'brier', 'baseline_brier', 'roi_lower_95')]
                if all(checks) and all(math.isfinite(v) for v in metrics) and math.isfinite(margin) and .01 <= margin <= .2:
                    return margin
            except (ValueError, KeyError, TypeError):
                continue
        return None


def configuration_hash(settings):
    import hashlib
    values = {k: getattr(settings, k) for k in ('model_base_mu', 'model_home_adv', 'model_shrinkage', 'model_xi')}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
