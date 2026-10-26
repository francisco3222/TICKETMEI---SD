import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import urlopen

import pika
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

CATALOG_URL = os.getenv("CATALOG_URL", "http://catalog:8001")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/%2F")
QUEUE_NAME = os.getenv("ORDER_QUEUE", "order_created")
MAX_TICKETS_PER_ORDER = int(os.getenv("MAX_TICKETS_PER_ORDER", "4"))
MAX_BODY_BYTES = 10_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [order] %(levelname)s %(message)s")
log = logging.getLogger("order")

# ---------------------------------------------------------------- métricas
orders_initiated = Counter(
    "orders_initiated",
    "Orders created by the Order service, by resulting status.",
    ("status",),
)
order_requests = Counter(
    "order_requests",
    "Total number of requests handled by the Order service.",
    ("method", "endpoint", "http_status"),
)
order_request_duration = Histogram(
    "order_request_duration_seconds",
    "Time spent handling Order requests.",
    ("endpoint",),
)
order_messages_published = Counter(
    "order_messages_published",
    "OrderCreated messages published to RabbitMQ, by result.",
    ("result",),
)

# ---------------------------------------------------------------- dados (exclusivos deste serviço)
ORDERS = {}
ORDERS_LOCK = threading.Lock()


class ValidationError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- RabbitMQ
class Publisher:
    """Ligação única ao RabbitMQ. pika não é thread-safe, por isso o acesso é serializado com um lock."""

    def __init__(self, url, queue):
        self._params = pika.URLParameters(url)
        self._params.heartbeat = 30
        self._params.blocked_connection_timeout = 5
        self._queue = queue
        self._connection = None
        self._channel = None
        self._lock = threading.Lock()

    def _connect(self):
        self._connection = pika.BlockingConnection(self._params)
        self._channel = self._connection.channel()
        self._channel.queue_declare(queue=self._queue, durable=True)
        self._channel.confirm_delivery()

    def publish(self, message):
        body = json.dumps(message).encode("utf-8")
        props = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            type="OrderCreated",
            message_id=message["order_id"],
        )
        with self._lock:
            for attempt in (1, 2):  # uma nova tentativa se a ligação tiver caído
                try:
                    if self._connection is None or self._connection.is_closed:
                        self._connect()
                    self._channel.basic_publish(exchange="", routing_key=self._queue, body=body, properties=props)
                    return
                except pika.exceptions.AMQPError:
                    self._connection = None
                    if attempt == 2:
                        raise


publisher = Publisher(RABBITMQ_URL, QUEUE_NAME)


# ---------------------------------------------------------------- lógica de negócio
def fetch_event(event_id):
    try:
        with urlopen(f"{CATALOG_URL}/events/{quote(event_id, safe='')}", timeout=3) as response:
            return json.loads(response.read())
    except HTTPError as error:
        if error.code == 404:
            raise ValidationError(404, f"Event '{event_id}' not found")
        raise ValidationError(502, "Catalog service error")
    except (URLError, TimeoutError, ConnectionError):
        raise ValidationError(503, "Catalog service unavailable")


def validate(payload):
    if not isinstance(payload, dict):
        raise ValidationError(400, "Body must be a JSON object")
    customer_id = payload.get("customer_id")
    event_id = payload.get("event_id")
    quantity = payload.get("quantity")
    email = payload.get("email")

    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValidationError(400, "customer_id is required")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValidationError(400, "event_id is required")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise ValidationError(400, "quantity must be a positive integer")
    if quantity > MAX_TICKETS_PER_ORDER:
        raise ValidationError(400, f"quantity cannot exceed {MAX_TICKETS_PER_ORDER} tickets per order")
    if not isinstance(email, str) or not EMAIL_RE.match(email):
        raise ValidationError(400, "a valid email is required")
    return customer_id.strip(), event_id.strip(), quantity, email.strip()


def create_order(payload):
    customer_id, event_id, quantity, email = validate(payload)
    event = fetch_event(event_id)
    price = float(event.get("price_eur", 0))

    order = {
        "id": str(uuid.uuid4()),
        "customer_id": customer_id,
        "event_id": event_id,
        "email": email,
        "quantity": quantity,
        "total_amount": round(price * quantity, 2),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING",
    }
    with ORDERS_LOCK:
        ORDERS[order["id"]] = order

    message = {
        "type": "OrderCreated",
        "order_id": order["id"],
        "customer_id": customer_id,
        "event_id": event_id,
        "email": email,
        "quantity": quantity,
        "total_amount": order["total_amount"],
        "created_at": order["created_at"],
    }
    try:
        publisher.publish(message)
        order_messages_published.labels("success").inc()
    except Exception:  # noqa: BLE001 - qualquer falha no broker torna a compra FAILED
        log.exception("Failed to publish OrderCreated for %s", order["id"])
        order_messages_published.labels("error").inc()
        with ORDERS_LOCK:
            order["status"] = "FAILED"
        orders_initiated.labels("FAILED").inc()
        return 503, order

    orders_initiated.labels("PENDING").inc()
    log.info("Order %s PENDING (%s x %s)", order["id"], quantity, event_id)
    return 202, order


# ---------------------------------------------------------------- HTTP
class OrderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        if path == "/metrics":
            self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
            return

        started = perf_counter()
        if path == "/orders":
            # Lista as compras; ?customer_id=c1 filtra por cliente, ?status=PENDING por estado
            endpoint = "/orders"
            query = parse_qs(url.query)
            customer_id = query.get("customer_id", [None])[0]
            wanted_status = query.get("status", [None])[0]
            with ORDERS_LOCK:
                result = [
                    dict(o) for o in ORDERS.values()
                    if (customer_id is None or o["customer_id"] == customer_id)
                    and (wanted_status is None or o["status"] == wanted_status.upper())
                ]
            result.sort(key=lambda o: o["created_at"], reverse=True)
            status = 200
        elif path.startswith("/orders/"):
            endpoint = "/orders/{id}"
            with ORDERS_LOCK:
                order = ORDERS.get(path.removeprefix("/orders/"))
                order = dict(order) if order else None
            status, result = (200, order) if order else (404, {"error": "Order not found"})
        elif path == "/health":
            endpoint, status, result = "/health", 200, {"status": "ok"}
        else:
            endpoint, status, result = "unknown", 404, {"error": "Not found"}
        self._finish("GET", endpoint, status, result, started)

    def do_POST(self):
        started = perf_counter()
        path = urlsplit(self.path).path
        if path != "/orders":
            self._finish("POST", "unknown", 404, {"error": "Not found"}, started)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ValidationError(413, "Payload too large")
            try:
                payload = json.loads(self.rfile.read(length) or b"null")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValidationError(400, "Invalid JSON")
            status, result = create_order(payload)
        except ValidationError as error:
            status, result = error.status, {"error": error.message}
        self._finish("POST", "/orders", status, result, started)

    def _finish(self, method, endpoint, status, result, started):
        self._send(status, json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")
        order_requests.labels(method, endpoint, str(status)).inc()
        order_request_duration.labels(endpoint).observe(perf_counter() - started)

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    for status in ("PENDING", "CONFIRMED", "CANCELLED", "FAILED"):
        orders_initiated.labels(status)  # séries a 0 desde o arranque
    log.info("Order service listening on :8002 (queue=%s)", QUEUE_NAME)
    ThreadingHTTPServer(("0.0.0.0", 8002), OrderHandler).serve_forever()
