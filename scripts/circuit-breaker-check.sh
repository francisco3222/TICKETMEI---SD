#!/usr/bin/env bash
# Verificação rápida (≈30 s) do circuit breaker, usada no CI e útil na demonstração.
#   ORDER_URL=http://localhost:8002 PAYMENT_URL=http://localhost:8004 ./scripts/circuit-breaker-check.sh
# No Kubernetes:  kubectl port-forward -n ticketmei svc/order 8002:8002  (e svc/payment 8004:8004)
set -euo pipefail
ORDER_URL=${ORDER_URL:-http://localhost:8002}
PAYMENT_URL=${PAYMENT_URL:-http://localhost:8004}
OPEN_SECONDS=${CB_OPEN_SECONDS:-10}
BODY='{"customer_id":"cb-check","event_id":"evt-101","quantity":1,"email":"cb@ticketmei.pt"}'

state() { curl -fs "$ORDER_URL/metrics" | awk '/^circuit_breaker_state / {print int($2)}'; }
faults() { curl -fs -X POST "$PAYMENT_URL/payments/configure-faults" -H 'Content-Type: application/json' -d "$1"; echo; }
order() { curl -s -o /tmp/cb_body.json -w '%{http_code}' -X POST "$ORDER_URL/orders" -H 'Content-Type: application/json' -d "$BODY"; }
cleanup() { faults '{"error_rate":0,"latency_ms":0}' >/dev/null || true; }
trap cleanup EXIT

echo "1) Estado inicial: $(state) (0=CLOSED)"
[ "$(state)" = "0" ] || { echo "Esperava CLOSED"; exit 1; }

echo "2) Payment a falhar 100 %"; faults '{"error_rate":1.0,"latency_ms":0}'
codes=""
for i in $(seq 1 12); do codes="$codes $(order)"; done
echo "   respostas: $codes"
[ "$(state)" = "1" ] || { echo "FALHOU: o circuito devia estar OPEN (1), está $(state)"; exit 1; }
grep -q '"Payment system unavailable, try again later"' /tmp/cb_body.json || { echo "FALHOU: payload do 503"; cat /tmp/cb_body.json; exit 1; }
echo "   OK: circuito OPEN e fallback 503 com o payload pedido"

before=$(curl -fs "$PAYMENT_URL/metrics" | awk '/^payment_requests_total\{http_status="500"\}/ {print int($2)}')
t0=$(date +%s%N); code=$(order); t1=$(date +%s%N)
after=$(curl -fs "$PAYMENT_URL/metrics" | awk '/^payment_requests_total\{http_status="500"\}/ {print int($2)}')
echo "3) Com o circuito OPEN: HTTP $code em $(( (t1 - t0) / 1000000 )) ms; pedidos ao Payment: $before -> $after"
[ "$code" = "503" ] && [ "$before" = "$after" ] || { echo "FALHOU: fail-fast devia dar 503 sem chamar o Payment"; exit 1; }

echo "4) Payment recuperado; espera ${OPEN_SECONDS}s para HALF_OPEN"; faults '{"error_rate":0,"latency_ms":0}'
sleep $((OPEN_SECONDS + 1))
codes=""
for i in 1 2 3 4; do codes="$codes $(order)"; done
echo "   respostas: $codes"
[ "$(state)" = "0" ] || { echo "FALHOU: o circuito devia ter voltado a CLOSED (0), está $(state)"; exit 1; }
echo "   OK: OPEN -> HALF_OPEN -> CLOSED"
curl -fs "$ORDER_URL/metrics" | grep -E '^circuit_breaker_(transitions_total|fallbacks_total)'
echo "Circuit breaker OK"
