"""Payment service – simula um sistema de pagamentos externo, com injeção de falhas.

Endpoints
  POST /payments                    {"order_id", "amount"}  -> 201 SUCCESS | 500 FAILED (falha injetada)
  GET  /payments/{id}               detalhes de um pagamento
  GET  /payments?order_id=...       pagamentos de uma compra
  POST /payments/configure-faults   {"error_rate": 0.8, "latency_ms": 10000}
  GET  /payments/configure-faults   configuração atual das falhas
  GET  /health, GET /metrics

Idempotência: o Order repete pedidos (retry). Se a 1.ª tentativa chegou a cobrar mas a resposta
se perdeu (timeout), a 2.ª não pode cobrar outra vez. Por isso um pagamento SUCCESS já existente
para o mesmo order_id é devolvido (200) em vez de criar outro.

NOTA: a configuração de falhas é por processo. No Kubernetes o Payment corre com 1 réplica para
que POST /payments/configure-faults afete todos os pedidos.
"""

import json
import logging
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.parse import parse_qs, urlsplit

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

PORT = int(os.getenv("PORT", "8004"))
MAX_BODY_BYTES = 10_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [payment] %(levelname)s %(message)s")
log = logging.getLogger("payment")

# Com latência injetada de 10 s e 50 pedidos/s ficam centenas de threads a dormir:
# uma stack mais pequena mantém a memória baixa.
threading.stack_size(512 * 1024)

# ---------------------------------------------------------------- métricas (pedidas no enunciado + extra)
payment_requests = Counter(
    "payment_requests",
    "Requests to POST /payments, by HTTP status returned.",
    ("http_status",),
)
payment_processing_duration = Histogram(
    "payment_processing_duration_seconds",
    "Time spent processing a payment (includes injected latency).",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 12.5, 15, 20, 30),
)
payment_fault_error_rate = Gauge("payment_fault_error_rate", "Injected error rate (0-1).")
payment_fault_latency_seconds = Gauge("payment_fault_latency_seconds", "Injected latency in seconds.")
payment_amount_total = Counter("payment_amount_euros", "Sum of successfully charged amounts (EUR).")

# ---------------------------------------------------------------- injeção de falhas
FAULTS = {
    "error_rate": float(os.getenv("FAULT_ERROR_RATE", "0")),
    "latency_ms": int(os.getenv("FAULT_LATENCY_MS", "0")),
    "latency_jitter_ms": int(os.getenv("FAULT_LATENCY_JITTER_MS", "0")),
}
FAULTS_LOCK = threading.Lock()


def _publish_fault_gauges():
    payment_fault_error_rate.set(FAULTS["error_rate"])
    payment_fault_latency_seconds.set(FAULTS["latency_ms"] / 1000)


def configure_faults(payload):
    if not isinstance(payload, dict):
        raise ValueError("Body must be a JSON object")
    new = {}
    if "error_rate" in payload:
        rate = payload["error_rate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ValueError("error_rate must be a number")
        if rate > 1:          # aceita 80 como 80 %
            rate = rate / 100
        if not 0 <= rate <= 1:
            raise ValueError("error_rate must be between 0 and 1 (or 0-100 %)")
        new["error_rate"] = float(rate)
    for key in ("latency_ms", "latency_jitter_ms"):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 60_000:
                raise ValueError(f"{key} must be a number between 0 and 60000")
            new[key] = int(value)
    if not new:
        raise ValueError("Nothing to configure: use error_rate, latency_ms and/or latency_jitter_ms")
    with FAULTS_LOCK:
        FAULTS.update(new)
        _publish_fault_gauges()
        current = dict(FAULTS)
    log.warning("Faults configured: %s", current)
    return current


# ---------------------------------------------------------------- dados (exclusivos deste serviço)
PAYMENTS = {}               # id -> payment
SUCCESS_BY_ORDER = {}       # order_id -> payment id (idempotência)
DATA_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def process_payment(payload):
    """Devolve (http_status, payment)."""
    if not isinstance(payload, dict):
        raise ApiError(400, "Body must be a JSON object")
    order_id = payload.get("order_id")
    amount = payload.get("amount")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ApiError(400, "order_id is required")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        raise ApiError(400, "amount must be a positive number")
    order_id = order_id.strip()

    with DATA_LOCK:
        existing = SUCCESS_BY_ORDER.get(order_id)
        if existing:
            return 200, dict(PAYMENTS[existing])   # já cobrado: não cobrar duas vezes

    with FAULTS_LOCK:
        faults = dict(FAULTS)

    delay_ms = faults["latency_ms"] + random.randint(0, faults["latency_jitter_ms"])
    if delay_ms:
        time.sleep(delay_ms / 1000)                 # "sistema externo" lento
    failed = random.random() < faults["error_rate"]

    payment = {
        "id": str(uuid.uuid4()),
        "order_id": order_id,
        "amount": round(float(amount), 2),
        "status": "FAILED" if failed else "SUCCESS",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    with DATA_LOCK:
        if not failed:
            existing = SUCCESS_BY_ORDER.get(order_id)
            if existing:                            # outra tentativa concorrente ganhou
                return 200, dict(PAYMENTS[existing])
            SUCCESS_BY_ORDER[order_id] = payment["id"]
            payment_amount_total.inc(payment["amount"])
        PAYMENTS[payment["id"]] = payment

    if failed:
        log.info("Payment %s FAILED (injected) for order %s", payment["id"], order_id)
        return 500, dict(payment, error="Payment provider error (injected fault)")
    return 201, dict(payment)


# ---------------------------------------------------------------- HTTP
class PaymentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/metrics":
            self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
        elif path == "/health":
            self._json(200, {"status": "ok"})
        elif path == "/payments/configure-faults":
            with FAULTS_LOCK:
                self._json(200, dict(FAULTS))
        elif path == "/payments":
            order_id = parse_qs(url.query).get("order_id", [None])[0]
            with DATA_LOCK:
                result = [dict(p) for p in PAYMENTS.values() if order_id is None or p["order_id"] == order_id]
            result.sort(key=lambda p: p["processed_at"], reverse=True)
            self._json(200, result)
        elif path.startswith("/payments/"):
            with DATA_LOCK:
                payment = PAYMENTS.get(path.removeprefix("/payments/"))
                payment = dict(payment) if payment else None
            self._json(200, payment) if payment else self._json(404, {"error": "Payment not found"})
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/payments/configure-faults":
            try:
                self._json(200, configure_faults(self._read_json()))
            except ApiError as error:
                self._json(error.status, {"error": error.message})
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if path != "/payments":
            self._json(404, {"error": "Not found"})
            return

        started = perf_counter()
        try:
            status, result = process_payment(self._read_json())
        except ApiError as error:
            status, result = error.status, {"error": error.message}
        payment_processing_duration.observe(perf_counter() - started)
        payment_requests.labels(str(status)).inc()
        self._json(status, result)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Payload too large")
        try:
            return json.loads(self.rfile.read(length) or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON")

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status, body, content_type):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # o cliente (Order) desistiu por timeout enquanto a latência injetada decorria

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512


if __name__ == "__main__":
    _publish_fault_gauges()
    for code in ("200", "201", "400", "500"):
        payment_requests.labels(code)
    log.info("Payment service listening on :%s (faults=%s)", PORT, FAULTS)
    Server(("0.0.0.0", PORT), PaymentHandler).serve_forever()
