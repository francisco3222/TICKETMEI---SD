import http from 'k6/http';
import { check, sleep } from 'k6';

// Teste rápido (~30 s) usado pelo GitHub Actions para validar fases 1 e 2.
const BASE_URL = __ENV.BASE_URL || 'http://api-gateway:8000';

export const options = {
  vus: 5,
  duration: '30s',
  thresholds: {
    checks: ['rate>0.99'],
    http_req_duration: ['p(95)<1000'],
  },
};

export default function () {
  const events = http.get(`${BASE_URL}/events`);
  check(events, {
    'GET /events 200': (r) => r.status === 200,
    'events not empty': (r) => r.status === 200 && r.json().length > 0,
  });

  check(http.get(`${BASE_URL}/events/evt-101`), { 'GET /events/{id} 200': (r) => r.status === 200 });

  const order = http.post(`${BASE_URL}/orders`,
    JSON.stringify({ customer_id: `ci-${__VU}`, event_id: 'evt-101', quantity: 2, email: 'ci@ticketmei.pt' }),
    { headers: { 'Content-Type': 'application/json' } });
  check(order, {
    'POST /orders 202': (r) => r.status === 202,
    'order PENDING': (r) => r.status === 202 && r.json('status') === 'PENDING',
  });

  check(http.get(`${BASE_URL}/metrics`), { 'gateway /metrics 200': (r) => r.status === 200 });
  sleep(0.5);
}
