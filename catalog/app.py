from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from time import perf_counter
from urllib.parse import unquote, urlsplit

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


with Path("/app/data/events.json").open(encoding="utf-8") as events_file:
    EVENTS = json.load(events_file)



STATIC_INDEX = Path("/app/static/index.html")

catalog_requests = Counter(
    "catalog_requests",
    "Total number of requests handled by the catalog.",
    ("endpoint", "http_status"),
)
catalog_request_duration = Histogram(
    "catalog_request_duration_seconds",
    "Time spent handling catalog requests.",
)


class CatalogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/metrics":
            self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
            return

        started = perf_counter()
        content_type = "application/json; charset=utf-8"

        if path in ("/", "/index.html"):
            endpoint = "/"
            try:
                body = STATIC_INDEX.read_bytes()
                status = 200
                content_type = "text/html; charset=utf-8"
            except OSError:
                status = 404
                body = json.dumps({"error": "Dashboard not found"}).encode("utf-8")
        else:
            if path == "/events" :
                endpoint = "/events"
                status = 200
                result = EVENTS
            elif path.startswith("/events/"):
                endpoint = "/events/{id}"
                event_id = unquote(path.removeprefix("/events/"))
                result = next((event for event in EVENTS if event.get("id") == event_id), None)
                status = 200 if result is not None else 404
                if result is None:
                    result = {"error": "Event not found"}
            else:
                endpoint = "unknown"
                status = 404
                result = {"error": "Not found"}
            body = json.dumps(result).encode("utf-8")

        self._send(status, body, content_type)
        catalog_requests.labels(endpoint, str(status)).inc()
        catalog_request_duration.observe(perf_counter() - started)

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8001), CatalogHandler).serve_forever()