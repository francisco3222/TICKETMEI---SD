# TicketMEI – Sistemas Distribuídos 2026/2027

Grupo: Andre · Adelino · Leandro

Plataforma de venda de bilhetes baseada em microsserviços, orquestrada em Kubernetes
com CI/CD (GitHub Actions + GHCR + ArgoCD) e monitorização (Prometheus + Grafana).

## Arquitetura

```
cliente ──HTTP──► api-gateway:8000 ──GET──► catalog:8001  (events.json dentro da imagem)
                   │  (rate limiter ──► redis:6379, 5 POST /orders por s por IP → 429)
                   │
                   └──POST/GET──► order:8002 ──GET /events/{id}──► catalog
                                     │
                                     ├─ POST /payments ──[circuit breaker + retry c/ jitter]──► payment:8004
                                     │
                                     └─ OrderCreated ─► RabbitMQ orders.exchange ─► fila order_created ─► notification:8003
```

| Serviço      | Porta | Endpoints                                         |
|--------------|-------|---------------------------------------------------|
| api-gateway  | 8000  | `GET /events`, `GET /events/{id}`, `GET /orders`, `POST /orders`, `GET /orders/{id}`, `GET /health`, `GET /metrics` |
| catalog      | 8001  | `GET /events`, `GET /events/{id}`, `GET /metrics` |
| order        | 8002  | `POST /orders`, `GET /orders`, `GET /orders/{id}`, `GET /circuit-breaker`, `GET /health`, `GET /metrics` |
| payment      | 8004  | `POST /payments`, `GET /payments`, `GET /payments/{id}`, `POST/GET /payments/configure-faults`, `GET /health`, `GET /metrics` (interno: não passa pelo gateway) |
| notification | 8003  | `GET /metrics` (consumidor RabbitMQ)              |
| redis        | 6379  | contadores do rate limiter                        |
| rabbitmq     | 5672 / 15672 / 15692 | AMQP / UI (guest/guest) / métricas  |
| prometheus   | 9090  |                                                   |
| grafana      | 3000  | admin/admin – dashboards na pasta *TicketMEI*     |

## Estrutura do repositório

```
api-gateway/ catalog/ order/ notification/   código + Dockerfile de cada serviço (build a partir da raiz)
data/events.json                              dados do Catalog
k8s/                                          manifestos do cluster LOCAL (o ArgoCD local vigia esta pasta)
  kustomization.yaml                          ponto de entrada do cluster local
  apps/kustomization.yaml                     serviços + TAGS das imagens (base partilhada; atualizada pelo CD)
k8s-global/                                   cluster GLOBAL da UMa (grupo04): reutiliza k8s/apps + Redis,
                                              ServiceMonitors, NetworkPolicies, Ingress, credenciais via Secret
  apps/  infra/  monitoring/                  serviços, redis/rabbitmq, prometheus/grafana
  monitoring/grafana/                         dashboards e provisioning (usados também pelo docker-compose)
argocd/application.yaml                       Application do ArgoCD local (aplicar uma vez)
argocd/application-global.yaml                Application do cluster global (criar pela UI do ArgoCD, projeto grupo04)
.github/workflows/ci.yml                      testes (compose + k6) e validação de /k8s
.github/workflows/cd.yml                      build por serviço → GHCR (tag = Git SHA) → atualiza /k8s
k6/                                           load-test, order-test, smoke-test, rate-limit-test
prometheus/prometheus.yml                     configuração do Prometheus para o docker-compose
```

---

## Fases 1 e 2 – Docker Compose

```bash
docker compose up -d --build

# criar uma compra (máx. 4 bilhetes)  –  PowerShell:
Invoke-RestMethod -Method Post -Uri http://localhost:8000/orders -ContentType 'application/json' `
  -Body '{"customer_id":"c1","event_id":"evt-101","quantity":2,"email":"c1@ticketmei.pt"}'
