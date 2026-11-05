import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import redis
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from ratelimiter import RedisRateLimiter

CATALOG_URL = os.getenv("CATALOG_URL", "http://catalog:8001")
ORDER_URL = os.getenv("ORDER_URL", "http://order:8002")
MAX_BODY_BYTES = 10_000

# ---- Rate limiter (RNF3): 5 pedidos/s por IP em POST /orders, por defeito
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = float(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "1"))
# Só confiar no X-Forwarded-For quando o gateway está atrás de um proxy/ingress conhecido;
# caso contrário qualquer cliente fugia ao limite inventando o cabeçalho.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"
# (método, endpoint) protegidos pelo limiter
RATE_LIMITED = {("POST", "/orders")}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [gateway] %(levelname)s %(message)s")
log = logging.getLogger("gateway")

rate_limiter = (
    RedisRateLimiter(REDIS_URL, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS) if RATE_LIMIT_ENABLED else None
)

gateway_requests = Counter(
    "gateway_requests",
    "Total number of requests handled by the API gateway.",
    ("method", "endpoint", "http_status"),
)
gateway_request_duration = Histogram(
    "gateway_request_duration_seconds",
    "Time spent handling API gateway requests.",
    ("endpoint",),
)
gateway_rate_limited = Counter(
    "gateway_rate_limited",
    "Requests rejected with HTTP 429 by the Redis rate limiter.",
    ("endpoint",),
)
gateway_rate_limiter_errors = Counter(
    "gateway_rate_limiter_errors",
    "Redis errors while checking the rate limit (request was allowed: fail-open).",
)

# (método, regex do caminho, label do endpoint, URL base do serviço de destino)
ROUTES = [
    ("GET", re.compile(r"^/events$"), "/events", CATALOG_URL),
    ("GET", re.compile(r"^/events/[^/]+$"), "/events/{id}", CATALOG_URL),
    ("GET", re.compile(r"^/orders$"), "/orders", ORDER_URL),
    ("POST", re.compile(r"^/orders?$"), "/orders", ORDER_URL),
    ("GET", re.compile(r"^/orders/[^/]+$"), "/orders/{id}", ORDER_URL),
]


def resolve(method, path):
    for route_method, pattern, endpoint, base_url in ROUTES:
        if pattern.match(path):
            if route_method != method:
                continue
            # /order (singular) é aceite por compatibilidade com o RNF3, mas reencaminhado para /orders
            upstream_path = "/orders" if endpoint == "/orders" else path
            return endpoint, base_url + upstream_path
    return "unknown", None


class GatewayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        url = urlsplit(self.path)
        path = url.path
        if method == "GET" and path == "/metrics":
            self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
            return
        if method == "GET" and path == "/health":
            self._send(200, b'{"status":"ok"}', "application/json")
            return

        started = perf_counter()
        endpoint, upstream = resolve(method, path)
        extra_headers = {}

        limit = self._rate_limit(method, endpoint) if upstream else None
        if limit is not None:
            extra_headers = {
                "X-RateLimit-Limit": str(limit.limit),
                "X-RateLimit-Remaining": str(limit.remaining),
            }

        if upstream is None:
            status, body = 404, b'{"error":"Not found"}'
        elif limit is not None and not limit.allowed:
            status = 429
            body = json.dumps({
                "error": "Too Many Requests",
                "detail": f"Limite de {limit.limit} pedidos por {RATE_LIMIT_WINDOW_SECONDS:g}s por cliente",
            }).encode("utf-8")
            extra_headers["Retry-After"] = str(limit.retry_after)
            gateway_rate_limited.labels(endpoint).inc()
        else:
            if url.query:
                upstream += "?" + url.query  # reencaminha filtros, ex.: ?customer_id=c1
            status, body = self._forward(method, upstream)

        self._send(status, body, "application/json; charset=utf-8", extra_headers)
        gateway_requests.labels(method, endpoint, str(status)).inc()
        gateway_request_duration.labels(endpoint).observe(perf_counter() - started)

    def _client_ip(self):
        if TRUST_PROXY_HEADERS:
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _rate_limit(self, method, endpoint):
        if rate_limiter is None or (method, endpoint) not in RATE_LIMITED:
            return None
        try:
            return rate_limiter.check(self._client_ip(), endpoint)
        except redis.RedisError as error:
            gateway_rate_limiter_errors.inc()
            log.warning("Rate limiter unavailable (%s): allowing request", error.__class__.__name__)
            return None

    def _forward(self, method, url):
        data = None
        headers = {"Accept": "application/json"}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return 413, b'{"error":"Payload too large"}'
            data = self.rfile.read(length) if length else b""
            headers["Content-Type"] = "application/json"
        # Preserva o IP original do cliente (útil para o rate limiter da fase 3)
        headers["X-Forwarded-For"] = self._client_ip()
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers), timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()
        except (URLError, TimeoutError, ConnectionError):
            service = url.split("//", 1)[1].split(":", 1)[0]
            return 502, json.dumps({"error": f"{service} service unavailable"}).encode("utf-8")

    def _send(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    for ep in {e for _, e in RATE_LIMITED}:
        gateway_rate_limited.labels(ep)  # série a 0 desde o arranque
    if rate_limiter:
        log.info("Rate limiter ON: %s req / %gs per IP on %s (redis=%s)",
                 RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS, sorted(RATE_LIMITED), REDIS_URL)
    ThreadingHTTPServer(("0.0.0.0", 8000), GatewayHandler).serve_forever()
