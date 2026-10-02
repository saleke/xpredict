"""The odds key pool must never overspend, and must actually use every key."""
from __future__ import annotations

import unittest

from lisa import config as cfg
from lisa.key_pool import OddsKeyPool, PoolStatus, QuotaExhausted, RotatingOddsClient


class _Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance_days(self, days: float) -> None:
        self.t += days * 86400.0


def _pool(keys=("k1", "k2"), **kw) -> OddsKeyPool:
    return OddsKeyPool(keys, **kw)


class TestKeyCollection(unittest.TestCase):
    def test_every_documented_key_form_is_collected(self):
        import os
        saved = {k: v for k, v in os.environ.items() if "API_KEY" in k}
        try:
            for k in list(saved):
                os.environ.pop(k, None)
            os.environ["THE_ODDS_API_KEYS"] = "pool1,pool2"
            os.environ["THE_ODDS_API_KEY_3"] = "extra3"
            os.environ["THE_ODD_API_KEY"] = "typo"
            keys = cfg._collect_api_keys()
            self.assertEqual(keys, ("pool1", "pool2", "typo", "extra3"))
            self.assertEqual(len(set(keys)), len(keys), "keys must be de-duplicated")
        finally:
            for k in list(os.environ):
                if "API_KEY" in k:
                    os.environ.pop(k, None)
            os.environ.update(saved)

    def test_the_primary_key_is_the_first_of_the_pool(self):
        """The invariant matters; how many keys the operator has does not.

        This used to assert against the developer's own ``.env`` -- it read
        whatever credentials happened to be lying around and failed or passed
        depending on who ran it. Setting the environment explicitly tests the
        same property and makes the result identical everywhere.
        """
        import os
        saved = {k: v for k, v in os.environ.items() if "ODD" in k}
        try:
            for k in list(saved):
                os.environ.pop(k, None)
            os.environ["THE_ODDS_API_KEYS"] = "first,second,third"
            settings = cfg.load_settings()
            self.assertEqual(settings.odds_api_keys, ("first", "second", "third"))
            self.assertEqual(settings.odds_api_key, "first")
        finally:
            for k in list(saved):
                os.environ.pop(k, None)
            os.environ.update(saved)


class TestRotation(unittest.TestCase):
    def test_unknown_quota_keys_are_used_before_known_ones(self):
        pool = _pool(budget_daily=0, credit_stop=0)
        first = pool.pick()
        self.assertIsNone(first.remaining)
        pool.record_response(first, used=10, remaining=500)
        second = pool.pick()
        self.assertEqual(second.key, "k2", "must spread onto the unused key")

    def test_load_is_spread(self):
        pool = _pool(budget_daily=0, credit_stop=0, credit_warn=0)
        picks = []
        for _ in range(8):
            state = pool.pick()
            pool.record_use(state)
            picks.append(state.key)
        self.assertEqual(picks.count("k1"), 4)
        self.assertEqual(picks.count("k2"), 4)

    def test_headroom_decides_between_two_known_keys(self):
        pool = _pool(budget_daily=0, credit_stop=0, credit_warn=0)
        low, high = pool._states
        pool.record_response(low, used=490, remaining=10)
        pool.record_response(high, used=0, remaining=500)
        self.assertEqual(pool.pick().key, "k2")
        pool.record_response(high, used=495, remaining=5)
        pool.record_response(low, used=400, remaining=100)
        self.assertEqual(pool.pick().key, "k1")

    def test_a_key_at_the_floor_is_never_used(self):
        pool = _pool(credit_stop=20)
        spent, spare = pool._states
        pool.record_response(spent, used=480, remaining=20)
        pool.record_response(spare, used=0, remaining=500)
        for _ in range(5):
            self.assertEqual(pool.pick().key, "k2")

    def test_all_keys_exhausted_raises_with_a_reason(self):
        pool = _pool(credit_stop=20)
        for state in pool._states:
            pool.record_response(state, used=480, remaining=19)
        with self.assertRaises(QuotaExhausted) as ctx:
            pool.pick()
        self.assertIn("quota floor", str(ctx.exception))


