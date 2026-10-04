import tempfile
import unittest
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from lisa.config import Settings
from lisa.provider_credentials import CredentialStore
from lisa.providers.base import ProviderNotAvailableError
from lisa.providers.rapidapi import (RapidApiTransport, ODDSPAPI_RAPIDAPI_HOST,
    ODDSPAPI_RAPIDAPI_ROUTES, ODDSPAPI_RAPIDAPI_PARAMS, http_diagnostic)
from lisa.runtime import RuntimeConfig, editable_fields
from lisa.storage import SqliteStorage


class Response:
    status = 200
    def __init__(self, headers=None, body=b'{"example": true}'):
        self.headers = headers or {}
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return self.body[:limit]


class RapidApiTests(unittest.TestCase):
    def test_upstream_query_auth_requires_explicit_option_and_is_not_reported(self):
        self.transport.request('markets', {}, 'private-key', provider_api_key='upstream-private-key')
        req = self.opener.open.call_args.args[0]
        self.assertIn('apiKey=upstream-private-key', req.full_url)
        self.assertEqual(req.get_header('X-rapidapi-key'), 'private-key')
        self.assertNotIn('upstream-private-key', str(self.storage.admin_get_telemetry()))

    def test_upstream_query_auth_failure_does_not_echo_credential_url(self):
        self.opener.open.side_effect = OSError('https://synthetic.p.rapidapi.com?apiKey=upstream-private-key')
        with self.assertRaises(ProviderNotAvailableError) as raised:
            self.transport.request('markets', {}, 'private-key', provider_api_key='upstream-private-key')
        self.assertNotIn('upstream-private-key', str(raised.exception))
        self.assertNotIn('apiKey', str(raised.exception))

    def test_forbidden_errors_expose_only_fixed_diagnostics(self):
        body = json.dumps({'message': 'You are not subscribed to this API. private-key'}).encode()
        self.opener.open.side_effect = HTTPError('https://synthetic.p.rapidapi.com', 403,
            'private-key', {}, BytesIO(body))
        with self.assertRaises(ProviderNotAvailableError) as raised:
            self.transport.request('markets', {}, 'private-key')
        self.assertEqual(raised.exception.http_status, 403)
        self.assertEqual(raised.exception.diagnostic_code, 'subscription_not_active')
        telemetry = self.storage.get_telemetry(self.transport.status_key)
        self.assertEqual(telemetry['diagnostic_code'], 'subscription_not_active')
        self.assertNotIn('private-key', json.dumps(telemetry))
        self.assertEqual(http_diagnostic(403, b'{"message":"apiKey is required"}'),
                         'additional_provider_authentication_required')
        self.assertEqual(http_diagnostic(403, b'<html>private-key</html>'), 'access_forbidden')

    def test_supplied_main_odds_contract_uses_exact_route_and_parameters(self):
        transport = RapidApiTransport(host=ODDSPAPI_RAPIDAPI_HOST,
            routes=ODDSPAPI_RAPIDAPI_ROUTES, storage=self.storage,
            subscription_scope='verified-route-test', opener=self.opener)
        transport.request('main-odds', ODDSPAPI_RAPIDAPI_PARAMS, 'private-key')
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url,
            'https://odds-api1.p.rapidapi.com/fixtures/odds/main?since=0&bookmakers=pinnacle%2Cstake%2Cdraftkings')
        self.assertEqual(req.get_header('X-rapidapi-host'), ODDSPAPI_RAPIDAPI_HOST)

    def test_probe_reports_bounded_structure_without_leaf_values(self):
        spec = importlib.util.spec_from_file_location('rapidapi_probe',
            Path(__file__).resolve().parents[2] / 'scripts' / 'probe_rapidapi.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        payload = {'apiKey': 'private-key', 'fixtures': [{'fixtureId': 'test-fixture',
            'home': 'secret-home-name', 'odds': 2.11, 'nested': {'token': 'private-token'}}]}
        structure = probe.payload_structure(payload)
        encoded = json.dumps(structure)
        for secret in ('private-key', 'private-token', 'secret-home-name', 'test-fixture', '2.11'):
            self.assertNotIn(secret, encoded)
        self.assertIn('fixtureId', encoded)
        self.assertIn('float', encoded)
        self.assertLess(len(json.dumps(probe.payload_structure([payload] * 10000))), 12000)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.storage = SqliteStorage(str(Path(self.folder.name) / 'test.db'))
        self.opener = Mock()
        self.opener.open.return_value = Response()
        self.transport = self.make()

    def make(self, **kwargs):
        # Deliberately synthetic host/routes: no claim about marketplace routes.
        return RapidApiTransport(host='synthetic.p.rapidapi.com', routes={'markets': '/example/markets'},
            storage=self.storage, subscription_scope='test-subscription-period',
            opener=self.opener, **kwargs)

    def test_header_only_authentication(self):
        result = self.transport.request('markets', {'language': 'en'}, 'private-key')
        self.assertEqual(result, {'example': True})
        req = self.opener.open.call_args.args[0]
        self.assertNotIn('private-key', req.full_url)
        self.assertEqual(req.get_header('X-rapidapi-key'), 'private-key')
        self.assertEqual(req.get_header('X-rapidapi-host'), 'synthetic.p.rapidapi.com')
        self.assertNotIn('private-key', str(self.storage.admin_get_telemetry()))

    def test_unverified_route_and_credential_query_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.transport.request('account', {}, 'private-key')
        with self.assertRaises(ValueError):
            self.transport.request('markets', {'apiKey': 'private-key'}, 'private-key')
        self.opener.open.assert_not_called()

    def test_budget_is_atomic_and_shared_across_instances(self):
        def reserve(_):
            transport = self.make(monthly_limit=8, reserve=2)
            try:
                transport._reserve()
                return True
            except ProviderNotAvailableError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(12))), 6)
        with self.assertRaises(ProviderNotAvailableError):
            self.make(monthly_limit=8, reserve=2)._reserve()

    def test_external_usage_floor_cannot_be_lowered_by_late_response(self):
        self.transport._reserve()
        self.transport._reconcile({'X-RateLimit-Requests-Limit': '250',
            'X-RateLimit-Requests-Remaining': '25', 'X-RateLimit-Requests-Reset': '12345'}, 200)
        self.transport._reconcile({'x-ratelimit-requests-limit': '250',
            'x-ratelimit-requests-remaining': '200'}, 200)
        with self.assertRaises(ProviderNotAvailableError):
            self.transport._reserve()
        self.assertEqual(self.storage.get_telemetry(self.transport.status_key)['requests_reserved'], 225)

    def test_exhaustion_does_not_reset_on_key_replacement_or_calendar_change(self):
        self.transport._reserve()
        self.transport._reconcile({'x-ratelimit-requests-limit': '250',
            'x-ratelimit-requests-remaining': '0', 'x-ratelimit-requests-reset': '1'}, 200)
        restarted = self.make(clock=lambda: 2100000000)
        with self.assertRaises(ProviderNotAvailableError):
            restarted.request('markets', {}, 'replacement-key')
        self.opener.open.assert_not_called()

    def test_http_errors_are_sanitized_and_cooldown_persists(self):
        self.opener.open.side_effect = HTTPError('https://synthetic.p.rapidapi.com', 429,
            'private-key', {'Retry-After': '600'}, BytesIO(b'private-key'))
        with self.assertRaises(ProviderNotAvailableError) as raised:
            self.transport.request('markets', {}, 'private-key')
        self.assertNotIn('private-key', str(raised.exception))
        with self.assertRaises(ProviderNotAvailableError):
            self.make()._reserve()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_mock_payload_not_accepted_as_live_validation(self):
        self.opener.open.return_value = Response({'X-RapidAPI-Mock-Response': 'true'})
        with self.assertRaises(ProviderNotAvailableError):
            self.transport.request('markets', {}, 'private-key')

    def test_malformed_headers_do_not_corrupt_quota(self):
        self.transport._reserve()
        self.transport._reconcile({'x-ratelimit-requests-limit': '-1',
            'x-ratelimit-requests-remaining': 'not-a-number'}, 200)
        self.assertEqual(self.storage.get_telemetry(self.transport.status_key)['requests_reserved'], 1)

    def test_unsafe_hosts_routes_and_above_free_limit_rejected(self):
        for host in ('localhost', 'synthetic.p.rapidapi.com@evil.com', 'https://synthetic.p.rapidapi.com'):
            with self.assertRaises(ValueError):
                RapidApiTransport(host=host, routes={'markets': '/markets'}, storage=self.storage,
                                  subscription_scope='test')
        with self.assertRaises(ValueError):
            self.make(monthly_limit=251)
        with self.assertRaises(ValueError):
            RapidApiTransport(host='synthetic.p.rapidapi.com', routes={'markets': '//evil/path'},
                              storage=self.storage, subscription_scope='test')

    def test_admin_credential_is_private_and_distinct_from_direct_key(self):
        path = str(Path(self.folder.name) / 'credentials.json')
        runtime = RuntimeConfig(Settings(provider_credentials_path=path, oddspapi_key='direct-key'))
        CredentialStore(path).update('oddspapi_rapidapi', 'rapid-key')
        self.assertEqual(runtime.settings().oddspapi_rapidapi_key, 'rapid-key')
        self.assertEqual(runtime.settings().oddspapi_key, 'direct-key')
        self.assertNotIn('oddspapi_rapidapi_key', editable_fields())


if __name__ == '__main__':
    unittest.main()
