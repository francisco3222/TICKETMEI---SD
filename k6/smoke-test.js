import http from 'k6/http';
import { check, sleep } from 'k6';

// Teste rápido (~30 s) usado pelo GitHub Actions.
// As compras são enviadas a 2/s, abaixo do limite do rate limiter (5/s por IP).
const BASE_URL = __ENV.BASE_URL || 'http://api-gateway:8000';

export const options = {
  scenarios: {
    reads: { executor: 'constant-vus', vus: 5, duration: '30s', exec: 'reads' },
    orders: {
      executor: 'constant-arrival-rate', rate: 2, timeUnit: '1s', duration: '30s',
      preAllocatedVUs: 4, exec: 'orders',
    },
  },
  thresholds: {
    checks: ['rate>0.99'],
    http_req_duration: ['p(95)<1000'],
  },
};

export function reads() {
  const events = http.get(`${BASE_URL}/events`);
  check(events, {
    'GET /events 200': (r) => r.status === 200,
    'events not empty': (r) => r.status === 200 && r.json().length > 0,
  });
  check(http.get(`${BASE_URL}/events/evt-101`), { 'GET /events/{id} 200': (r) => r.status === 200 });
  check(http.get(`${BASE_URL}/metrics`), { 'gateway /metrics 200': (r) => r.status === 200 });
  sleep(0.5);
}

export function orders() {
  const order = http.post(`${BASE_URL}/orders`,
    JSON.stringify({ customer_id: `ci-${__VU}`, event_id: 'evt-101', quantity: 2, email: 'ci@ticketmei.pt' }),
    { headers: { 'Content-Type': 'application/json' } });
  check(order, {
    'POST /orders 202': (r) => r.status === 202,
    'order PENDING': (r) => r.status === 202 && r.json('status') === 'PENDING',
  });
  if (order.status === 202) {
    check(http.get(`${BASE_URL}/orders/${order.json('id')}`), { 'GET /orders/{id} 200': (r) => r.status === 200 });
  }
}
