"""Rate limiter distribuído baseado em Redis (RNF3).

Algoritmo: janela fixa por IP de cliente.
  - Cada IP tem um contador por janela (por defeito 1 s) guardado no Redis:
      chave = ratelimit:<endpoint>:<ip>:<n.º da janela>
  - INCR + PEXPIRE são executados num script Lua, por isso a operação é atómica
    mesmo com várias réplicas do API Gateway a partilhar o mesmo Redis.
  - Se o contador passar o limite, o pedido é rejeitado com HTTP 429.

Porque Redis e não um dicionário em memória? No Kubernetes o gateway corre com
várias réplicas; um contador em memória seria por réplica, e um cliente podia
fazer N x limite pedidos. Com Redis o limite é global.

Se o Redis estiver em baixo o limiter "falha aberto" (deixa passar) para não
derrubar a loja inteira; o erro fica registado na métrica
gateway_rate_limiter_errors_total.
"""

import math
import time

import redis

# Devolve {contador_atual, ms_até_a_janela_expirar}
_LUA_FIXED_WINDOW = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('PTTL', KEYS[1])
return {current, ttl}
"""


class RateLimitResult:
    __slots__ = ("allowed", "limit", "remaining", "retry_after")

    def __init__(self, allowed, limit, remaining, retry_after):
        self.allowed = allowed
        self.limit = limit
        self.remaining = remaining
        self.retry_after = retry_after


class RedisRateLimiter:
    def __init__(self, redis_url, limit, window_seconds, prefix="ratelimit"):
        self.limit = int(limit)
        self.window_ms = int(float(window_seconds) * 1000)
        self.prefix = prefix
        self._redis = redis.Redis.from_url(
            redis_url, socket_connect_timeout=0.5, socket_timeout=0.5, health_check_interval=30
        )
        self._script = self._redis.register_script(_LUA_FIXED_WINDOW)

    def check(self, client_ip, endpoint):
        """Lança redis.RedisError se o Redis não responder (quem chama decide o que fazer)."""
        window = int(time.time() * 1000) // self.window_ms
        key = f"{self.prefix}:{endpoint}:{client_ip}:{window}"
        current, ttl_ms = self._script(keys=[key], args=[self.window_ms])
        current = int(current)
        ttl_ms = int(ttl_ms) if int(ttl_ms) > 0 else self.window_ms
        allowed = current <= self.limit
        return RateLimitResult(
            allowed=allowed,
            limit=self.limit,
            remaining=max(self.limit - current, 0),
            retry_after=max(1, math.ceil(ttl_ms / 1000)),
        )

    def ping(self):
        return self._redis.ping()
