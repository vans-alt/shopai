# ShopAI – Microservices edition

Your single FastAPI "backend" is now **6 independent services + Redis + the nginx frontend**, each in its own container,
each deployable and scalable on its own in Kubernetes.

```
Browser
  │
  ▼
Azure Load Balancer (public IP) ──► frontend (nginx, static page)
                                         │  /api/*
                                         ▼
                                    gateway :8000        ← only backend the browser knows
        ┌─────────────┬─────────────┬────┴────────┬─────────────┐
        ▼             ▼             ▼             ▼             ▼
   catalog :8001  search :8002  chatbot :8003  cart :8004   order :8005
   (products)     (TF-IDF,      (rules + Azure  (Redis)      (Redis)
        ▲          recommend)    OpenAI)           ▲            │
        │             │            │               │            │ GET/DELETE cart
        └─────────────┴────────────┘ (HTTP)        └────────────┘
                                         catalog ◄─ cart validates products
                       redis :6379  ← shared by cart + order
```

## The services

| # | Service | Port | Owns | Calls | New? |
|---|---|---|---|---|---|
| 1 | **gateway** | 8000 | routing, request-id, `/api/status`, `/api/version` | all others | new |
| 2 | **catalog** | 8001 | products + categories in **MongoDB** (seeded from `products.json`) | mongo | split from `catalog.py` |
| 3 | **search** | 8002 | TF-IDF index, `/search`, `/recommend` | catalog | split from `catalog.py` |
| 4 | **chatbot** | 8003 | chat logic, policies, Azure OpenAI | search, catalog | from `chatbot.py` (1 import line changed + `clients.py`) |
| 5 | **cart** | 8004 | shopping carts (Redis) | catalog, redis | **new feature** |
| 6 | **order** | 8005 | checkout, coupons, shipping, order history in **PostgreSQL** | cart, postgres | **new feature** |
| – | redis | 6379 | data for cart + order | | new |
| – | frontend | 80 | the page (cart drawer, checkout, orders added) | gateway | changed |

### Old file → new home
| Old | New |
|---|---|
| `backend/app/main.py` | `services/gateway/app/main.py` (proxy) + routes inside each service's `main.py` |
| `backend/app/catalog.py` (products, categories) | `services/catalog/app/main.py` |
| `backend/app/catalog.py` (search, recommend) | `services/search/app/main.py` |
| `backend/app/chatbot.py` | `services/chatbot/app/chatbot.py` (same code) + `clients.py` |
| `backend/app/products.json` | `services/catalog/app/products.json` |
| `backend/Dockerfile` | one `Dockerfile` per service |
| `frontend/nginx.conf` | now proxies `/api/` to `gateway` instead of `backend` |
| `k8s/deployment.yaml` | `k8s/deployments.yaml` (8 deployments) |

The public URLs the page uses (`/api/search`, `/api/chat`, ...) did not change; the gateway maps them:

| Public | Goes to |
|---|---|
| `/api/products*`, `/api/categories*` | catalog `/products*`, `/categories*` |
| `/api/search`, `/api/recommendations` | search `/search`, `/recommend` |
| `/api/chat` | chatbot `/chat` |
| `/api/cart/{session}/...` | cart `/cart/{session}/...` |
| `/api/orders`, `/api/quote` | order `/orders`, `/quote` |
| `/api/status`, `/api/version` | answered by the gateway itself |

## Repo layout
```
services/{gateway,catalog,search,chatbot,cart,order}/   app/main.py  requirements.txt  Dockerfile
frontend/        index.html  nginx.conf  Dockerfile
k8s/             deployments.yaml  services.yaml  hpa.yaml (optional autoscaling)
tests/smoke_test.py
docker-compose.yml  run_local.sh  .env.example  .github/workflows/deploy.yml
```

## 1. Run locally
**Docker (recommended):**
```bash
docker compose up --build         # page: http://localhost:8080   gateway: http://localhost:8000/api/status
python tests/smoke_test.py        # 11 end-to-end checks
```
**Without Docker:** `./run_local.sh` → http://localhost:8000 (cart/orders kept in memory, no Redis).

