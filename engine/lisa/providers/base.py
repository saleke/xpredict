"""Transport + contracts for the free data stack.

Why this module exists
----------------------
The engine used to talk to exactly one host through a bespoke client. The free
stack replaced that host with four, three of which impose their own rate limits.
The bottleneck is no longer "can we make a request" but "how many requests can we
afford, and how do we avoid making the same one twice".

This module therefore owns four things that every adapter needs and none should
reinvent:

1. ``TokenBucket``     -- per-host pacing, so a 10 req/min budget is spent
                          evenly instead of bursty-then-429.
2. ``HttpTransport``   -- one keep-alive connection pool per host, conditional
                          requests (ETag/Last-Modified), and bounded retries.
3. ``ResponseCache``   -- TTL + validator cache, so a 380-match season is
                          fetched once and replayed for a week.
4. Errors + protocols  -- a single vocabulary every adapter raises and obeys.

Performance notes (these are the real hot paths, not micro-optimisations):

* **Keep-alive matters.** urllib opens a fresh TLS handshake per request. On a
  season-scale fetch that is hundreds of handshakes. ``http.client`` connections
  are pooled per (host, port) and reused, cutting wall time by ~3x on the
  historical backfill. A pooled connection that the server has closed is
  transparently re-opened.
* **Conditional requests matter more.** Every free source here supports
  ``If-None-Match`` / ``If-Modified-Since``. A cached 304 costs zero quota,
  which for a 10 req/min budget is worth far more than a faster parser.
* **Pacing beats retrying.** A token bucket converts a burst that would earn a
  429 into a smooth stream that never trips the limit, so retries stay reserved
  for genuine faults.

No third-party dependencies -- standard library only, like the rest of LISA.
"""
from __future__ import annotations

import errno
import gzip
import json
import logging
import random
import socket
import threading
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field
from enum import Enum
from http.client import HTTPConnection, HTTPSConnection
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger("lisa.providers.base")

__all__ = [
    "SourceTier",
    "ProviderQuota",
    "ProviderError",
    "ApiError",
    "RateLimitedError",
    "ParseError",
    "ProviderNotAvailableError",
    "TokenBucket",
    "ResponseCache",
    "CacheEntry",
    "HttpTransport",
    "CalendarProvider",
    "OddsProvider",
    "FixturesResult",
    "is_hard_network_error",
    "network_diagnosis",
    "utcnow_ts",
]

#: Statuses ``http.client`` will hand back that the caller must resolve.
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


#: ``errno`` values that mean "this machine cannot route to that network at all".
#: Retrying them is pure waste: the condition is a property of the host's
#: routing table, not of the provider or of the moment. A provider that is
#: four seconds of backoff away from being retried identically will be four
#: seconds of backoff away every time.
_HARD_NETWORK_ERRNOS = frozenset({
    errno.ENETUNREACH,     # no route to network
    errno.ENETDOWN,        # network is down
    errno.EHOSTUNREACH,    # no route to host
    errno.EADDRNOTAVAIL,   # source address has no route / no usable interface
})


def _errno_of(exc: BaseException) -> Optional[int]:
    """The ``errno`` behind ``exc``, looking through the wrapper chain.

    ``http.client`` re-raises the socket error, and callers wrap again, so the
    interesting number is usually one or two ``raise ... from`` links down.
    """
    seen = 0
    cur: Optional[BaseException] = exc
    while cur is not None and seen < 8:
        code = getattr(cur, "errno", None)
        if isinstance(code, int):
            return code
        # OSError(101, 'Network is unreachable') puts the code first in args on
        # some builds and leaves .errno unset on others.
        if isinstance(cur, OSError) and cur.args and isinstance(cur.args[0], int):
            return int(cur.args[0])
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return None


