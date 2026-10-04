"""Small allowlisted diagnostic codes; never disclose provider error text."""
import socket
from urllib.error import URLError
from .base import ParseError, ProviderNotAvailableError

DIAGNOSTIC_CODES = frozenset({'subscription_or_season_restricted', 'quota_or_rate_limited',
    'authentication_rejected', 'provider_error', 'malformed_envelope', 'unsupported_result_shape',
    'http_access_denied', 'http_error', 'dns_failed', 'request_timed_out',
    'network_unavailable', 'invalid_json', 'transport_failed'})


def http_error(provider, status):
    error = ProviderNotAvailableError('Provider HTTP request failed; response omitted', provider=provider)
    if type(status) is int and 100 <= status <= 599:
        error.http_status = status
    error.diagnostic_code = ('authentication_rejected' if status == 401 else
        'http_access_denied' if status == 403 else 'quota_or_rate_limited' if status == 429 else 'http_error')
    return error


def transport_error(provider, cause):
    import json
    error = ProviderNotAvailableError('Provider transport or JSON failure; details omitted', provider=provider)
    reason = cause.reason if isinstance(cause, URLError) else cause
    error.diagnostic_code = ('dns_failed' if isinstance(reason, socket.gaierror) else
        'request_timed_out' if isinstance(reason, TimeoutError) else
        'network_unavailable' if isinstance(reason, OSError) else
        'invalid_json' if isinstance(reason, json.JSONDecodeError) else 'transport_failed')
    return error


def safe_error_summary(error):
    """Preserve actionable error classifications without exception text."""
    details = [type(error).__name__]
    status = getattr(error, 'http_status', None)
    code = getattr(error, 'diagnostic_code', None)
    if type(status) is int and 100 <= status <= 599:
        details.append('HTTP '+str(status))
    if isinstance(code, str) and code in DIAGNOSTIC_CODES:
        details.append(code)
    return ' · '.join(details)


def response_error(message, *, provider, errors=None, fallback='provider_error'):
    code = fallback
    # Read provider text only to classify it; never return or log the text.
    values = list(errors.values()) if isinstance(errors, dict) else [errors]
    text = ' '.join(value for value in values if isinstance(value, str)).lower()
    if any(word in text for word in ('quota', 'rate limit', 'request limit', 'too many requests')):
        code = 'quota_or_rate_limited'
    elif any(word in text for word in ('invalid key', 'invalid token', 'unauthorized', 'authentication')):
        code = 'authentication_rejected'
    elif any(word in text for word in ('subscription', 'free plan', 'access denied', 'not available for', 'upgrade')):
        code = 'subscription_or_season_restricted'
    error = ParseError(message, provider=provider)
    error.diagnostic_code = code
    return error
