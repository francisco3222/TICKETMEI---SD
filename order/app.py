import json
import logging
import os
import re
import socket
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

import pika
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from circuit_breaker import (
    CLOSED, OPEN, STATE_NAMES, CallFailed, CircuitBreaker, CircuitOpenError, RetryPolicy,
)

CATALOG_URL = os.getenv("CATALOG_URL", "http://catalog:8001")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8004")
PAYMENT_TIMEOUT_SECONDS = float(os.getenv("PAYMENT_TIMEOUT_SECONDS", "2"))
MAX_TICKETS_PER_ORDER = int(os.getenv("MAX_TICKETS_PER_ORDER", "4"))
MAX_BODY_BYTES = 10_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# RabbitMQ: OrderCreated vai para o exchange orders.exchange (enunciado, Parte II)
ORDERS_EXCHANGE = os.getenv("ORDERS_EXCHANGE", "orders.exchange")
ORDER_CREATED_ROUTING_KEY = os.getenv("ORDER_CREATED_ROUTING_KEY", "order.created")
QUEUE_NAME = os.getenv("ORDER_QUEUE", "order_created")

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
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15),
)
order_messages_published = Counter(
    "order_messages_published",
    "OrderCreated messages published to RabbitMQ, by result.",
    ("result",),
)
# --- circuit breaker (nomes pedidos no enunciado)
circuit_breaker_state = Gauge(
    "circuit_breaker_state", "Circuit breaker Order->Payment: 0=Closed, 1=Open, 2=Half-Open.",
)
circuit_breaker_fallbacks = Counter(
    "circuit_breaker_fallbacks", "Requests answered by the fallback without calling Payment (circuit open).",
)
payment_client_requests = Counter(
    "payment_client_requests",
    "Requests actually sent from Order to Payment, by outcome (2xx, 500, timeout, ...). "
    "Not incremented while the circuit is OPEN.",
    ("status",),
)
# --- extra: ajudam a medir o tempo de transição e o efeito do retry
circuit_breaker_transitions = Counter(
    "circuit_breaker_transitions", "Circuit breaker state transitions.", ("from_state", "to_state"),
)
payment_client_duration = Histogram(
    "payment_client_request_duration_seconds",
    "Duration of each HTTP call from Order to Payment (one attempt).",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 1.5, 2, 3, 5),
)
payment_retries = Counter("payment_retries", "Retries (with jitter) of calls to Payment.")


def _on_state_change(old, new):
    circuit_breaker_state.set(new)
    circuit_breaker_transitions.labels(STATE_NAMES[old], STATE_NAMES[new]).inc()
    level = logging.WARNING if new == OPEN else logging.INFO
    log.log(level, "Circuit breaker %s -> %s", STATE_NAMES[old], STATE_NAMES[new])


breaker = CircuitBreaker(
    failure_rate_threshold=float(os.getenv("CB_FAILURE_RATE_THRESHOLD", "0.5")),
    slow_call_rate_threshold=float(os.getenv("CB_SLOW_CALL_RATE_THRESHOLD", "0.8")),
    slow_call_seconds=float(os.getenv("CB_SLOW_CALL_SECONDS", "1.5")),
    window_size=int(os.getenv("CB_WINDOW_SIZE", "20")),
    min_calls=int(os.getenv("CB_MIN_CALLS", "10")),
    open_seconds=float(os.getenv("CB_OPEN_SECONDS", "10")),
    half_open_max_calls=int(os.getenv("CB_HALF_OPEN_MAX_CALLS", "3")),
    on_state_change=_on_state_change,
)
retry_policy = RetryPolicy(
    max_attempts=int(os.getenv("RETRY_MAX_ATTEMPTS", "3")),
    base_delay=float(os.getenv("RETRY_BASE_DELAY_SECONDS", "0.1")),
    max_delay=float(os.getenv("RETRY_MAX_DELAY_SECONDS", "1.0")),
)

