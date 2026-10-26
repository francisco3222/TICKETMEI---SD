# TicketMEI – Sistemas Distribuídos 2026/2027

Grupo: Andre · Adelino · Leandro

Plataforma de venda de bilhetes baseada em microsserviços.

## Arquitetura (fase 2)

```
cliente ──HTTP──► api-gateway:8000 ──GET──► catalog:8001  (data/events.json)
                        │
                        └──POST/GET──► order:8002 ──GET /events/{id}──► catalog
                                          │
                                          └─ OrderCreated ─► RabbitMQ (fila order_created) ─► notification:8003
```

| Serviço      | Porta | Endpoints                                         |
|--------------|-------|---------------------------------------------------|
| api-gateway  | 8000  | `GET /events`, `GET /events/{id}`, `POST /orders`, `GET /orders/{id}`, `GET /metrics` |
| catalog      | 8001  | `GET /events`, `GET /events/{id}`, `GET /metrics` |
| order        | 8002  | `POST /orders`, `GET /orders/{id}`, `GET /health`, `GET /metrics` |
| notification | 8003  | `GET /metrics` (consumidor RabbitMQ)              |
| rabbitmq     | 5672 / 15672 / 15692 | AMQP / UI (guest/guest) / métricas  |
| prometheus   | 9090  |                                                   |
| grafana      | 3000  | admin/admin – dashboards provisionados na pasta *TicketMEI* |

## Executar

```bash
docker compose up -d --build

# criar uma compra (máx. 4 bilhetes)
curl -i -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"c1","event_id":"evt-101","quantity":2,"email":"c1@ticketmei.pt"}'
# -> 202 Accepted, status PENDING

curl http://localhost:8000/orders/<id>

# log das notificações enviadas
docker compose exec notification cat /app/logs/notifications.log
```

## Testes de carga (k6)

```bash
docker compose --profile testing run --rm k6 run load-test.js    # GET /events (fase 1)
docker compose --profile testing run --rm k6 run order-test.js   # POST /orders (fase 2)
docker compose --profile testing run --rm k6 run smoke-test.js   # usado no CI
```

## Monitorização

- Dashboards provisionados automaticamente: *Gateway*, *Catalog*, *Order & Notification* (inclui profundidade da fila).
- Dashboard oficial do RabbitMQ: Grafana → Dashboards → New → Import → ID **10991** (RabbitMQ-Overview) → datasource *Prometheus*.

## CI

`.github/workflows/ci.yml` valida a sintaxe, o `docker-compose.yml`, faz build, arranca a stack, verifica os `/metrics` e corre `k6/smoke-test.js` em cada push/PR.

## Utilização de IA

- Descrição base e ficheiros iniciais do enunciado: Google Gemini (fornecido pela docente).
- Fase 2 (serviços Order e Notification, atualização do API Gateway, docker-compose, provisionamento do Grafana, scripts k6 e workflow GitHub Actions): gerado com apoio do Claude (Anthropic) e revisto pelo grupo.
