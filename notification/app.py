import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pika
from prometheus_client import Counter, Histogram, start_http_server

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/%2F")
QUEUE_NAME = os.getenv("ORDER_QUEUE", "order_created")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8003"))
EMAIL_DELAY_SECONDS = float(os.getenv("EMAIL_DELAY_SECONDS", "0.1"))
LOG_FILE = Path(os.getenv("NOTIFICATION_LOG", "/app/logs/notifications.log"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [notification] %(levelname)s %(message)s")
log = logging.getLogger("notification")

notifications_processed = Counter(
    "notifications_processed",
    "Notifications processed, by delivery status.",
    ("status",),
)
notification_processing_duration = Histogram(
    "notification_processing_duration_seconds",
    "Time spent processing one OrderCreated message (includes the simulated email send).",
)


def record_delivery(order_id, email, status):
    """Regista a entrega num log próprio (dados exclusivos do Notification)."""
    entry = {
        "order_id": order_id,
        "email": email,
        "status": status,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    log.info("Notification %s for order %s <%s>", status, order_id, email)


def process(body):
    """Simula o envio de email. Devolve (order_id, email, status)."""
    try:
        message = json.loads(body)
        order_id = message["order_id"]
        email = message["email"]
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        return None, None, "FAILED"
    time.sleep(EMAIL_DELAY_SECONDS)  # "envio" do email
    return order_id, email, "SENT"


def on_message(channel, method, properties, body):
    with notification_processing_duration.time():
        order_id, email, status = process(body)
        record_delivery(order_id, email, status)
    notifications_processed.labels(status).inc()
    # ack mesmo em FAILED: uma mensagem malformada nunca vai passar a ser válida (evita loop infinito)
    channel.basic_ack(delivery_tag=method.delivery_tag)


def consume_forever():
    params = pika.URLParameters(RABBITMQ_URL)
    params.heartbeat = 30
    while True:
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=10)
            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=on_message)
            log.info("Consuming from queue '%s'", QUEUE_NAME)
            channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as error:
            log.warning("RabbitMQ unavailable (%s); retrying in 3s", error.__class__.__name__)
            time.sleep(3)


if __name__ == "__main__":
    # Pré-inicializa as séries para aparecerem a 0 no Prometheus/Grafana
    notifications_processed.labels("SENT")
    notifications_processed.labels("FAILED")
    start_http_server(METRICS_PORT)
    log.info("Metrics on :%s/metrics", METRICS_PORT)
    consume_forever()