# -> 201 Created, status CONFIRMED (Parte II: a compra só é confirmada depois do pagamento)

# log das notificações enviadas
docker compose exec notification cat /app/logs/notifications.log
```

Testes k6:

```bash
docker compose --profile testing run --rm k6 run load-test.js        # GET /events (fase 1)
docker compose --profile testing run --rm k6 run order-test.js       # POST /orders a 4/s (abaixo do limite)
docker compose --profile testing run --rm k6 run rate-limit-test.js  # 20 POST/s → ~5/s aceites, resto 429
docker compose --profile testing run --rm k6 run smoke-test.js       # usado no CI
```

---

## Fase 3 – Kubernetes + CI/CD + ArgoCD

### Pipeline

```
git push main
   └─► cd.yml
        1. test               → chama ci.yml (compose + k6 + kustomize/kubeconform)
        2. changes            → dorny/paths-filter: que serviços mudaram?
        3. build (matrix)     → só os serviços alterados → ghcr.io/<owner>/ticketmei-<svc>:<git sha>
        4. update-manifests   → kustomize edit set image … → commit "chore(k8s): deploy <sha> [skip ci]"
   └─► ArgoCD deteta o commit em k8s/ → sync automático → rolling update no cluster
```

### Preparação (uma única vez)

1. **Ativar o Kubernetes** – Docker Desktop → Settings → Kubernetes → *Enable Kubernetes*.
   ```powershell
   kubectl config use-context docker-desktop
   docker compose down        # liberta as portas 8000/3000/9090 usadas pelo compose
   ```

2. **GitHub – permissões do workflow** – no repositório: Settings → Actions → General →
   *Workflow permissions* → **Read and write permissions** → Save.

3. **Primeiro build de todas as imagens** – Actions → *CD* → *Run workflow* (com *build_all* marcado).

4. **Tornar as imagens públicas** (o GHCR cria-as privadas; sem isto o cluster dá `ImagePullBackOff`):
   github.com/francisco3222 → *Packages* → para cada `ticketmei-*` → *Package settings* →
   *Change visibility* → **Public**.

5. **Instalar o ArgoCD**
   ```powershell
   kubectl create namespace argocd
   kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
   kubectl wait --for=condition=available deployment --all -n argocd --timeout=300s

   # password do utilizador admin
   $p = kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}"
   [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p))

   # UI em https://localhost:8081 (deixar este terminal aberto)
   kubectl port-forward svc/argocd-server -n argocd 8081:443
   ```

6. **Registar a aplicação no ArgoCD** (é o único `kubectl apply` manual de todo o projeto):
   ```powershell
   kubectl apply -n argocd -f argocd/application.yaml
   ```

7. **Verificar**
   ```powershell
   kubectl get pods -n ticketmei          # todos Running / READY
   Invoke-RestMethod http://localhost:8000/events
   ```
   Grafana: http://localhost:3000 · Prometheus: http://localhost:9090 · RabbitMQ: http://localhost:15672

### Demonstração

**Atualização automática** – altera por exemplo um preço em `data/events.json`, faz commit e push:
1. Actions → *CD*: só o `catalog` é construído (os outros serviços não mudaram).
2. Aparece um commit `chore(k8s): deploy <sha> [skip ci]` que muda a tag em `k8s/kustomization.yaml`.
3. ArgoCD sincroniza (consulta o Git a cada ~3 min; para ser imediato carregar em *Refresh*).
4. `kubectl get pods -n ticketmei -w` mostra o rolling update; `http://localhost:8000/events` já mostra o novo preço.

**Rate limiter** (RNF3):
```powershell
docker run --rm -v "${PWD}/k6:/scripts" -e BASE_URL=http://host.docker.internal:8000 grafana/k6:0.49.0 run /scripts/rate-limit-test.js
```
Resultado esperado: ~5 pedidos/s com 202 e os restantes com **429 Too Many Requests** (+ cabeçalho
`Retry-After`). No Grafana, dashboard *TicketMei - P1* → painel *Rate limiter*.

