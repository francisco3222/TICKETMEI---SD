import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

// Parte II – Fase 1: teste do circuit breaker Order -> Payment.
//   - carga constante de 50 POST /orders por segundo durante 5 min;
//   - ao fim de 1 min injeta degradação no Payment durante 2 min (FAULT=errors: 80 % HTTP 500,
//     FAULT=latency: 10 s de atraso) e depois restaura;
//   - acompanhar no Grafana (dashboard "TicketMEI - Payment & Circuit Breaker") a transição
//     CLOSED -> OPEN, a queda de latência com o fail-fast e o regresso HALF-OPEN -> CLOSED.
//
// Vai DIRETAMENTE ao Order (não ao gateway): todos os pedidos do k6 saem do mesmo IP e o
// rate limiter do gateway (5/s por IP) rejeitaria 90 % deles com 429.
//
//   docker compose:  docker compose --profile testing run --rm k6 run circuit-breaker-test.js
//                    docker compose --profile testing run --rm -e FAULT=latency k6 run circuit-breaker-test.js
//   Kubernetes:      kubectl port-forward -n ticketmei svc/order 8002:8002
//                    kubectl port-forward -n ticketmei svc/payment 8004:8004      (noutro terminal)
//                    docker run --rm -v "${PWD}/k6:/scripts" -e BASE_URL=http://host.docker.internal:8002 \
//                      -e PAYMENT_URL=http://host.docker.internal:8004 grafana/k6:0.49.0 run /scripts/circuit-breaker-test.js
//                    (Linux: --network host e localhost em vez de host.docker.internal)

const BASE_URL = __ENV.BASE_URL || 'http://order:8002';
const PAYMENT_URL = __ENV.PAYMENT_URL || 'http://payment:8004';
const FAULT = (__ENV.FAULT || 'errors').toLowerCase();           // errors | latency
const RATE = Number(__ENV.RATE || 50);
const NORMAL = __ENV.NORMAL || '1m';                               // antes da degradação
const DEGRADED = __ENV.DEGRADED || '2m';                           // duração da degradação
const RECOVERY = __ENV.RECOVERY || '2m';                           // depois de restaurar

const toSeconds = (d) => Number(d.replace(/[a-z]+$/, '')) * (d.endsWith('m') ? 60 : 1);
const TOTAL = `${toSeconds(NORMAL) + toSeconds(DEGRADED) + toSeconds(RECOVERY)}s`;
const RESTORE_AT = `${toSeconds(NORMAL) + toSeconds(DEGRADED)}s`;

const FAULTS = {
  errors: { error_rate: 0.8, latency_ms: 0 },
  latency: { error_rate: 0, latency_ms: 10000 },
};

const latencyConfirmed = new Trend('latency_201_confirmed', true);
const latencyPaymentFailed = new Trend('latency_502_payment_failed', true);
const latencyCircuitOpen = new Trend('latency_503_circuit_open', true);
const confirmed = new Counter('orders_201_confirmed');
const paymentFailed = new Counter('orders_502_payment_failed');
const circuitOpen = new Counter('orders_503_circuit_open');

export const options = {
  scenarios: {
    load: {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: TOTAL,
      preAllocatedVUs: 100, maxVUs: 1000, exec: 'placeOrder',
    },
    inject_fault: { executor: 'per-vu-iterations', vus: 1, iterations: 1, startTime: NORMAL, exec: 'injectFault' },
    restore: { executor: 'per-vu-iterations', vus: 1, iterations: 1, startTime: RESTORE_AT, exec: 'restore' },
  },
  thresholds: {
    // fail-fast: com o circuito aberto a resposta tem de ser imediata
    'latency_503_circuit_open': ['p(95)<200'],
    // o circuito tem mesmo de abrir durante a degradação
    'orders_503_circuit_open': ['count>0'],
    // só respostas esperadas (201/502/503): nada de timeouts ou 500 do próprio Order
    checks: ['rate>0.99'],
  },
};

const EVENTS = ['evt-101', 'evt-102', 'evt-103', 'evt-104'];

export function placeOrder() {
  const payload = JSON.stringify({
    customer_id: `cb-${__VU}`,
    event_id: EVENTS[__ITER % EVENTS.length],
    quantity: 1 + (__ITER % 4),
    email: `cb${__VU}@ticketmei.pt`,
  });
  const res = http.post(`${BASE_URL}/orders`, payload, {
    headers: { 'Content-Type': 'application/json' },
    responseCallback: http.expectedStatuses(201, 502, 503),
    timeout: '30s',
    tags: { name: 'POST /orders' },
  });

  check(res, { 'status 201/502/503': (r) => [201, 502, 503].includes(r.status) });

  if (res.status === 201) { confirmed.add(1); latencyConfirmed.add(res.timings.duration); }
  else if (res.status === 502) { paymentFailed.add(1); latencyPaymentFailed.add(res.timings.duration); }
  else if (res.status === 503) {
    circuitOpen.add(1);
    latencyCircuitOpen.add(res.timings.duration);
    check(res, {
      '503 payload is the fallback message':
        (r) => r.json('error') === 'Payment system unavailable, try again later',
    });
  }
}

function configure(body) {
  const res = http.post(`${PAYMENT_URL}/payments/configure-faults`, JSON.stringify(body),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'configure-faults' } });
  check(res, { 'configure-faults 200': (r) => r.status === 200 });
  console.log(`[${new Date().toISOString()}] Payment faults -> ${res.body}`);
}

export function injectFault() {
  if (!FAULTS[FAULT]) throw new Error(`FAULT tem de ser "errors" ou "latency" (recebi "${FAULT}")`);
  configure(FAULTS[FAULT]);
}

export function restore() {
  configure({ error_rate: 0, latency_ms: 0 });
}

// Garante que o Payment fica saudável mesmo que o teste seja interrompido a meio
export function teardown() {
  configure({ error_rate: 0, latency_ms: 0 });
}