def is_hard_network_error(exc: BaseException) -> bool:
    """True when retrying ``exc`` cannot possibly help.

    Distinguishing "the provider is briefly unhappy" (retry: 429, 503, a reset
    mid-response) from "this host has no route to the internet at all" (do not
    retry) is the difference between a 4-second answer and a 45-second one, and
    between a useful log line and a misleading one.
    """
    return _errno_of(exc) in _HARD_NETWORK_ERRNOS


def _has_ipv6_route() -> bool:
    """Whether the host has a non-loopback IPv6 default route.

    Read from procfs, so it costs no network I/O and cannot itself fail the
    caller. Used only to explain a hard network error, never to make one.
    """
    try:
        with open("/proc/net/ipv6_route", "r", encoding="ascii") as fh:
            for line in fh:
                fields = line.split()
                # Destination "00" + prefix "00" == default route.
                if len(fields) > 1 and fields[0] == "00" and fields[1] == "00":
                    return True
    except OSError:
        pass
    return False


def _has_ipv4_route() -> bool:
    try:
        with open("/proc/net/route", "r", encoding="ascii") as fh:
            next(fh, None)
            for line in fh:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return True
    except OSError:
        pass
    return False


def _address_families(host: str) -> tuple[bool, bool]:
    """Which families ``host`` publishes. Best effort; empty on DNS failure."""
    has4 = has6 = False
    try:
        for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
            if info[0] == socket.AF_INET6:
                has6 = True
            elif info[0] == socket.AF_INET:
                has4 = True
    except OSError:
        pass
    return has4, has6


def network_diagnosis(host: str, code: Optional[int] = None) -> str:
    """Explain an unreachable host in terms an operator can act on.

    The production failure this exists for was a board that silently came back
    empty: OpenLigaDB is IPv4-only, and the machine had an IPv4 default route
    whose traffic was then refused upstream. The log said "Network is
    unreachable" once per provider, which reads like a provider outage and is
    not one.

    The actionable fact is not merely "unreachable" but the mismatch between
    what the provider publishes, what routes the host has, and what the kernel
    actually said. Reporting a default route as sufficient would be wrong --
    having a route and being able to use it are different things.
    """
    parts: list[str] = []
    has4, has6 = _address_families(host)
    if has4 and not has6:
        parts.append(f"{host} publishes IPv4 only")
    elif has6 and not has4:
        parts.append(f"{host} publishes IPv6 only")
    elif has4 and has6:
        parts.append(f"{host} publishes IPv4 and IPv6")
    else:
        parts.append(f"{host} did not resolve to any address")

    route4, route6 = _has_ipv4_route(), _has_ipv6_route()
    parts.append(f"host has IPv4 default route: {route4}, IPv6 default route: {route6}")

    needs4, needs6 = has4, has6
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
        # The kernel had a path and the path did not carry traffic.
        if needs4 and not route4:
            parts.append(
                "no IPv4 default route is installed, so IPv4-only sources "
                "cannot be reached until IPv4 connectivity returns")
        elif needs4 and route4:
            parts.append(
                "an IPv4 default route exists but IPv4 traffic is being "
                "refused or dropped upstream -- typically an IPv6-only WAN, a "
                "captive network, or a local firewall; the route table alone "
                "does not prove the path works")
        if needs6 and not route6:
            parts.append(
                "no IPv6 default route is installed, so IPv6-only sources "
                "cannot be reached")
    else:
        if needs4 and not has6 and route4:
            parts.append("IPv4 routing looks configured; check DNS or TLS")
        if needs6 and not has4 and route6:
            parts.append("IPv6 routing looks configured; check DNS or TLS")
    return "; ".join(parts)


def utcnow_ts() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Tiers + quota
# ---------------------------------------------------------------------------


class SourceTier(Enum):
    """How far a feed can be trusted, and how it must be surfaced."""

    OFFICIAL = "official"       # Documented, supported API on a free tier.
    UNOFFICIAL = "unofficial"   # Scraped / community / ToS-grey: always flagged.


