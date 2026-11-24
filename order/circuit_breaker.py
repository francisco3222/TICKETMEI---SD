"""Circuit breaker + retry com jitter para as chamadas Order -> Payment (RNF4).

Estados (gauge circuit_breaker_state):
  0 CLOSED     – os pedidos passam; cada resultado entra numa janela deslizante.
  1 OPEN       – fail-fast: nenhum pedido chega ao Payment; executa-se o fallback.
  2 HALF_OPEN  – depois de open_seconds deixa passar poucos pedidos de teste.

Quando abre (CLOSED -> OPEN), olhando para os últimos `window_size` pedidos
(e só depois de pelo menos `min_calls`):
  - taxa de falhas  >= failure_rate_threshold   (erro 5xx, timeout, ligação recusada), OU
  - taxa de pedidos lentos >= slow_call_rate_threshold (mais lentos que slow_call_seconds,
    mesmo que tenham tido sucesso)  -> limite de LATÊNCIA.

HALF_OPEN: permite `half_open_max_calls` pedidos de teste.
  - todos com sucesso -> CLOSED;   - um falha -> OPEN outra vez (novo período de espera).

Retry com jitter ("full jitter"): uma tentativa falhada é repetida depois de
random(0, min(cap, base * 2^tentativa)) segundos, até `max_attempts`. Cada tentativa
conta para o circuit breaker; se ele abrir entre tentativas, os retries param logo.
"""

import random
import threading
import time
from collections import deque

CLOSED, OPEN, HALF_OPEN = 0, 1, 2
STATE_NAMES = {CLOSED: "CLOSED", OPEN: "OPEN", HALF_OPEN: "HALF_OPEN"}


class CircuitOpenError(Exception):
    """O circuito está aberto: o pedido nem foi enviado (fail-fast)."""


class CallFailed(Exception):
    """A chamada falhou de forma que conta para o circuito (5xx, timeout, rede)."""

    def __init__(self, reason, status_label):
        super().__init__(reason)
        self.reason = reason
        self.status_label = status_label   # "500", "503", "timeout", "connection_error"


class CircuitBreaker:
    def __init__(self, *, failure_rate_threshold=0.5, slow_call_rate_threshold=0.8,
                 slow_call_seconds=2.0, window_size=20, min_calls=10, open_seconds=10.0,
                 half_open_max_calls=3, on_state_change=None, clock=time.monotonic):
        self.failure_rate_threshold = failure_rate_threshold
        self.slow_call_rate_threshold = slow_call_rate_threshold
        self.slow_call_seconds = slow_call_seconds
        self.min_calls = min_calls
        self.open_seconds = open_seconds
        self.half_open_max_calls = half_open_max_calls
        self._on_state_change = on_state_change
        self._clock = clock
        self._lock = threading.Lock()
        self._window = deque(maxlen=window_size)   # (falhou?, lento?)
        self._state = CLOSED
        self._opened_at = 0.0
        self._half_open_in_flight = 0
        self._half_open_successes = 0

    # ------------------------------------------------------------ estado
    @property
    def state(self):
        with self._lock:
            self._maybe_half_open()
            return self._state

    def snapshot(self):
        with self._lock:
            self._maybe_half_open()
            calls = len(self._window)
            failures = sum(1 for f, _ in self._window if f)
            slow = sum(1 for _, s in self._window if s)
            remaining = max(0.0, self.open_seconds - (self._clock() - self._opened_at)) if self._state == OPEN else 0
            return {
                "state": STATE_NAMES[self._state],
                "window_calls": calls,
                "failure_rate": round(failures / calls, 3) if calls else 0.0,
                "slow_call_rate": round(slow / calls, 3) if calls else 0.0,
                "open_remaining_seconds": round(remaining, 1),
                "config": {
                    "failure_rate_threshold": self.failure_rate_threshold,
                    "slow_call_rate_threshold": self.slow_call_rate_threshold,
                    "slow_call_seconds": self.slow_call_seconds,
                    "window_size": self._window.maxlen,
                    "min_calls": self.min_calls,
                    "open_seconds": self.open_seconds,
                    "half_open_max_calls": self.half_open_max_calls,
                },
            }

    def _transition(self, new_state):
        old = self._state
        if old == new_state:
            return
        self._state = new_state
        if new_state == OPEN:
            self._opened_at = self._clock()
        if new_state in (OPEN, HALF_OPEN, CLOSED):
            self._half_open_in_flight = 0
            self._half_open_successes = 0
        if new_state == CLOSED:
            self._window.clear()
        if self._on_state_change:
            self._on_state_change(old, new_state)

    def _maybe_half_open(self):
        if self._state == OPEN and self._clock() - self._opened_at >= self.open_seconds:
            self._transition(HALF_OPEN)

    # ------------------------------------------------------------ uma chamada
    def _acquire(self):
        with self._lock:
            self._maybe_half_open()
            if self._state == OPEN:
                raise CircuitOpenError()
            if self._state == HALF_OPEN:
                if self._half_open_in_flight >= self.half_open_max_calls:
                    raise CircuitOpenError()          # só alguns pedidos de teste
                self._half_open_in_flight += 1
            return self._state

    def _record(self, acquired_state, failed, slow):
        with self._lock:
            if acquired_state == HALF_OPEN:
                if self._state != HALF_OPEN:
                    return                            # outro pedido de teste já decidiu
                self._half_open_in_flight -= 1
                if failed or slow:
                    self._transition(OPEN)
                else:
                    self._half_open_successes += 1
                    if self._half_open_successes >= self.half_open_max_calls:
                        self._transition(CLOSED)
                return
            if self._state != CLOSED:
                return
            self._window.append((failed, slow))
            calls = len(self._window)
            if calls < self.min_calls:
                return
            failure_rate = sum(1 for f, _ in self._window if f) / calls
            slow_rate = sum(1 for _, s in self._window if s) / calls
            if failure_rate >= self.failure_rate_threshold or slow_rate >= self.slow_call_rate_threshold:
                self._transition(OPEN)

    def call(self, fn):
        """Executa fn() protegido. Lança CircuitOpenError (fail-fast) ou CallFailed."""
        acquired_state = self._acquire()
        started = self._clock()
        try:
            result = fn()
        except CallFailed:
            self._record(acquired_state, failed=True, slow=False)
            raise
        except Exception:
            # erro que não é culpa do Payment (ex.: 4xx): não conta para o circuito
            self._record(acquired_state, failed=False, slow=False)
            raise
        slow = (self._clock() - started) >= self.slow_call_seconds
        self._record(acquired_state, failed=False, slow=slow)
        return result


class RetryPolicy:
    def __init__(self, max_attempts=3, base_delay=0.1, max_delay=1.0, sleep=time.sleep, rng=random.random):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._rng = rng

    def backoff(self, attempt):
        """Full jitter: aleatório entre 0 e min(cap, base * 2^tentativa)."""
        return self._rng() * min(self.max_delay, self.base_delay * (2 ** attempt))

    def run(self, breaker, fn, on_retry=None):
        """Tenta até max_attempts. Pára imediatamente se o circuito abrir (CircuitOpenError)."""
        last_error = None
        for attempt in range(self.max_attempts):
            if attempt:
                delay = self.backoff(attempt)
                if on_retry:
                    on_retry(attempt, delay, last_error)
                self._sleep(delay)
            try:
                return breaker.call(fn)
            except CallFailed as error:
                last_error = error
            # CircuitOpenError não é apanhado aqui: sai logo, sem mais retries
        raise last_error
