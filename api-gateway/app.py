from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


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


class GatewayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/metrics":
            self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
            return

        started = perf_counter()
        endpoint = "/events" if path == "/events" else "unknown"
        try:
            if path != "/events":
                status = 404
                body = b'{"error":"Not found"}'
            else:
                with urlopen("http://catalog:8001/events", timeout=5) as response:
                    body = response.read()
                    status = response.status
        except HTTPError as error:
            status = error.code
            body = error.read()
        except URLError:
            status = 502
            body = b'{"error":"Catalog service unavailable"}'

        self._send(status, body, "application/json; charset=utf-8")
        gateway_requests.labels("GET", endpoint, str(status)).inc()
        gateway_request_duration.labels(endpoint).observe(perf_counter() - started)

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8000), GatewayHandler).serve_forever()