## 2. Deploy to Azure (AKS)
Same flow as before; only the build loop and manifest names change.
```bash
RG=shopai-rg; LOC=centralindia; ACR=shopaiacr$RANDOM; AKS=shopai-aks; TAG=m1
az login
az group create -n $RG -l $LOC
az acr create -g $RG -n $ACR --sku Basic

# build all 7 images in the cloud (no local Docker needed)
for s in gateway catalog search chatbot cart order; do az acr build -r $ACR -t shopai-$s:$TAG ./services/$s; done
az acr build -r $ACR -t shopai-frontend:$TAG ./frontend

az aks create -g $RG -n $AKS --node-count 2 --node-vm-size Standard_B2s --attach-acr $ACR --generate-ssh-keys
az aks get-credentials -g $RG -n $AKS

sed -i "s|ACR_NAME|$ACR|g; s|IMAGE_TAG|$TAG|g" k8s/deployments.yaml   # macOS: sed -i '' ...

# optional: Azure OpenAI for the chatbot (chatbot reads these; bot falls back to rules without them)
kubectl create secret generic shopai-secrets \
  --from-literal=AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com/" \
  --from-literal=AZURE_OPENAI_API_KEY="<key>" --from-literal=AZURE_OPENAI_DEPLOYMENT="<deployment>"

kubectl apply -f k8s/deployments.yaml -f k8s/services.yaml
kubectl get pods -w                       # wait until all are Running/Ready
kubectl get service frontend --watch      # EXTERNAL-IP = your public address
python tests/smoke_test.py http://<EXTERNAL-IP>
```
> 8 deployments × 2 pods is a lot for 2× `Standard_B2s`. If pods stay `Pending`, run `kubectl scale deploy --all --replicas=1`
> (keep redis at 1) or use `--node-count 3`.

**Update ONE service** (this is the point of microservices):
```bash
TAG=m2
az acr build -r $ACR -t shopai-chatbot:$TAG ./services/chatbot
kubectl set image deploy/chatbot chatbot=$ACR.azurecr.io/shopai-chatbot:$TAG
kubectl rollout status deploy/chatbot       # catalog, cart, order... are untouched
```
Other handy commands: `kubectl logs deploy/search`, `kubectl scale deploy/chatbot --replicas=4`, `kubectl apply -f k8s/hpa.yaml`.
Clean up: `az group delete -n $RG --yes --no-wait`.

## 3. CI/CD
`.github/workflows/deploy.yml` builds all 7 images tagged with the commit SHA and rolls out every deployment.
Secrets needed are unchanged: `AZURE_CREDENTIALS`, `ACR_NAME`, `RESOURCE_GROUP`, `CLUSTER_NAME`.

## 4. How the services behave when something fails
| If this is down | What the user sees |
|---|---|
| search | product grid/recommendations fail; chat says "can't reach the product service"; cart, checkout still work |
| chatbot | chat says unavailable; rest of the shop works |
| cart / redis | cart button shows "unavailable"; browsing, search, chat work |
| order | checkout fails with a clear message; cart is NOT cleared |
| any service | gateway returns 503 `"<name>-service is unreachable"`; `/api/status` shows which one |

## 5. Known limits (honest list)
- **Redis (carts) is a single pod without a persistent volume**: restarting it empties carts (orders are safe in PostgreSQL). **MongoDB and PostgreSQL are single pods on one disk each**: no replication or backups. For production use Azure Cosmos DB for MongoDB and Azure Database for PostgreSQL, and just change `MONGO_URL` / `ORDER_DB_*`.
- **Payment is a demo** (no gateway call). **No login:** the cart is tied to an anonymous browser session id, and anyone who knows a session id can read that cart/orders. Add an auth service before real use.
- Search keeps its index in memory, built from catalog at startup; `POST /reload` on search rebuilds it after catalog changes.
- Services trust each other inside the cluster (no mTLS / NetworkPolicy yet).

## 6. Ideas for service #7+
`user/auth` (JWT login), `payment` (Razorpay/Stripe sandbox), `inventory` (stock counts, reserve on checkout),
`notification` (order email/SMS via a queue), `review` (ratings), `analytics` (consume view/purchase events to improve recommendations).

## 7. Databases (MongoDB + PostgreSQL)
- **catalog -> MongoDB** (`MONGO_URL`). Collection `shopai.products`; seeded from `products.json` on first start only. New products: `POST /api/products` with header `X-Admin-Key` (needs `ADMIN_KEY`), then `POST` search `/reload` so the search index sees them.
- **order -> PostgreSQL** (`ORDER_DB_HOST`, `ORDER_DB_PORT`, `ORDER_DB_USER`, `ORDER_DB_PASSWORD`, `ORDER_DB_NAME`). Tables `orders` and `order_items` are created automatically. An order and its items are saved in one transaction.
- Without these variables each service falls back to memory mode, so the project still runs with nothing installed.
- Docker: `docker compose up --build` starts both databases with persistent volumes. Local Windows: see `run_local.ps1`.
- AKS: create the secret once, then apply `k8s/databases.yaml` before the deployments:
  `kubectl create secret generic shopai-db-secrets --from-literal=POSTGRES_PASSWORD='...'`
- `/health` is liveness (never fails because a DB is down); `/ready` is readiness; `/api/status` reports a service as down if its database is unreachable.
#   s h o p a i  
 