class TestDailyBudget(unittest.TestCase):
    def test_budget_caps_requests_per_key_per_day(self):
        clock = _Clock()
        pool = _pool(budget_daily=3, credit_stop=0, credit_warn=0, now_fn=clock)
        for _ in range(3 * pool.size):
            state = pool.pick()
            pool.record_use(state)
            pool.record_response(state, used=1, remaining=400)
        with self.assertRaises(QuotaExhausted) as ctx:
            pool.pick()
        self.assertIn("daily budget", str(ctx.exception))
        self.assertEqual(pool.status().total_spent_today, 6)

    def test_budget_resets_on_the_next_utc_day(self):
        clock = _Clock()
        pool = _pool(budget_daily=2, credit_stop=0, credit_warn=0, now_fn=clock)
        for _ in range(2 * pool.size):
            pool.record_use(pool.pick())
        with self.assertRaises(QuotaExhausted):
            pool.pick()
        clock.advance_days(1)
        state = pool.pick()
        self.assertEqual(state.spent_today, 0, "a new day resets the counter")

    def test_zero_budget_means_unlimited(self):
        pool = _pool(budget_daily=0, credit_stop=0, credit_warn=0)
        for _ in range(50):
            pool.record_use(pool.pick())
        self.assertIsNotNone(pool.pick())

    def test_rate_limit_pauses_a_key(self):
        clock = _Clock()
        pool = _pool(budget_daily=0, credit_stop=0, credit_warn=0, now_fn=clock)
        first = pool.pick()
        pool.record_response(first, used=1, remaining=500)
        pool.record_error(first, rate_limited=True)
        self.assertEqual(pool.pick().key, "k2")
        clock.t += 301
        pool.record_response(pool._states[1], used=2, remaining=480)
        # Both usable again; the least-used/most-headroom wins.
        self.assertIn(pool.pick().key, ("k1", "k2"))


class TestStatus(unittest.TestCase):
    def test_status_reports_per_key_spend_without_secrets(self):
        pool = _pool(budget_daily=10, credit_warn=100, credit_stop=20)
        state = pool.pick()
        pool.record_use(state)
        pool.record_response(state, used=12, remaining=488)
        status: PoolStatus = pool.status()
        self.assertEqual(status.total_remaining, 488)
        self.assertEqual(status.total_spent_today, 1)
        self.assertEqual(status.daily_budget, 20)
        self.assertEqual(status.state, "ok")
        row = status.keys[0]
        self.assertNotIn("k1", str(row), "raw keys must never be reported")
        self.assertEqual(row["spent_today"], 1)
        self.assertTrue(row["usable"])

    def test_status_flips_to_exhausted(self):
        pool = _pool(credit_stop=20)
        for state in pool._states:
            pool.record_response(state, used=480, remaining=20)
        self.assertEqual(pool.status().state, "exhausted")
        self.assertTrue(pool.any_exhausted())

    def test_status_is_constrained_near_the_floor(self):
        pool = _pool(credit_warn=100, credit_stop=20)
        for i, state in enumerate(pool._states):
            pool.record_response(state, used=400 - i * 10, remaining=100 - i * 10)
        self.assertEqual(pool.status().state, "constrained")
        self.assertFalse(pool.any_exhausted())


class TestRotatingClient(unittest.TestCase):
    def test_client_requires_a_key(self):
        with self.assertRaises(ValueError):
            RotatingOddsClient([])

    def test_pool_keys_get_one_transport_each(self):
        client = RotatingOddsClient(["a" * 32, "b" * 32], budget_daily=5)
        self.assertEqual(len(client._transports), 2)
        self.assertEqual(client._transports[0].api_key, "a" * 32)
        self.assertEqual(client._transports[1].api_key, "b" * 32)
        self.assertEqual(client.pool.size, 2)

    def test_client_exposes_the_pool_shape_the_daemon_expects(self):
        client = RotatingOddsClient(["a" * 32, "b" * 32], budget_daily=5)
        self.assertIsNone(client.last_remaining)
        state = client.pool.pick()
        client.pool.record_response(state, used=5, remaining=495)
        self.assertEqual(client.last_remaining, 495)
        self.assertEqual(client.status().state, "ok")

    def test_exhausted_client_raises_before_any_request(self):
        client = RotatingOddsClient(["a" * 32], budget_daily=1, credit_stop=0, credit_warn=0)
        client.pool.record_use(client.pool.pick())
        with self.assertRaises(QuotaExhausted):
            client.get_odds("soccer_epl")