# ---------------------------------------------------------------- dados (exclusivos deste serviço)
ORDERS = {}
ORDERS_LOCK = threading.Lock()


class ValidationError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class PaymentRejected(Exception):
    """Payment respondeu 4xx: erro do pedido, não do Payment -> não conta para o circuito."""


# ---------------------------------------------------------------- RabbitMQ
def rabbitmq_parameters():
    """RABBITMQ_URL (local) ou RABBITMQ_HOST/VHOST/USER/PASSWORD (cluster global, via Secret)."""
    host = os.getenv("RABBITMQ_HOST")
    if host:
        params = pika.ConnectionParameters(
            host=host,
            port=int(os.getenv("RABBITMQ_PORT", "5672")),
            virtual_host=os.getenv("RABBITMQ_VHOST", "/"),
            credentials=pika.PlainCredentials(os.getenv("RABBITMQ_USER", "guest"),
                                              os.getenv("RABBITMQ_PASSWORD", "guest")),
        )
    else:
        params = pika.URLParameters(os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/%2F"))
    params.heartbeat = 30
    params.blocked_connection_timeout = 5
    params.socket_timeout = 5
    return params


class Publisher:
    """Ligação única ao RabbitMQ. pika não é thread-safe, por isso o acesso é serializado com um lock.

    Declara o exchange orders.exchange (topic), a fila order_created e a ligação entre os dois,
    para que nenhuma mensagem se perca se o Notification ainda não tiver arrancado."""

    def __init__(self):
        self._params = rabbitmq_parameters()
        self._connection = None
        self._channel = None
        self._lock = threading.Lock()

    def _connect(self):
        self._connection = pika.BlockingConnection(self._params)
        self._channel = self._connection.channel()
        self._channel.exchange_declare(exchange=ORDERS_EXCHANGE, exchange_type="topic", durable=True)
        self._channel.queue_declare(queue=QUEUE_NAME, durable=True)
        self._channel.queue_bind(queue=QUEUE_NAME, exchange=ORDERS_EXCHANGE, routing_key=ORDER_CREATED_ROUTING_KEY)
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
                    self._channel.basic_publish(
                        exchange=ORDERS_EXCHANGE, routing_key=ORDER_CREATED_ROUTING_KEY, body=body, properties=props,
                    )
                    return
                except (pika.exceptions.AMQPError, OSError):
                    self._connection = None
                    if attempt == 2:
                        raise


publisher = Publisher()


# ---------------------------------------------------------------- Payment (síncrono, protegido)
def _call_payment_once(order):
    """Uma tentativa HTTP. Lança CallFailed (conta para o circuito) ou PaymentRejected (não conta)."""
    body = json.dumps({"order_id": order["id"], "amount": order["total_amount"]}).encode("utf-8")
    request = Request(
        f"{PAYMENT_URL}/payments", data=body, method="POST",
        headers={"Content-Type": "application/json", "Idempotency-Key": order["id"]},
    )
    started = perf_counter()
    try:
        with urlopen(request, timeout=PAYMENT_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read())
        payment_client_requests.labels("2xx").inc()
        return result
    except HTTPError as error:
        if error.code >= 500:
            payment_client_requests.labels(str(error.code)).inc()
            raise CallFailed(f"Payment returned HTTP {error.code}", str(error.code))
        payment_client_requests.labels(str(error.code)).inc()
        raise PaymentRejected(f"Payment rejected the request (HTTP {error.code})")
    except (TimeoutError, socket.timeout):
        payment_client_requests.labels("timeout").inc()
        raise CallFailed(f"Payment timed out after {PAYMENT_TIMEOUT_SECONDS}s", "timeout")
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            payment_client_requests.labels("timeout").inc()
            raise CallFailed(f"Payment timed out after {PAYMENT_TIMEOUT_SECONDS}s", "timeout")
        payment_client_requests.labels("connection_error").inc()
        raise CallFailed(f"Payment unreachable ({error.reason})", "connection_error")
    except (ConnectionError, OSError) as error:
        payment_client_requests.labels("connection_error").inc()
        raise CallFailed(f"Payment unreachable ({error.__class__.__name__})", "connection_error")
    finally:
        payment_client_duration.observe(perf_counter() - started)


def _on_retry(attempt, delay, error):
    payment_retries.inc()
    log.info("Retry %s to Payment in %.0f ms (%s)", attempt, delay * 1000, error.reason if error else "?")


def charge(order):
    """Retry com jitter dentro do circuit breaker. Lança CircuitOpenError, CallFailed ou PaymentRejected."""
    return retry_policy.run(breaker, lambda: _call_payment_once(order), on_retry=_on_retry)


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


def _set(order, **fields):
    with ORDERS_LOCK:
        order.update(fields)
        return dict(order)


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
        "payment_id": None,
        "failure_reason": None,
    }
    with ORDERS_LOCK:
        ORDERS[order["id"]] = order

    # 1) pagamento síncrono dentro do circuit breaker
    try:
        payment = charge(order)
    except CircuitOpenError:
        circuit_breaker_fallbacks.inc()
        orders_initiated.labels("FAILED").inc()
        _set(order, status="FAILED", failure_reason="circuit_open")
        return 503, {"error": "Payment system unavailable, try again later"}
    except CallFailed as error:
        orders_initiated.labels("FAILED").inc()
        result = _set(order, status="FAILED", failure_reason=error.reason)
        return 502, {"error": "Payment failed", "detail": error.reason, "order": result}
    except PaymentRejected as error:
        orders_initiated.labels("FAILED").inc()
        result = _set(order, status="FAILED", failure_reason=str(error))
        return 422, {"error": "Payment rejected", "detail": str(error), "order": result}

    # 2) pagamento aceite -> CONFIRMED
    result = _set(order, status="CONFIRMED", payment_id=payment.get("id"))
    orders_initiated.labels("CONFIRMED").inc()

    # 3) OrderCreated -> orders.exchange (Notification envia o email)
    message = {
        "type": "OrderCreated",
        "order_id": order["id"],
        "customer_id": customer_id,
        "event_id": event_id,
        "email": email,
        "quantity": quantity,
        "total_amount": order["total_amount"],
        "payment_id": result["payment_id"],
        "created_at": order["created_at"],
    }
    try:
        publisher.publish(message)
        order_messages_published.labels("success").inc()
    except Exception:  # noqa: BLE001
        # O cliente já pagou: a compra continua CONFIRMED; só a notificação falhou.
        # (Numa versão mais robusta usava-se o padrão Outbox para reenviar mais tarde.)
        log.exception("Order %s CONFIRMED but OrderCreated could not be published", order["id"])
        order_messages_published.labels("error").inc()

    log.info("Order %s CONFIRMED (%s x %s, %.2f EUR)", order["id"], quantity, event_id, order["total_amount"])
    return 201, result


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
            # Lista as compras; ?customer_id=c1 filtra por cliente, ?status=CONFIRMED por estado
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
        elif path == "/circuit-breaker":
            endpoint, status, result = "/circuit-breaker", 200, breaker.snapshot()
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
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512     # aguenta rajadas de 50+ pedidos/s sem recusar ligações


if __name__ == "__main__":
    for status in ("PENDING", "CONFIRMED", "CANCELLED", "FAILED"):
        orders_initiated.labels(status)          # séries a 0 desde o arranque
    for status in ("2xx", "500", "timeout", "connection_error"):
        payment_client_requests.labels(status)
    circuit_breaker_state.set(CLOSED)
    log.info("Order service on :8002 (payment=%s, timeout=%ss, exchange=%s)",
             PAYMENT_URL, PAYMENT_TIMEOUT_SECONDS, ORDERS_EXCHANGE)
    log.info("Circuit breaker config: %s", breaker.snapshot()["config"])
    Server(("0.0.0.0", 8002), OrderHandler).serve_forever()