### Rate limiter – como funciona

- `api-gateway/ratelimiter.py`: janela fixa de 1 s por IP; contador `ratelimit:/orders:<ip>:<janela>` no Redis,
  incrementado por um script Lua (`INCR` + `PEXPIRE` atómicos).
- Está no **Redis** e não em memória porque o gateway corre com **2 réplicas** no Kubernetes:
  o limite tem de ser global, não por réplica.
- Se o Redis falhar, o gateway deixa passar os pedidos (*fail-open*) e conta o erro em
  `gateway_rate_limiter_errors_total`.
- Limitação conhecida da janela fixa: na fronteira entre duas janelas podem passar até 2× o limite num
  intervalo curto (uma janela por *sliding window* resolveria isto, com mais custo no Redis).
- Configurável por variáveis de ambiente: `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS`,
  `RATE_LIMIT_ENABLED`, `TRUST_PROXY_HEADERS`.

### Decisões e limitações

- **Order com 1 réplica**: as compras estão em memória; com 2 réplicas um `GET /orders/{id}` podia ir à réplica
  errada. Um rollout do Order apaga as compras existentes (escalar exigiria uma base de dados própria do Order).
- **Notification** pode escalar (`replicas: N`): o RabbitMQ distribui as mensagens (*competing consumers*).
- **Prometheus no k8s** descobre os pods pela anotação `prometheus.io/scrape` (cada réplica é recolhida à parte)
  e lê CPU/memória do cAdvisor do kubelet.

---

## Parte II – Fase 1: Payment + Circuit Breaker + Retry com jitter

### Fluxo de `POST /orders`

```
validar ─► Catalog (preço) ─► compra PENDING ─► Payment (dentro do circuit breaker, com retry + jitter)
   ├─ pagamento OK                     ─► CONFIRMED ─► OrderCreated → orders.exchange ─► 201 Created
   ├─ Payment falhou (circuito fechado) ─► FAILED                                       ─► 502 {"error":"Payment failed",...}
   └─ circuito OPEN (fail-fast)         ─► FAILED, fallback sem chamar o Payment        ─► 503 {"error":"Payment system unavailable, try again later"}
```

### Circuit breaker (`order/circuit_breaker.py`)

| Parâmetro (variável de ambiente) | Valor | Significado |
|---|---|---|
| `CB_FAILURE_RATE_THRESHOLD` | 0.5 | abre com ≥ 50 % de falhas (5xx, timeout, ligação recusada)… |
| `CB_SLOW_CALL_SECONDS` / `CB_SLOW_CALL_RATE_THRESHOLD` | 1.5 s / 0.8 | …ou com ≥ 80 % de chamadas mais lentas que 1,5 s (limite de **latência**) |
| `CB_WINDOW_SIZE` / `CB_MIN_CALLS` | 20 / 10 | janela deslizante dos últimos 20 pedidos, só decide com ≥ 10 |
| `CB_OPEN_SECONDS` | 10 | tempo em OPEN antes de passar a HALF-OPEN |
| `CB_HALF_OPEN_MAX_CALLS` | 3 | pedidos de teste em HALF-OPEN (3 sucessos → CLOSED; 1 falha → OPEN) |
| `PAYMENT_TIMEOUT_SECONDS` | 2 | timeout de cada chamada ao Payment |
| `RETRY_MAX_ATTEMPTS` / `RETRY_BASE_DELAY_SECONDS` / `RETRY_MAX_DELAY_SECONDS` | 3 / 0.1 / 1 | retry com *full jitter*: espera `random(0, min(1, 0.1·2^n))` |

