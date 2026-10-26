import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

CATALOG_URL = os.getenv("CATALOG_URL", "http://catalog:8001")
ORDER_URL = os.getenv("ORDER_URL", "http://order:8002")
MAX_BODY_BYTES = 10_000

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

        started = perf_counter()
        endpoint, upstream = resolve(method, path)

        if upstream is None:
            status, body = 404, b'{"error":"Not found"}'
        else:
            if url.query:
                upstream += "?" + url.query  # reencaminha filtros, ex.: ?customer_id=c1
            status, body = self._forward(method, upstream)

        self._send(status, body, "application/json; charset=utf-8")
        gateway_requests.labels(method, endpoint, str(status)).inc()
        gateway_request_duration.labels(endpoint).observe(perf_counter() - started)

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
        headers["X-Forwarded-For"] = self.client_address[0]
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers), timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()
        except (URLError, TimeoutError, ConnectionError):
            service = url.split("//", 1)[1].split(":", 1)[0]
            return 502, json.dumps({"error": f"{service} service unavailable"}).encode("utf-8")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), GatewayHandler).serve_forever()
