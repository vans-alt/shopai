"""cart-service: one cart per browser session. Stored in Redis (shared by all replicas); falls back to memory
when REDIS_URL is not set (handy for local runs / tests)."""
import json
import os
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

CATALOG_URL = os.getenv("CATALOG_URL", "http://localhost:8001")
REDIS_URL = os.getenv("REDIS_URL", "")
CART_TTL = int(os.getenv("CART_TTL_SECONDS", str(7 * 24 * 3600)))
app = FastAPI(title="cart-service")


class Store:
    def __init__(self):
        self.mem: Dict[str, dict] = {}
        self.r = None
        if REDIS_URL:
            import redis  # imported lazily so local runs don't need the package
            self.r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)

    def get(self, sid: str) -> dict:
        if self.r:
            raw = self.r.get(f"cart:{sid}")
            return json.loads(raw) if raw else {}
        return self.mem.get(sid, {})

    def put(self, sid: str, cart: dict):
        if self.r:
            if cart:
                self.r.set(f"cart:{sid}", json.dumps(cart), ex=CART_TTL)
            else:
                self.r.delete(f"cart:{sid}")
        elif cart:
            self.mem[sid] = cart
        else:
            self.mem.pop(sid, None)

    def ping(self) -> bool:
        return self.r.ping() if self.r else True


store = Store()


def _products(ids):
    if not ids:
        return {}
    r = httpx.get(f"{CATALOG_URL}/products/batch", params={"ids": ",".join(ids)}, timeout=5)
    r.raise_for_status()
    return {str(p["id"]): p for p in r.json()}


def _view(sid: str, cart: dict) -> dict:
    prods = _products(list(cart))
    lines = []
    for pid, qty in cart.items():
        p = prods.get(pid)
        if p:  # product removed from catalog -> silently dropped from the view
            lines.append({"product_id": p["id"], "name": p["name"], "emoji": p["emoji"], "price": p["price"],
                          "qty": qty, "line_total": p["price"] * qty})
    return {"session_id": sid, "items": lines, "count": sum(l["qty"] for l in lines),
            "subtotal": sum(l["line_total"] for l in lines)}


class AddIn(BaseModel):
    product_id: int
    qty: int = Field(1, ge=1, le=20)


class QtyIn(BaseModel):
    qty: int = Field(..., ge=0, le=20)


@app.get("/health")  # liveness: never fails because Redis is down
def health():
    try:
        ok = store.ping()
    except Exception:
        ok = False
    return {"status": "ok", "service": "cart", "store": "redis" if store.r else "memory", "store_ok": ok}


@app.get("/ready")
def ready():
    try:
        store.ping()
    except Exception as e:
        raise HTTPException(503, f"store down: {e}")
    return {"ready": True}


@app.get("/cart/{sid}")
def get_cart(sid: str):
    return _view(sid, store.get(sid))


@app.post("/cart/{sid}/items")
def add_item(sid: str, body: AddIn):
    try:
        r = httpx.get(f"{CATALOG_URL}/products/{body.product_id}", timeout=5)
    except httpx.HTTPError:
        raise HTTPException(503, "catalog-service unreachable")
    if r.status_code == 404:
        raise HTTPException(404, "Product not found")
    r.raise_for_status()
    cart = store.get(sid)
    key = str(body.product_id)
    cart[key] = min(20, cart.get(key, 0) + body.qty)
    store.put(sid, cart)
    return _view(sid, cart)


@app.put("/cart/{sid}/items/{pid}")
def set_qty(sid: str, pid: int, body: QtyIn):
    cart = store.get(sid)
    if body.qty == 0:
        cart.pop(str(pid), None)
    elif str(pid) in cart:
        cart[str(pid)] = body.qty
    else:
        raise HTTPException(404, "Item not in cart")
    store.put(sid, cart)
    return _view(sid, cart)


@app.delete("/cart/{sid}/items/{pid}")
def remove_item(sid: str, pid: int):
    cart = store.get(sid)
    cart.pop(str(pid), None)
    store.put(sid, cart)
    return _view(sid, cart)


@app.delete("/cart/{sid}")
def clear_cart(sid: str):
    store.put(sid, {})
    return _view(sid, {})