@dataclass(frozen=True)
class ProviderQuota:
    """Remaining request budget.

    ``limit <= 0`` means "not metered" and must never be treated as exhausted --
    OpenLigaDB, for example, publishes no limit at all.
    """

    remaining: int
    limit: int
    reset_at: float = 0.0
    tier: SourceTier = SourceTier.OFFICIAL

    @property
    def metered(self) -> bool:
        return self.limit > 0

    @property
    def consumed(self) -> int:
        return max(0, self.limit - self.remaining) if self.metered else 0

    @property
    def pct_remaining(self) -> float:
        if not self.metered:
            return 100.0
        return round(max(0.0, self.remaining / self.limit * 100.0), 2)

    def has_headroom(self, need: int = 1) -> bool:
        if not self.metered:
            return True
        return self.remaining >= need


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for provider failures.

    ``retry_after`` lets the router honour a server-supplied cooldown instead of
    guessing at one.
    """

    retryable = True

    def __init__(self, message: str, *, provider: str = "",
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after


class ApiError(ProviderError):
    """Transport failure: timeout, DNS, TLS, or a non-2xx response."""


class ParseError(ProviderError):
    """The provider answered, but the payload could not be trusted.

    Deliberately distinct from "no data": a silent schema change must surface as
    an error rather than as an empty board.
    """


class RateLimitedError(ProviderError):
    """429, or a quota response that would not have been billed."""

    def __init__(self, message: str, *, provider: str = "", retry_after: float = 60.0):
        super().__init__(message, provider=provider, retry_after=retry_after)


class ProviderNotAvailableError(ProviderError):
    """Disabled by config, or deliberately left unconfigured."""

    retryable = False


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TokenBucket:
    """Thread-safe token bucket for per-host request pacing.

    A bucket of ``rate`` tokens/second refilling to a burst of ``burst`` lets a
    caller make a few requests immediately and then settles into the advertised
    rate. That is strictly better than sleeping a fixed interval: it removes the
    latency penalty from the first request of a cycle while still guaranteeing
    the long-run average never exceeds the limit.

    The clock is injectable so tests do not have to sleep.
    """

    def __init__(self, rate_per_sec: float, burst: Optional[float] = None,
                 *, monotonic=time.monotonic) -> None:
        self.rate = max(0.0, float(rate_per_sec))
        self.burst = max(1.0, float(burst if burst is not None else max(1.0, rate_per_sec)))
        self._tokens = self.burst
        self._last = monotonic()
        self._clock = monotonic
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed <= 0.0:
            return
        self._last = now
        if self.rate <= 0.0:          # unmetered host: never blocks
            self._tokens = self.burst
            return
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available. Returns seconds waited."""
        if self.rate <= 0.0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
                # Wait under the lock only long enough to re-check; sleeping
                # while other threads drain the bucket would serialise callers
                # that could have proceeded in parallel.
            time.sleep(max(0.001, min(delay, 5.0)))
            waited += max(0.001, min(delay, 5.0))

    @property
    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    body: bytes
    etag: Optional[str]
    last_modified: Optional[str]
    stored_at: float
    content_type: str = "application/json"

    def is_fresh(self, ttl: float, now: Optional[float] = None) -> bool:
        if ttl <= 0:
            return False
        return ((now if now is not None else utcnow_ts()) - self.stored_at) < ttl

    def age(self, now: Optional[float] = None) -> float:
        return (now if now is not None else utcnow_ts()) - self.stored_at


