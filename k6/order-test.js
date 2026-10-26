import http from 'k6/http';
import { check, sleep } from 'k6';

// Teste da fase 2: POST /orders através do API Gateway.
// Uso:  docker compose --profile testing run --rm k6 run order-test.js
const BASE_URL = __ENV.BASE_URL || 'http://api-gateway:8000';
const EVENTS = ['evt-101', 'evt-102', 'evt-103', 'evt-104'];

export const options = {
  scenarios: {
    orders: {
      executor: 'ramping-vus',
      stages: [
        { duration: '30s', target: 20 },
        { duration: '1m', target: 20 },
        { duration: '15s', target: 0 },
      ],
    },
  },
  thresholds: {
    'http_req_duration{name:create_order}': ['p(95)<500'],
    'checks{check:accepted}': ['rate>0.99'],
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
  }, { check: 'accepted' });

  if (ok) {
    const get = http.get(`${BASE_URL}/orders/${res.json('id')}`, { tags: { name: 'get_order' } });
    check(get, { 'GET /orders/{id} is 200': (r) => r.status === 200 });
  }

  // Validação do limite de bilhetes: 5 bilhetes deve dar 400 (só 5% das iterações)
  if (Math.random() < 0.05) {
    const bad = http.post(`${BASE_URL}/orders`,
      JSON.stringify({ customer_id: 'x', event_id: 'evt-101', quantity: 5, email: 'x@ticketmei.pt' }),
      { headers: { 'Content-Type': 'application/json' }, tags: { name: 'invalid_order' } });
    check(bad, { 'quantity > 4 rejected with 400': (r) => r.status === 400 });
  }

  sleep(0.5);
}
