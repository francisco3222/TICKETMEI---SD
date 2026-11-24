"""Testes do circuit breaker e do retry com jitter (correm no CI: python -m unittest)."""

import unittest

from circuit_breaker import CLOSED, HALF_OPEN, OPEN, CallFailed, CircuitBreaker, CircuitOpenError, RetryPolicy


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def ok():
    return "ok"


def boom():
    raise CallFailed("HTTP 500", "500")


class CircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.transitions = []
        self.cb = CircuitBreaker(
            failure_rate_threshold=0.5, slow_call_rate_threshold=0.8, slow_call_seconds=1.0,
            window_size=10, min_calls=4, open_seconds=10, half_open_max_calls=2,
            on_state_change=lambda a, b: self.transitions.append((a, b)), clock=self.clock,
        )

    def fail(self, n):
        for _ in range(n):
            with self.assertRaises(CallFailed):
                self.cb.call(boom)

    def test_stays_closed_below_min_calls(self):
        self.fail(3)
        self.assertEqual(self.cb.state, CLOSED)

    def test_opens_on_failure_rate_and_fails_fast(self):
        self.cb.call(ok)
        self.cb.call(ok)
        self.fail(2)                                  # 2/4 = 50 % -> abre
        self.assertEqual(self.cb.state, OPEN)
        calls = []
        with self.assertRaises(CircuitOpenError):
            self.cb.call(lambda: calls.append(1))
        self.assertEqual(calls, [], "com o circuito aberto o Payment não pode ser chamado")

    def test_opens_on_slow_calls(self):
        def slow():
            self.clock.now += 2.0                     # 2 s > slow_call_seconds
            return "ok"
        for _ in range(4):
            self.cb.call(slow)
        self.assertEqual(self.cb.state, OPEN)

    def test_half_open_then_closed_after_successes(self):
        self.fail(4)
        self.clock.now += 10
        self.assertEqual(self.cb.state, HALF_OPEN)
        self.cb.call(ok)
        self.cb.call(ok)
        self.assertEqual(self.cb.state, CLOSED)
        self.assertEqual(self.transitions, [(CLOSED, OPEN), (OPEN, HALF_OPEN), (HALF_OPEN, CLOSED)])

    def test_half_open_failure_reopens(self):
        self.fail(4)
        self.clock.now += 10
        self.fail(1)
        self.assertEqual(self.cb.state, OPEN)
        self.clock.now += 9
        self.assertEqual(self.cb.state, OPEN, "novo período de espera completo")

    def test_half_open_limits_trial_calls(self):
        self.fail(4)
        self.clock.now += 10
        self.cb._acquire()                            # 2 pedidos de teste em voo
        self.cb._acquire()
        with self.assertRaises(CircuitOpenError):
            self.cb.call(ok)

    def test_client_errors_do_not_count(self):
        def bad_request():
            raise ValueError("400")
        for _ in range(6):
            with self.assertRaises(ValueError):
                self.cb.call(bad_request)
        self.assertEqual(self.cb.state, CLOSED)


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cb = CircuitBreaker(failure_rate_threshold=0.5, window_size=10, min_calls=4,
                                 open_seconds=10, clock=self.clock)
        self.sleeps = []
        self.retry = RetryPolicy(max_attempts=3, base_delay=0.1, max_delay=1.0,
                                 sleep=self.sleeps.append, rng=lambda: 0.5)

    def test_retry_succeeds_after_failure(self):
        attempts = iter([boom, ok])
        self.assertEqual(self.retry.run(self.cb, lambda: next(attempts)()), "ok")
        self.assertEqual(len(self.sleeps), 1)

    def test_full_jitter_bounds(self):
        policy = RetryPolicy(base_delay=0.1, max_delay=1.0, rng=lambda: 1.0)
        self.assertAlmostEqual(policy.backoff(1), 0.2)
        self.assertAlmostEqual(policy.backoff(5), 1.0)     # limitado por max_delay
        self.assertEqual(RetryPolicy(rng=lambda: 0.0).backoff(3), 0.0)

    def test_gives_up_after_max_attempts(self):
        with self.assertRaises(CallFailed):
            self.retry.run(self.cb, boom)
        self.assertEqual(len(self.sleeps), 2)              # 3 tentativas = 2 esperas

    def test_retries_stop_when_circuit_opens(self):
        self.cb.call(ok)
        self.cb.call(ok)
        self.fail_once()                                   # 1/3: ainda fechado
        calls = []

        def counted_boom():
            calls.append(1)
            boom()
        with self.assertRaises(CircuitOpenError):
            self.retry.run(self.cb, counted_boom)          # 1.ª falha abre (2/4) -> 2.ª tentativa não sai
        self.assertEqual(len(calls), 1)

    def fail_once(self):
        with self.assertRaises(CallFailed):
            self.cb.call(boom)


if __name__ == "__main__":
    unittest.main()