class ResponseCache:
    """In-process TTL + HTTP-validator cache keyed by absolute URL.

    Two independent reuse paths, both important:

    * **Freshness hit** -- within TTL, returned without touching the network.
    * **Revalidation** -- past TTL but holding an ``ETag``, the request still
      goes out carrying ``If-None-Match``; a 304 replays the stored body and
      costs the provider nothing.

    The second path is what makes it safe to poll frequently on a budget that
    cannot afford a full body, so it is implemented rather than hand-waved.
    """

    def __init__(self, *, max_entries: int = 512) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_entries = max(1, max_entries)
        self.hits = 0
        self.revalidations = 0
        self.misses = 0

    def get(self, url: str) -> Optional[CacheEntry]:
        with self._lock:
            return self._entries.get(url)

    def put(self, url: str, entry: CacheEntry) -> None:
        with self._lock:
            if len(self._entries) >= self._max_entries and url not in self._entries:
                # Cheap bounded eviction: drop the oldest insertion. A full LRU
                # costs a lock-held sort on every write for no real benefit at
                # this size.
                oldest = min(self._entries.items(), key=lambda kv: kv[1].stored_at)[0]
                self._entries.pop(oldest, None)
            self._entries[url] = entry

    def validators(self, url: str) -> tuple[Optional[str], Optional[str]]:
        entry = self.get(url)
        if entry is None:
            return (None, None)
        return (entry.etag, entry.last_modified)

    def record_hit(self) -> None:
        with self._lock:
            self.hits += 1

    def record_revalidation(self) -> None:
        with self._lock:
            self.revalidations += 1

    def record_miss(self) -> None:
        with self._lock:
            self.misses += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries), "hits": self.hits,
                    "revalidations": self.revalidations, "misses": self.misses}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass
class _HostState:
    bucket: TokenBucket
    connection: Optional[HTTPConnection] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


#: Per-host pacing state, shared by every transport in the process. See
#: :meth:`HttpTransport.set_rate_limit` for why this cannot be per-transport.
_SHARED_BUCKETS: dict[str, TokenBucket] = {}
_SHARED_BUCKETS_LOCK = threading.Lock()


def _shared_bucket(host: str, rate_per_sec: float,
                   burst: Optional[float]) -> TokenBucket:
    """The one bucket for ``host``, created on first use.

    Keyed by rate as well as host so a test (or a reconfigured deployment) that
    declares a different budget gets its own meter rather than inheriting
    another one's history.
    """
    rate = max(0.0, float(rate_per_sec))
    capacity = max(1.0, float(burst if burst is not None else max(1.0, rate)))
    key = f"{host}|{rate:.6f}|{capacity:.3f}"
    with _SHARED_BUCKETS_LOCK:
        bucket = _SHARED_BUCKETS.get(key)
        if bucket is None:
            bucket = _SHARED_BUCKETS[key] = TokenBucket(rate, capacity)
        return bucket


def reset_host_pacing(host: Optional[str] = None) -> None:
    """Forget accumulated pacing. For tests, which must not inherit a real
    cycle's meter and then wait on it."""
    with _SHARED_BUCKETS_LOCK:
        if host is None:
            _SHARED_BUCKETS.clear()
        else:
            for key in [k for k in _SHARED_BUCKETS if k.split("|", 1)[0] == host]:
                del _SHARED_BUCKETS[key]