- Cada tentativa (incluindo os retries) conta para o circuito; **se o circuito abrir entre tentativas, os retries param logo**.
- Erros 4xx do Payment não contam para o circuito (o problema é do pedido, não do Payment).
- **Idempotência**: o Order envia `Idempotency-Key: <order_id>` e o Payment nunca cobra duas vezes a mesma compra
  (importante com retries: um timeout não quer dizer que a cobrança não aconteceu).
- Testes unitários: `cd order && python -m unittest -v test_circuit_breaker` (correm no CI).

Métricas: `circuit_breaker_state` (0/1/2), `circuit_breaker_fallbacks_total`, `payment_client_requests_total{status}`,
`orders_initiated_total{status}`, `circuit_breaker_transitions_total{from_state,to_state}`, `payment_retries_total`;
no Payment: `payment_requests_total{http_status}`, `payment_processing_duration_seconds`, `payment_fault_error_rate`,
`payment_fault_latency_seconds`. Dashboard: **TicketMEI - Payment & Circuit Breaker**.

### Injetar falhas no Payment

```powershell
# PowerShell (docker-compose: o Payment está em localhost:8004)
Invoke-RestMethod -Method Post -Uri http://localhost:8004/payments/configure-faults -ContentType 'application/json' -Body '{"error_rate":0.8}'
Invoke-RestMethod -Method Post -Uri http://localhost:8004/payments/configure-faults -ContentType 'application/json' -Body '{"latency_ms":10000}'
Invoke-RestMethod -Method Post -Uri http://localhost:8004/payments/configure-faults -ContentType 'application/json' -Body '{"error_rate":0,"latency_ms":0}'   # restaurar
Invoke-RestMethod http://localhost:8002/circuit-breaker      # estado do circuito
```
```bash
# Mac/Linux
curl -X POST localhost:8004/payments/configure-faults -H 'Content-Type: application/json' -d '{"error_rate":0.8}'
```
No Kubernetes o Payment e o Order **não** estão expostos (só o gateway): abrir túneis primeiro
```bash
kubectl port-forward -n ticketmei svc/payment 8004:8004
kubectl port-forward -n ticketmei svc/order 8002:8002     # noutro terminal
```

### Teste k6 do circuit breaker (50 pedidos/s, degradação a meio)

```bash
# docker-compose (Windows/Mac/Linux)
docker compose --profile testing run --rm k6 run circuit-breaker-test.js                    # 80 % HTTP 500
docker compose --profile testing run --rm -e FAULT=latency k6 run circuit-breaker-test.js   # 10 s de atraso

# Kubernetes (com os dois port-forward acima abertos)
docker run --rm -v "${PWD}/k6:/scripts" -e BASE_URL=http://host.docker.internal:8002 -e PAYMENT_URL=http://host.docker.internal:8004 grafana/k6:0.49.0 run /scripts/circuit-breaker-test.js
#   Linux: acrescentar --network host e usar http://localhost:8002 / :8004
```
1 min normal → 2 min degradado → 2 min recuperação. O teste vai **diretamente ao Order** (não ao gateway):
todos os pedidos do k6 saem do mesmo IP e o rate limiter (5/s por IP) rejeitaria 90 % com 429.
Resultado medido localmente (versão encurtada): degradação com 80 % de erros → **OPEN no mesmo segundo**;
com 10 s de latência → **OPEN em ~2,2 s** (um timeout); respostas 503 com p95 de 2–25 ms (fail-fast);
depois de restaurar → HALF-OPEN → CLOSED em ≤ 10 s.

Verificação rápida (≈30 s, também corre no CI): `bash scripts/circuit-breaker-check.sh` (bash / Git Bash).

### Limitações conhecidas

- Payment com **1 réplica**: a configuração de falhas é por processo.
- Os dados (compras, pagamentos) continuam em memória: um reinício apaga-os.
- Se o pagamento for aceite mas o RabbitMQ falhar, a compra fica CONFIRMED sem email
  (a solução robusta seria o padrão *Outbox*).

---

## Cluster global (UMa) – grupo04

