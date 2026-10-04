"""Credential-safe reporting and truthful handling of unavailable networks."""
import importlib.util
import json
import socket
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from lisa.providers.diagnostics import safe_error_summary, http_error

spec = importlib.util.spec_from_file_location('provider_validation',
    Path(__file__).resolve().parents[2] / 'scripts' / 'validate_providers.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProviderValidationTests(unittest.TestCase):
    def test_shared_transport_preserves_http_classification_without_body(self):
        from lisa.providers.base import HttpTransport, RateLimitedError
        body = b'{"account":"private-account","apiKey":"private-secret"}'
        for status, code in ((401, 'authentication_rejected'), (403, 'http_access_denied'),
                             (404, 'http_error'), (429, 'quota_or_rate_limited'),
                             (503, 'http_error')):
            with self.subTest(status=status):
                error = HttpTransport._error_for(status, {'retry-after':'45'}, body, 'sharpapi')
                self.assertIn('HTTP '+str(status), safe_error_summary(error))
                self.assertIn(code, safe_error_summary(error))
                self.assertNotIn('private-', str(error))
                if status == 429:
                    self.assertIsInstance(error, RateLimitedError)
                    self.assertEqual(error.retry_after, 45)

    def test_sharp_snapshot_never_copies_arbitrary_exception_text(self):
        from lisa.providers.sharpapi import SharpApiOddsProvider
        transport = Mock()
        transport.get_json.side_effect = RuntimeError('https://provider/?apiKey=private-secret')
        provider = SharpApiOddsProvider('private-key', transport=transport)
        snapshot = provider.fetch(sport_keys=['soccer_epl'], window_hours=24)
        self.assertIn('RuntimeError', snapshot.error)
        self.assertNotIn('private-', json.dumps(snapshot.to_dict()))
        self.assertNotIn('https://', snapshot.error)

    def test_collecting_provider_failures_does_not_duplicate_price_errors(self):
        from lisa.feed import FeedReport, ProviderStatus, _collect_provider_errors
        from lisa.providers.base import SourceTier
        report = FeedReport(began=datetime.now(timezone.utc), providers=[
            ProviderStatus(name='sharpapi', tier=SourceTier.OFFICIAL, configured=True, used=False,
                error='odds fetch failed: HTTP 401'),
            ProviderStatus(name='oddspapi', tier=SourceTier.OFFICIAL, configured=True, used=False,
                error='request failed: HTTP 403')],
            errors=['sharpapi: odds fetch failed: HTTP 401'])
        _collect_provider_errors(report)
        _collect_provider_errors(report)
        self.assertEqual(report.errors, ['sharpapi: odds fetch failed: HTTP 401',
                                        'oddspapi: request failed: HTTP 403'])

    def test_price_transport_preserves_http_reason_without_credential_urls(self):
        from lisa.providers.the_odds_api import PrivateTransport as OddsTransport
        from lisa.providers.oddspapi import PrivateTransport as PapiTransport
        for adapter,path in ((OddsTransport(),'/v4/sports'),(PapiTransport(),'account')):
            adapter.opener = Mock()
            adapter.opener.open.side_effect = HTTPError('https://example/?apiKey=private-secret',403,
                                                        'private response',{},None)
            with self.assertRaises(Exception) as caught:
                adapter.request(path,{},'private-secret')
            summary = safe_error_summary(caught.exception)
            self.assertIn('HTTP 403',summary)
            self.assertIn('http_access_denied',summary)
            self.assertNotIn('private',summary)

    def test_price_snapshot_keeps_authentication_classification(self):
        from lisa.providers.oddspapi import OddsPapiProvider
        adapter = OddsPapiProvider('private-key',transport=Mock())
        adapter.transport.request.side_effect = http_error('oddspapi',401)
        snapshot = adapter.fetch(sport_keys=['soccer_epl'],window_hours=24,now=datetime.now(timezone.utc))
        self.assertIn('HTTP 401',snapshot.error)
        self.assertIn('authentication_rejected',snapshot.error)
        self.assertNotIn('private-key',snapshot.error)

    def test_dns_classification_is_not_reported_as_an_invalid_key(self):
        from lisa.providers.the_odds_api import PrivateTransport
        adapter = PrivateTransport()
        adapter.opener = Mock()
        adapter.opener.open.side_effect = URLError(socket.gaierror('private DNS detail'))
        with self.assertRaises(Exception) as caught:
            adapter.request('/v4/sports',{},'private-secret')
        self.assertIn('dns_failed',safe_error_summary(caught.exception))
        self.assertNotIn('authentication_rejected',safe_error_summary(caught.exception))

    def test_price_deadline_does_not_inherit_earlier_fixture_delay(self):
        provider = Mock()
        provider.fetch.return_value = SimpleNamespace(error=None, pages=1, quotes=[], truncated=False)
        old_start = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = probe.price_summary(provider, 'soccer_epl', old_start)
        kwargs = provider.fetch.call_args.kwargs
        self.assertGreater(kwargs['deadline'], datetime.now(timezone.utc))
        self.assertEqual(result['request_state'], 'ok')

    def test_zero_page_truncation_cannot_pass_validation(self):
        provider = Mock()
        provider.fetch.return_value = SimpleNamespace(error=None, pages=0, quotes=[], truncated=True)
        result = probe.price_summary(provider, 'soccer_epl', datetime.now(timezone.utc))
        self.assertEqual(result['request_state'], 'not_completed')

    def test_diagnostic_classification_never_exposes_response_text(self):
        from lisa.providers.diagnostics import response_error
        error = response_error('Provider rejected request', provider='test',
            errors={'access': 'Free plan cannot access this season; private-key'})
        def failed():
            raise error
        result = probe.safe_check(failed)
        self.assertEqual(result['diagnostic_code'], 'subscription_or_season_restricted')
        self.assertNotIn('private-key', json.dumps(result))

    def test_error_text_and_credentials_are_never_reported(self):
        def failed():
            raise ValueError('https://provider/?token=private-secret response body')
        result = probe.safe_check(failed)
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('private-secret', json.dumps(result))
        self.assertNotIn('response body', json.dumps(result))

    def test_dns_failure_does_not_test_or_reject_credentials(self):
        settings = SimpleNamespace(allsports_api_key='private-a', api_football_key='private-b',
            football_data_token='private-c', sharpapi_key='private-d', oddspapi_key='private-e')
        with patch.object(probe.socket, 'getaddrinfo', side_effect=socket.gaierror()), \
             patch.object(probe, 'AllSportsProvider') as adapter:
            report = probe.validate(settings)
        adapter.assert_not_called()
        self.assertFalse(report['live_validation_complete'])
        self.assertEqual({r['state'] for r in report['providers'].values()}, {'blocked_network'})
        self.assertNotIn('private-', json.dumps(report))

    def test_missing_credentials_skip_network_requests(self):
        settings = SimpleNamespace(allsports_api_key='', api_football_key='',
            football_data_token='', sharpapi_key='')
        with patch.object(probe.socket, 'getaddrinfo') as dns:
            report = probe.validate(settings)
        dns.assert_not_called()
        self.assertEqual({r['state'] for r in report['providers'].values()}, {'missing_credential'})


if __name__ == '__main__':
    unittest.main()