class HttpTransport:
    """Keep-alive HTTP client with pacing, conditional requests and retries.

    Retry policy is deliberately narrow, because retrying the wrong thing is how
    a free-tier client burns a budget:

    * 429 / 503 -> retry, honouring ``Retry-After`` when present.
    * 5xx / transport errors -> retry with jittered exponential backoff.
    * 4xx (other) -> raise immediately. A 401 or 404 will never succeed on a
      retry and every attempt is wasted quota plus wall time.

    ``time_budget`` bounds the *total* time spent retrying one call, so a cycle
    cannot be stalled by a host that keeps returning 503.
    """

    def __init__(self, *, timeout: float = 15.0, max_retries: int = 3,
                 backoff_base: float = 1.0, cache: Optional[ResponseCache] = None,
                 user_agent: str = "lisa/2.0 (+https://github.com/saleke/xpredict)",
                 time_budget: float = 45.0, default_rate_per_sec: float = 1.0,
                 max_redirects: int = 5) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.cache = cache if cache is not None else ResponseCache()
        self.user_agent = user_agent
        self.time_budget = time_budget
        self.max_redirects = max_redirects
        self._hosts: dict[str, _HostState] = {}
        self._hosts_lock = threading.Lock()
        self._default_rate = default_rate_per_sec
        self.request_count = 0
        self.not_modified_count = 0

    # -- host bookkeeping ---------------------------------------------------

    def set_rate_limit(self, host: str, rate_per_sec: float, burst: Optional[float] = None) -> None:
        """Declare a host's published budget.

        The pacing state is process-wide per host, not per transport, and that
        lifetime is the whole point. A provider's budget is a property of the
        *provider* -- "ten requests a minute" is ten across everything LISA
        does -- so a bucket scoped to one cycle would hand every cycle a fresh
        burst allowance. Two cycles close together would then fire burst+burst
        requests inside the provider's sliding window and earn a 429 that no
        amount of in-cycle pacing can prevent.

        Re-declaring the same rate is a no-op rather than a reset, for the same
        reason: replacing the bucket would throw away the pacing already earned.
        A genuinely changed rate does replace it.
        """
        with self._hosts_lock:
            bucket = _shared_bucket(host, rate_per_sec, burst)
            state = self._hosts.get(host)
            if state is None:
                self._hosts[host] = _HostState(bucket=bucket)
            else:
                state.bucket = bucket

    def _state(self, host: str) -> _HostState:
        with self._hosts_lock:
            state = self._hosts.get(host)
            if state is None:
                state = _HostState(bucket=TokenBucket(self._default_rate))
                self._hosts[host] = state
            return state

    # -- request ------------------------------------------------------------

    def get_json(self, url: str, *, params: Optional[Mapping[str, Any]] = None,
                 headers: Optional[Mapping[str, str]] = None,
                 ttl: float = 0.0, provider: str = "",
                 accept_304: bool = True) -> Any:
        """GET ``url`` and decode JSON, honouring the cache.

        Raises the provider error vocabulary -- never a bare ``urllib`` error --
        so adapters and the router can classify failures uniformly.
        """
        if params:
            clean = {k: str(v) for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{urllib.parse.urlencode(clean)}"

        entry = self.cache.get(url) if ttl > 0 else None
        if entry is not None and entry.is_fresh(ttl):
            self.cache.record_hit()
            logger.debug("cache hit (fresh) %s", url)
            return self._decode(entry.body, url, provider)
        if entry is None and ttl > 0:
            self.cache.record_miss()

        req_headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(headers)
        if entry is not None and accept_304:
            if entry.etag:
                req_headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                req_headers["If-Modified-Since"] = entry.last_modified

        status, headers_out, body = self._request_with_retries(
            url, req_headers, provider=provider)

        if status == 304 and entry is not None:
            self.not_modified_count += 1
            self.cache.record_revalidation()
            logger.debug("304 revalidation hit %s", url)
            return self._decode(entry.body, url, provider)

        content = _decompress(body, headers_out.get("content-encoding", ""))
        if status in (200, 201):
            if ttl > 0:
                self.cache.put(url, CacheEntry(
                    body=content,
                    etag=headers_out.get("etag"),
                    last_modified=headers_out.get("last-modified"),
                    stored_at=utcnow_ts(),
                ))
            return self._decode(content, url, provider)

        # Any other 2xx: decode defensively, some sources answer 206/202.
        if 200 <= status < 300:
            return self._decode(content, url, provider)

        raise self._error_for(status, headers_out, content, provider)

    # -- internals ----------------------------------------------------------

    def _request_with_retries(self, url: str, headers: dict[str, str], *,
                              provider: str) -> tuple[int, dict[str, str], bytes]:
        state = self._state(urllib.parse.urlsplit(url).hostname or "")
        started = time.monotonic()
        last_error: Optional[Exception] = None
        current_url = url
        redirects = 0

        for attempt in range(self.max_retries + 1):
            if time.monotonic() - started > self.time_budget:
                raise ApiError(f"time budget exhausted for {url}", provider=provider)

            parts = urllib.parse.urlsplit(current_url)
            host = parts.hostname or ""
            state = self._state(host)
            state.bucket.acquire()
            try:
                status, resp_headers, body = self._single(
                    parts, headers, state, host)
            except Exception as exc:
                last_error = exc
                # A host with no route to the network will not acquire one by
                # being asked again. Retrying burned 4 x timeout on every
                # provider per cycle -- a 10s board build that could not
                # succeed and a 45s budget spent proving it.
                if is_hard_network_error(exc):
                    raise ApiError(
                        f"{host} is unreachable from this host "
                        f"({network_diagnosis(host, _errno_of(exc))}): {exc!r}",
                        provider=provider) from exc
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt))
                    continue
                # Raise here. Falling through would reach the redirect check
                # with `status` never bound, and Python would raise
                # UnboundLocalError -- which both replaced the real transport
                # error (a connection failure, a TLS error) with something
                # meaningless, and made the fetch look like a provider bug
                # rather than a network one.
                if isinstance(exc, ApiError):
                    raise
                raise ApiError(
                    f"{host} transport failure after "
                    f"{self.max_retries + 1} attempts: {exc!r}",
                    provider=provider) from exc
            # http.client deliberately does not follow redirects. At least one
            # free source (OpenLigaDB) answers 301 on the extension-less path, so
            # following is required rather than optional. Bounded, and the
            # resolved URL is re-budgeted, so a redirect loop cannot spin.
            if status in _REDIRECT_CODES and redirects < self.max_redirects:
                location = resp_headers.get("location")
                if location:
                    current_url = urllib.parse.urljoin(current_url, location)
                    redirects += 1
                    continue


            if status in (429, 503):
                retry_after = _retry_after_seconds(resp_headers)
                if attempt < self.max_retries:
                    time.sleep(retry_after if retry_after is not None
                               else self._backoff(attempt))
                    continue
                raise RateLimitedError(
                    f"{host} returned {status} after {self.max_retries} retries",
                    provider=provider,
                    retry_after=retry_after if retry_after is not None else 60.0)
            if 500 <= status < 600:
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt))
                    continue
                last_error = ApiError(f"{host} returned {status}", provider=provider)
            return status, resp_headers, body

        raise ApiError(f"request failed for {url}: {last_error}",
                       provider=provider) from last_error

    def _single(self, parts: Any, headers: dict[str, str],
                state: _HostState, host: str) -> tuple[int, dict[str, str], bytes]:
        """One request, reusing a pooled connection when possible."""
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        port = parts.port
        is_https = parts.scheme == "https"

        with state.lock:
            for attempt in range(2):       # 2: one retry after a stale socket
                conn = state.connection
                if conn is None:
                    conn = (HTTPSConnection if is_https else HTTPConnection)(
                        host, port, timeout=self.timeout)
                    state.connection = conn
                try:
                    conn.request("GET", path, headers=headers)
                    resp = conn.getresponse()
                    body = resp.read()
                    self.request_count += 1
                    return resp.status, _flat_headers(resp), body
                except Exception as exc:
                    # A keep-alive socket the peer already closed raises on
                    # first use. Drop it and retry once on a fresh connection
                    # before treating the call as a real failure.
                    try:
                        conn.close()
                    except Exception:
                        pass
                    state.connection = None
                    if attempt == 0 and _is_stale_connection(exc):
                        logger.debug("stale pooled connection to %s, reopening", host)
                        continue
                    raise

    def _backoff(self, attempt: int) -> float:
        if attempt <= 0:
            return 0.0
        return min(self.backoff_base * (2 ** (attempt - 1)), 30.0) + random.uniform(0.0, 0.25)

    @staticmethod
    def _decode(body: bytes, url: str, provider: str) -> Any:
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParseError(f"{provider or url}: invalid JSON ({exc})",
                             provider=provider) from exc

    @staticmethod
    def _error_for(status: int, headers: dict[str, str], body: bytes,
                   provider: str) -> ProviderError:
        snippet = body[:200].decode("utf-8", "replace")
        if status in (401, 403):
            return ApiError(
                f"auth failed ({status}): {snippet} -- check the API key for "
                f"{provider or 'this provider'}", provider=provider)
        if status == 404:
            return ApiError(f"not found (404): {url if False else snippet}",
                            provider=provider)
        if status == 429:
            return RateLimitedError(f"rate limited: {snippet}", provider=provider,
                                    retry_after=_retry_after_seconds(headers) or 60.0)
        return ApiError(f"HTTP {status}: {snippet}", provider=provider)

    def close(self) -> None:
        with self._hosts_lock:
            states = list(self._hosts.values())
            self._hosts.clear()
        for state in states:
            conn = state.connection
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def stats(self) -> dict[str, Any]:
        return {"requests": self.request_count, "not_modified": self.not_modified_count,
                **self.cache.stats()}