Acesso só dentro da rede da UMa ou por VPN. **As credenciais (ArgoCD, RabbitMQ) nunca vão para o Git**:
o `.gitignore` bloqueia `*ClusterCredentials*` e `*.docx`, e no cluster as do RabbitMQ vêm do Secret
`grupo04-user-user-credentials`, que já existe no namespace.

| | Cluster local (`k8s/`) | Cluster global (`k8s-global/`) |
|---|---|---|
| Namespace | `ticketmei` (criado pelo ArgoCD) | `grupo04` (já existe) |
| Serviços da app e tags | `k8s/apps` | **os mesmos** (`../k8s/apps`) |
| RabbitMQ | o nosso | o do cluster, vhost `/grupo04`, credenciais do Secret |
| Prometheus / Grafana | os nossos | os do cluster: `ServiceMonitor` por serviço + `NetworkPolicy` |
| Entrada externa | LoadBalancer `localhost:8000` | Ingress `http://grupo04-ticketmei.10.2.15.161.nip.io` |
| Segurança | — | Pod Security *restricted*: tudo não-root (UID 10001; Redis 999) |

**Criar a aplicação no ArgoCD global** (uma vez): http://argocd.10.2.15.161.nip.io → login `grupo04` →
*+ NEW APP* → Name `ticketmei`, Project `grupo04`, Sync *Automatic* (+ *Prune* e *Self Heal*),
Repository `https://github.com/francisco3222/TICKETMEI---SD.git`, Revision `main`, Path **`k8s-global`**,
Cluster `https://kubernetes.default.svc`, Namespace `grupo04` → *CREATE*.
(Equivalente em YAML: `argocd/application-global.yaml`.)

**Verificar**: `http://grupo04-ticketmei.10.2.15.161.nip.io/events` no browser (rede UMa/VPN).

**Dashboards**: no Grafana do cluster (http://grafana.10.2.15.161.nip.io) → pasta do grupo → *Import* →
carregar os JSON de `k8s/monitoring/grafana/dashboards/` e escolher a datasource Prometheus no seletor
(os dashboards usam a variável `${datasource}`).

**NetworkPolicies**: a do template (Prometheus) sozinha *bloquearia* o tráfego entre os nossos serviços —
quando uma NetworkPolicy seleciona um pod, tudo o que ela não permite é recusado. Por isso há também
`allow-same-namespace` (os nossos pods entre si) e `allow-ingress-to-gateway` (só o gateway recebe de fora).

**Um push atualiza os dois clusters**: o CD só muda as tags em `k8s/apps/kustomization.yaml`, que é usado
por `k8s/` e por `k8s-global/`; cada ArgoCD sincroniza o seu.

## Monitorização

- Dashboards provisionados automaticamente (compose e k8s): *Gateway* (inclui rate limiter), *Catalog*,
  *Order & Notification* (inclui profundidade da fila).
- Dashboard oficial do RabbitMQ: Grafana → Dashboards → New → Import → ID **10991** → datasource *Prometheus*.

## Utilização de IA

- Descrição base e ficheiros iniciais do enunciado: Google Gemini (fornecido pela docente).
- Fase 2 (serviços Order e Notification, atualização do API Gateway, docker-compose, provisionamento do Grafana,
  scripts k6 e workflow GitHub Actions): gerado com apoio do Claude (Anthropic) e revisto pelo grupo.
- Parte II, fase 1 (serviço Payment, circuit breaker + retry com jitter, testes unitários, script de verificação,
  teste k6 do circuit breaker, dashboard Grafana, manifestos k8s do Payment, contentores não-root):
  gerado com apoio do Claude (Anthropic) e revisto pelo grupo.
- Fase 3 (rate limiter com Redis, manifestos Kubernetes, pipeline CD com GHCR, Application do ArgoCD, testes k6
  do rate limiter): gerado com apoio do Claude (Anthropic) e revisto pelo grupo.
