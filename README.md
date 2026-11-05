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
                                     └─ OrderCreated ─► RabbitMQ (fila order_created) ─► notification:8003
```

| Serviço      | Porta | Endpoints                                         |
|--------------|-------|---------------------------------------------------|
| api-gateway  | 8000  | `GET /events`, `GET /events/{id}`, `GET /orders`, `POST /orders`, `GET /orders/{id}`, `GET /health`, `GET /metrics` |
| catalog      | 8001  | `GET /events`, `GET /events/{id}`, `GET /metrics` |
| order        | 8002  | `POST /orders`, `GET /orders`, `GET /orders/{id}`, `GET /health`, `GET /metrics` |
| notification | 8003  | `GET /metrics` (consumidor RabbitMQ)              |
| redis        | 6379  | contadores do rate limiter                        |
| rabbitmq     | 5672 / 15672 / 15692 | AMQP / UI (guest/guest) / métricas  |
| prometheus   | 9090  |                                                   |
| grafana      | 3000  | admin/admin – dashboards na pasta *TicketMEI*     |

## Estrutura do repositório

```
api-gateway/ catalog/ order/ notification/   código + Dockerfile de cada serviço (build a partir da raiz)
data/events.json                              dados do Catalog
k8s/                                          manifestos Kubernetes (o ArgoCD vigia esta pasta)
  kustomization.yaml                          ponto de entrada + tags das imagens (atualizadas pelo CD)
  apps/  infra/  monitoring/                  serviços, redis/rabbitmq, prometheus/grafana
  monitoring/grafana/                         dashboards e provisioning (usados também pelo docker-compose)
argocd/application.yaml                       Application do ArgoCD (aplicar uma vez)
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
# -> 202 Accepted, status PENDING

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

## Monitorização

- Dashboards provisionados automaticamente (compose e k8s): *Gateway* (inclui rate limiter), *Catalog*,
  *Order & Notification* (inclui profundidade da fila).
- Dashboard oficial do RabbitMQ: Grafana → Dashboards → New → Import → ID **10991** → datasource *Prometheus*.

## Utilização de IA

- Descrição base e ficheiros iniciais do enunciado: Google Gemini (fornecido pela docente).
- Fase 2 (serviços Order e Notification, atualização do API Gateway, docker-compose, provisionamento do Grafana,
  scripts k6 e workflow GitHub Actions): gerado com apoio do Claude (Anthropic) e revisto pelo grupo.
- Fase 3 (rate limiter com Redis, manifestos Kubernetes, pipeline CD com GHCR, Application do ArgoCD, testes k6
  do rate limiter): gerado com apoio do Claude (Anthropic) e revisto pelo grupo.