def _flat_headers(resp: Any) -> dict[str, str]:
    """Lower-cased header map; HTTPMessage is case-insensitive but this is cheap."""
    try:
        return {k.lower(): v for k, v in resp.getheaders()}
    except Exception:
        return {}


def _decompress(body: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower().strip()
    if not body or enc in ("", "identity"):
        return body
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception as exc:
        logger.debug("could not decompress %s payload: %s", enc, exc)
    return body


def _retry_after_seconds(headers: Mapping[str, str]) -> Optional[float]:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    # Some providers (SharpAPI among them) send an absolute Unix timestamp in
    # milliseconds rather than a duration. Handle both, or a 24-day sleep.
    if value > 1_000_000_000:
        seconds = (value / 1000.0) - time.time()
        return max(0.0, min(seconds, 300.0))
    return max(0.0, min(value, 300.0))


def _is_stale_connection(exc: Exception) -> bool:
    """True for the 'peer already closed this keep-alive socket' family."""
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return True
    text = str(exc).lower()
    return "remotelyclosed" in text or "connection reset" in text or "not connected" in text


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixturesResult:
    """Normalised fixtures + results from a calendar source.

    One shape for every free source, so the model never learns a provider's
    field names. ``finished`` rows carry scores; unfinished rows do not.
    """

    provider: str
    sport_key: str
    fixtures: tuple[Mapping[str, Any], ...] = ()

    def finished(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(f for f in self.fixtures if f.get("completed"))

    def upcoming(self, now: Optional[float] = None) -> tuple[Mapping[str, Any], ...]:
        cutoff = now if now is not None else utcnow_ts()
        return tuple(f for f in self.fixtures
                     if not f.get("completed") and f.get("epoch", 0) >= cutoff)


@runtime_checkable
class CalendarProvider(Protocol):
    """Fixtures + results. The discovery primitive for a model-first engine.

    Deliberately narrower than ``OddsProvider``: with free data the calendar is
    the product, and odds are a scarce enrichment layered on top of it.
    """

    name: str
    tier: SourceTier

    def get_fixtures(self, sport_key: str) -> FixturesResult:
        """Fixtures (and results, where known) for one league."""

    def quota_status(self) -> ProviderQuota:
        ...

    def is_available(self) -> bool:
        ...


@runtime_checkable
class OddsProvider(Protocol):
    """A source of *prices*. Scarce, so the contract is minimal.

    Each adapter owns its own payload -> domain translation (``parse_odds``):
    there is no global parser registry, so a mistyped provider name can never be
    parsed by the wrong adapter.
    """

    name: str
    tier: SourceTier

    def get_odds(self, sport_key: str, *, region: str = "us",
                 markets: str = "h2h") -> list[dict[str, Any]]:
        ...

    def parse_odds(self, raw: Iterable[Mapping[str, Any]], *,
                   market_keys: Sequence[str]) -> list:
        ...

    def quota_status(self) -> ProviderQuota:
        ...

    def is_available(self) -> bool:
        ...