if __name__ == "__main__":
    unittest.main()


class _FakeTransport:
    """Stands in for OddsApiClient without network access.

    Deliberately exposes no ``last_used``/``last_remaining`` attributes unless a
    test sets them, which mirrors FixtureClient and any custom client.
    """

    def __init__(self, payload=None, *, remaining=None, used=None, boom=None):
        self._payload = payload if payload is not None else []
        self._remaining = remaining
        self._used = used
        self._boom = boom
        self.calls: list[str] = []
        if remaining is not None:
            self.last_remaining = remaining
        if used is not None:
            self.last_used = used

    def get_odds(self, sport_key, **kwargs):
        self.calls.append(sport_key)
        if self._boom:
            raise self._boom
        return self._payload

    def get_scores(self, sport_key, **kwargs):
        self.calls.append(f"scores:{sport_key}")
        if self._boom:
            raise self._boom
        return []


def _client(transports, **kw):
    client = RotatingOddsClient([f"k{i}x" * 8 for i in range(len(transports))], **kw)
    client._transports = list(transports)
    return client


class TestRotatingClientCalls(unittest.TestCase):
    """A successful fetch must never be discarded by the accounting layer."""

    def test_fetch_succeeds_against_a_client_without_quota_attributes(self):
        t = _FakeTransport(payload=[{"a": 1}])
        client = _client([t], budget_daily=10, credit_warn=0)
        # Regression: the pool used to read transport.last_used unguarded, so
        # every real request raised AttributeError after the fetch succeeded.
        self.assertEqual(client.get_odds("soccer_epl"), [{"a": 1}])
        self.assertEqual(t.calls, ["soccer_epl"])
        self.assertEqual(client.pool.status().total_spent_today, 1)
        self.assertIsNone(client.last_remaining)

    def test_quota_headers_are_recorded_when_present(self):
        t = _FakeTransport(payload=[], remaining=480, used=20)
        client = _client([t], budget_daily=10, credit_warn=0, credit_stop=20)
        client.get_odds("soccer_epl")
        status = client.status()
        self.assertEqual(status.total_remaining, 480)
        self.assertEqual(status.keys[0]["used"], 20)

    def test_a_failing_transport_is_counted_not_swallowed(self):
        t = _FakeTransport(boom=RuntimeError("upstream 500"))
        client = _client([t], budget_daily=10)
        with self.assertRaises(RuntimeError):
            client.get_odds("soccer_epl")
        row = client.status().keys[0]
        self.assertEqual(row["errors"], 1)
        self.assertEqual(row["requests"], 1)

    def test_spend_is_charged_even_when_the_call_fails(self):
        # Otherwise a failing upstream would be free, and a retry storm would
        # blow the daily budget with no trace.
        t = _FakeTransport(boom=RuntimeError("upstream 500"))
        client = _client([t], budget_daily=3, credit_warn=0)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                client.get_odds("soccer_epl")
        with self.assertRaises(QuotaExhausted):
            client.get_odds("soccer_epl")
        self.assertEqual(client.status().total_spent_today, 3)

    def test_rotation_spreads_requests_across_transports(self):
        ts = [_FakeTransport(), _FakeTransport()]
        client = _client(ts, budget_daily=10, credit_warn=0)
        for _ in range(6):
            client.get_odds("soccer_epl")
        self.assertEqual(len(ts[0].calls), 3)
        self.assertEqual(len(ts[1].calls), 3)

    def test_low_quota_warning_fires_without_raising(self):
        # Regression: the warn path touched a flag that was never initialised.
        t = _FakeTransport(payload=[], remaining=5, used=500)
        client = _client([t], budget_daily=100, credit_warn=50, credit_stop=20)
        with self.assertLogs("lisa.key_pool", level="WARNING"):
            client.get_odds("soccer_epl")
        # The warning is informational only, but the low reading also puts the
        # key under the floor, so the pool must refuse the next request.
        with self.assertRaises(QuotaExhausted):
            client.get_odds("soccer_epl")
