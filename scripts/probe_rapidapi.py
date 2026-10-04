#!/usr/bin/env python3
"""One sanitized authenticated request using an operator-verified route.

Writes only the local quota ledger/health telemetry. No forecast publication,
notifications, account changes, purchases or automatic endpoint discovery.
"""
import argparse
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

from lisa.config import load_settings
from lisa.runtime import RuntimeConfig
from lisa.providers.rapidapi import (RapidApiTransport, ODDSPAPI_RAPIDAPI_HOST,
    ODDSPAPI_RAPIDAPI_ROUTES, ODDSPAPI_RAPIDAPI_PARAMS)


def payload_structure(payload, *, depth=0, budget=None):
    """Bounded type/field sketch, never leaf values or arbitrary server text."""
    budget = [250] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth >= 7:
        return {'type': type(payload).__name__, 'detail': 'structure truncated'}
    if isinstance(payload, dict):
        fields = {}
        for name, value in list(payload.items())[:12]:
            if not isinstance(name, str):
                continue
            # Credential-bearing subtrees never enter the report. Keys that
            # contain controls or URLs are replaced rather than echoed.
            lower = name.lower()
            if any(word in lower for word in ('key', 'token', 'secret', 'password', 'authorization')):
                continue
            safe_name = name if len(name) <= 80 and all(c.isalnum() or c in '_-. ' for c in name) else '[field]'
            fields[safe_name] = payload_structure(value, depth=depth+1, budget=budget)
        return {'type': 'dict', 'count': len(payload), 'fields': fields}
    if isinstance(payload, list):
        return {'type': 'list', 'count': len(payload), 'examples': [
            payload_structure(value, depth=depth+1, budget=budget) for value in payload[:2]]}
    return {'type': type(payload).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=ODDSPAPI_RAPIDAPI_HOST, help='Exact X-RapidAPI-Host from the listing')
    parser.add_argument('--path', help='Exact GET endpoint path; defaults to verified main-odds route')
    parser.add_argument('--endpoint', choices=('account', 'markets', 'tournaments', 'participants',
        'odds-by-tournaments', 'historical-odds', 'main-odds'), default='main-odds')
    parser.add_argument('--subscription-scope', default='odds-api1-basic-confirmation-pending',
        help='Stable non-secret subscription plus confirmed billing-period identity; never the API key')
    parser.add_argument('--params', help='Non-secret JSON query parameters')
    parser.add_argument('--provider-auth', choices=('none', 'rapidapi', 'direct'), default='none',
        help='Diagnostic upstream apiKey query auth: none, reuse private RapidAPI key, or private direct OddsPapi key')
    parser.add_argument('--output', type=Path, help='Save a sanitized report, including response structure')
    args = parser.parse_args()
    def report(value):
        value['checked_at'] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(value, indent=2, sort_keys=True)
        if args.output:
            args.output.write_text(encoded + '\n')
        print(encoded)
    settings = RuntimeConfig(load_settings()).settings()
    if not settings.oddspapi_rapidapi_key:
        report({'state': 'missing_credential', 'required': 'ODDSPAPI_RAPIDAPI_KEY'})
        return 1
    if args.provider_auth == 'direct' and not settings.oddspapi_key:
        report({'state': 'missing_provider_credential', 'required': 'ODDSPAPI_KEY'})
        return 1
    from lisa.cli import _make_storage
    storage = _make_storage(settings)
    try:
        path = args.path or ODDSPAPI_RAPIDAPI_ROUTES.get(args.endpoint)
        params = json.loads(args.params) if args.params is not None else (
            dict(ODDSPAPI_RAPIDAPI_PARAMS) if args.endpoint == 'main-odds' else {})
        transport = RapidApiTransport(host=args.host, routes={args.endpoint: path},
            subscription_scope=args.subscription_scope, storage=storage)
        socket.getaddrinfo(args.host, 443, type=socket.SOCK_STREAM)
    except OSError:
        report({'state': 'blocked_network', 'detail': 'Hostname cannot be resolved; key not tested.'})
        return 1
    except (ValueError, TypeError):
        report({'state': 'invalid_contract', 'detail': 'Verify host, GET path and parameters.'})
        return 1
    try:
        provider_api_key = (settings.oddspapi_rapidapi_key if args.provider_auth == 'rapidapi' else
                            settings.oddspapi_key if args.provider_auth == 'direct' else None)
        payload = transport.request(args.endpoint, params, settings.oddspapi_rapidapi_key,
                                    provider_api_key=provider_api_key)
        summary = {'state': 'sample_received', 'endpoint': args.endpoint,
            'payload_type': type(payload).__name__,
            'top_level_items': len(payload) if isinstance(payload, (dict, list)) else None,
            'structure': payload_structure(payload),
            'quota': storage.get_telemetry(transport.status_key),
            'provider_auth_test': args.provider_auth,
            'detail': 'Payload withheld; successful JSON does not validate the market schema.'}
        report(summary)
        return 0
    except Exception as exc:
        result = {'state': 'failed', 'error_type': type(exc).__name__,
                  'provider_auth_test': args.provider_auth,
                  'quota': storage.get_telemetry(transport.status_key) or {},
                  'detail': 'Credential, response body and exception text withheld.'}
        if type(getattr(exc, 'http_status', None)) is int:
            result['http_status'] = exc.http_status
        if getattr(exc, 'diagnostic_code', None) in {
            'subscription_not_active', 'additional_provider_authentication_required',
            'credential_rejected', 'authentication_rejected', 'access_forbidden',
            'endpoint_not_found', 'rate_or_quota_limited', 'http_error'}:
            result['diagnostic_code'] = exc.diagnostic_code
        report(result)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
