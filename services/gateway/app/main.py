"""api-gateway: the ONLY backend the browser talks to. Routes /api/* to the right microservice,
adds a request id, and exposes /api/status (health of every service) and /api/version."""
import asyncio
import os
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

APP_VERSION = os.getenv("APP_VERSION", "v4-microservices")
SERVICES = {
    "catalog": os.getenv("CATALOG_URL", "http://localhost:8001"),
    "search": os.getenv("SEARCH_URL", "http://localhost:8002"),
    "chatbot": os.getenv("CHATBOT_URL", "http://localhost:8003"),
    "cart": os.getenv("CART_URL", "http://localhost:8004"),
    "order": os.getenv("ORDER_URL", "http://localhost:8005"),
}
# public prefix  ->  (service, prefix inside that service)
ROUTES = [
    ("/api/products", "catalog", "/products"),
    ("/api/categories", "catalog", "/categories"),
    ("/api/search", "search", "/search"),
    ("/api/recommendations", "search", "/recommend"),
    ("/api/chat", "chatbot", "/chat"),
    ("/api/cart", "cart", "/cart"),
    ("/api/orders", "order", "/orders"),
    ("/api/quote", "order", "/quote"),
]
app = FastAPI(title="api-gateway")
client = httpx.AsyncClient(timeout=15.0)
HOP = {"host", "content-length", "connection", "transfer-encoding", "content-encoding"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "gateway"}


@app.get("/api/version")
def version():
    return {"version": APP_VERSION, "architecture": "microservices", "services": list(SERVICES) + ["gateway"],
            "features": ["categories", "smart-chat", "tool-calling-llm", "cart", "orders"]}


@app.get("/api/status")
async def status():
    async def check(name, url):
        try:
            r = await client.get(f"{url}/health", timeout=3)
            d = r.json()
            return name, {"up": r.status_code == 200 and d.get("store_ok", True), "detail": d}
        except Exception as e:
            return name, {"up": False, "detail": str(e)[:120]}
    res = dict(await asyncio.gather(*(check(n, u) for n, u in SERVICES.items())))
    res["gateway"] = {"up": True}
    return {"all_up": all(v["up"] for v in res.values()), "services": res}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    full = "/api/" + path
    for prefix, svc, inner in ROUTES:
        if full == prefix or full.startswith(prefix + "/"):
            target = SERVICES[svc] + inner + full[len(prefix):]
            break
    else:
        return Response('{"detail":"Unknown API route"}', 404, media_type="application/json")
    rid = request.headers.get("x-request-id", uuid.uuid4().hex[:12])
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["x-request-id"] = rid
    try:
        up = await client.request(request.method, target, params=request.query_params,
                                  content=await request.body(), headers=headers)
    except httpx.ConnectError:
        return Response(f'{{"detail":"{svc}-service is unreachable"}}', 503, media_type="application/json",
                        headers={"x-request-id": rid})
    except httpx.TimeoutException:
        return Response(f'{{"detail":"{svc}-service timed out"}}', 504, media_type="application/json",
                        headers={"x-request-id": rid})
    out = {k: v for k, v in up.headers.items() if k.lower() not in HOP}
    out["x-request-id"] = rid
    out["x-served-by"] = f"{svc}-service"
    return Response(up.content, up.status_code, headers=out)


# Local no-Docker convenience only: run_local.sh sets FRONTEND_DIR so http://localhost:8000 shows the page.
# (In Docker/Kubernetes the frontend is its own nginx container, so this stays off.)
_fd = os.getenv("FRONTEND_DIR")
if _fd and Path(_fd).is_dir():
    app.mount("/", StaticFiles(directory=_fd, html=True), name="frontend")
