import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';

// Demonstração do rate limiter (RNF3): 20 POST /orders por segundo a partir de um só IP.
// Esperado: ~5/s aceites (202) e o resto rejeitado com 429 Too Many Requests.
//   docker compose --profile testing run --rm k6 run rate-limit-test.js
//   k8s (PowerShell ou bash, na raiz do repo):
//     docker run --rm -v "${PWD}/k6:/scripts" -e BASE_URL=http://host.docker.internal:8000 grafana/k6:0.49.0 run /scripts/rate-limit-test.js
const BASE_URL = __ENV.BASE_URL || 'http://api-gateway:8000';

const accepted = new Counter('orders_accepted');
const limited = new Counter('orders_rate_limited');
const limitedRate = new Rate('rate_limited_ratio');

export const options = {
  scenarios: {
    burst: {
      executor: 'constant-arrival-rate', rate: 20, timeUnit: '1s', duration: '20s',
      preAllocatedVUs: 20, maxVUs: 50,
    },
  },
  thresholds: {
    orders_rate_limited: ['count>0'],          // o limiter tem de atuar
    rate_limited_ratio: ['rate>0.5'],          // com 20/s e limite 5/s, ~75% devem ser 429
    checks: ['rate>0.99'],                     // só 202 ou 429, nunca 5xx
  },
};

export default function () {
  const res = http.post(`${BASE_URL}/orders`,
    JSON.stringify({ customer_id: 'burst', event_id: 'evt-101', quantity: 1, email: 'burst@ticketmei.pt' }),
    { headers: { 'Content-Type': 'application/json' }, responseCallback: http.expectedStatuses(202, 429) });

  check(res, { 'status is 202 or 429': (r) => r.status === 202 || r.status === 429 });
  check(res, { '429 has Retry-After': (r) => r.status !== 429 || r.headers['Retry-After'] !== undefined });

  if (res.status === 429) limited.add(1); else if (res.status === 202) accepted.add(1);
  limitedRate.add(res.status === 429);
}
