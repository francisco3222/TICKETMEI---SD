import http from 'k6/http';
import { check } from 'k6';

// Teste de carga de POST /orders através do API Gateway.
// Todos os pedidos do k6 saem do MESMO IP, por isso o ritmo fica em 4 compras/s,
// abaixo do rate limiter (5/s por IP). Para testar o limiter usa rate-limit-test.js.
//   docker compose --profile testing run --rm k6 run order-test.js
//   k8s (PowerShell ou bash, na raiz do repo):
//     docker run --rm -v "${PWD}/k6:/scripts" -e BASE_URL=http://host.docker.internal:8000 grafana/k6:0.49.0 run /scripts/order-test.js
const BASE_URL = __ENV.BASE_URL || 'http://api-gateway:8000';
const EVENTS = ['evt-101', 'evt-102', 'evt-103', 'evt-104'];
const RATE = Number(__ENV.RATE || 4);

export const options = {
  scenarios: {
    orders: {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: '1m',
      preAllocatedVUs: 10, maxVUs: 30,
    },
  },
  thresholds: {
    'http_req_duration{name:create_order}': ['p(95)<500'],
    checks: ['rate>0.99'],
  },
};

export default function () {
  const payload = JSON.stringify({
    customer_id: `cust-${__VU}`,
    event_id: EVENTS[Math.floor(Math.random() * EVENTS.length)],
    quantity: Math.floor(Math.random() * 4) + 1,   // 1..4 (limite por compra)
    email: `cliente${__VU}@ticketmei.pt`,
  });

  const res = http.post(`${BASE_URL}/orders`, payload, {
    headers: { 'Content-Type': 'application/json' },
    tags: { name: 'create_order' },
  });

  const ok = check(res, {
    'status is 202': (r) => r.status === 202,
    'order is PENDING': (r) => r.status === 202 && r.json('status') === 'PENDING',
  });

  if (ok) {
    const get = http.get(`${BASE_URL}/orders/${res.json('id')}`, { tags: { name: 'get_order' } });
    check(get, { 'GET /orders/{id} is 200': (r) => r.status === 200 });
  }
}
