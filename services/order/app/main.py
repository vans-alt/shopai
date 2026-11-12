"""order-service: turns a cart into an order (checkout). Talks to cart-service.
Storage: MySQL when ORDER_DB_HOST is set (tables `orders` + `order_items`, created automatically);
otherwise in memory. Payment is a demo: no real money moves."""
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

CART_URL = os.getenv("CART_URL", "http://localhost:8004")
DB_HOST = os.getenv("ORDER_DB_HOST", "")
DB_PORT = int(os.getenv("ORDER_DB_PORT", "3306"))
DB_USER = os.getenv("ORDER_DB_USER", "shopai")
DB_PASSWORD = os.getenv("ORDER_DB_PASSWORD", "")
DB_NAME = os.getenv("ORDER_DB_NAME", "shopai")
# NB: env names are ORDER_DB_* on purpose. Kubernetes injects MYSQL_PORT=tcp://... for a Service named "mysql",
# which would break int(os.getenv("MYSQL_PORT")).
FREE_SHIPPING_ABOVE, SHIPPING_FEE, COD_LIMIT = 999, 49, 10000
COUPONS = {"WELCOME10": 0.10}
PAYMENTS = {"upi", "card", "netbanking", "cod"}
app = FastAPI(title="order-service")

DDL = [
    """CREATE TABLE IF NOT EXISTS orders (
        order_id   VARCHAR(20)  PRIMARY KEY,
        session_id VARCHAR(64)  NOT NULL,
        customer   VARCHAR(80)  NOT NULL,
        address    VARCHAR(300) NOT NULL,
        payment    VARCHAR(20)  NOT NULL,
        status     VARCHAR(20)  NOT NULL,
        coupon     VARCHAR(20)  NULL,
        subtotal   INT NOT NULL,
        discount   INT NOT NULL,
        shipping   INT NOT NULL,
        total      INT NOT NULL,
        created_at BIGINT NOT NULL,
        INDEX idx_orders_session (session_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS order_items (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id   VARCHAR(20)  NOT NULL,
        product_id INT          NOT NULL,
        name       VARCHAR(200) NOT NULL,
        emoji      VARCHAR(16)  NOT NULL DEFAULT '',
        price      INT NOT NULL,
        qty        INT NOT NULL,
        line_total INT NOT NULL,
        INDEX idx_items_order (order_id),
        CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]
ORDER_COLS = ("order_id", "session_id", "customer", "address", "payment", "status", "coupon",
              "subtotal", "discount", "shipping", "total", "created_at")
ITEM_COLS = ("product_id", "name", "emoji", "price", "qty", "line_total")


class MemoryStore:
    mode = "memory"

    def __init__(self):
        self.orders: Dict[str, dict] = {}
        self.by_session: Dict[str, List[str]] = {}

    def save(self, o: dict):
        self.orders[o["order_id"]] = o
        self.by_session.setdefault(o["session_id"], []).insert(0, o["order_id"])

    def get(self, oid: str) -> Optional[dict]:
        return self.orders.get(oid)

    def for_session(self, sid: str) -> List[dict]:
        return [self.orders[i] for i in self.by_session.get(sid, [])]

    def ping(self) -> bool:
        return True


class MySQLStore:
    mode = "mysql"

    def __init__(self):
        self.ready = False
        self.lock = threading.Lock()

    def _conn(self):
        import pymysql
        return pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
                               charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                               connect_timeout=5, autocommit=False)

    def init(self):
        """Create tables if missing. Raises if MySQL is unreachable."""
        with self.lock:
            if self.ready:
                return
            conn = self._conn()
            try:
                with conn.cursor() as cur:
                    for stmt in DDL:
                        cur.execute(stmt)
                conn.commit()
                self.ready = True
                print(f"[order] MySQL ready ({DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}), tables ensured")
            finally:
                conn.close()

    def _db(self, fn):
        """Run fn(cursor) in one connection/transaction. DB trouble -> clean 503."""
        try:
            self.init()
            conn = self._conn()
            try:
                with conn.cursor() as cur:
                    out = fn(cur)
                conn.commit()
                return out
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except HTTPException:
            raise
        except Exception as e:
            print("[order] MySQL error:", repr(e))
            raise HTTPException(503, f"order database unavailable ({type(e).__name__})")

    @staticmethod
    def _items(cur, ids):
        if not ids:
            return {}
        cur.execute("SELECT order_id, " + ", ".join(ITEM_COLS) + " FROM order_items WHERE order_id IN ("
                    + ",".join(["%s"] * len(ids)) + ") ORDER BY id", list(ids))
        out: Dict[str, list] = {}
        for r in cur.fetchall():
            out.setdefault(r.pop("order_id"), []).append(r)
        return out

    def save(self, o: dict):
        def run(cur):  # one transaction: order + all its items, or nothing
            cur.execute("INSERT INTO orders (" + ", ".join(ORDER_COLS) + ") VALUES (" + ",".join(["%s"] * len(ORDER_COLS)) + ")",
                        [o.get(c) for c in ORDER_COLS])
            cur.executemany("INSERT INTO order_items (order_id, " + ", ".join(ITEM_COLS) + ") VALUES (%s,"
                            + ",".join(["%s"] * len(ITEM_COLS)) + ")",
                            [[o["order_id"]] + [i.get(c, "") for c in ITEM_COLS] for i in o["items"]])
        self._db(run)

    def get(self, oid: str) -> Optional[dict]:
        def run(cur):
            cur.execute("SELECT " + ", ".join(ORDER_COLS) + " FROM orders WHERE order_id = %s", [oid])
            row = cur.fetchone()
            if not row:
                return None
            return {**row, "items": self._items(cur, [oid]).get(oid, [])}
        return self._db(run)

    def for_session(self, sid: str) -> List[dict]:
        def run(cur):
            cur.execute("SELECT " + ", ".join(ORDER_COLS) + " FROM orders WHERE session_id = %s "
                        "ORDER BY created_at DESC, order_id DESC LIMIT 50", [sid])
            rows = cur.fetchall()
            items = self._items(cur, [r["order_id"] for r in rows])
            return [{**r, "items": items.get(r["order_id"], [])} for r in rows]
        return self._db(run)

    def ping(self) -> bool:
        def run(cur):
            cur.execute("SELECT 1")
            return True
        return self._db(run)


store = MySQLStore() if DB_HOST else MemoryStore()


@app.on_event("startup")
def startup():
    if not DB_HOST:
        return

    def init_with_retry():  # MySQL takes 10-30 s to boot on first start
        for attempt in range(20):
            try:
                store.init()
                return
            except Exception as e:
                print(f"[order] MySQL not ready ({type(e).__name__}: {e}); retry {attempt + 1}/20")
                time.sleep(3)
    threading.Thread(target=init_with_retry, daemon=True).start()


class CheckoutIn(BaseModel):
    session_id: str = Field(..., min_length=3, max_length=64)
    name: str = Field(..., min_length=2, max_length=80)
    address: str = Field(..., min_length=5, max_length=300)
    payment: str = "upi"
    coupon: Optional[str] = None


def price_order(subtotal: int, coupon: Optional[str]):
    code = (coupon or "").strip().upper()
    if code and code not in COUPONS:
        raise HTTPException(400, f"Invalid coupon '{code}'")
    discount = int(subtotal * COUPONS[code]) if code else 0
    after = subtotal - discount
    shipping = 0 if after >= FREE_SHIPPING_ABOVE else SHIPPING_FEE
    return {"subtotal": subtotal, "discount": discount, "shipping": shipping, "total": after + shipping,
            "coupon": code or None}


@app.get("/health")  # liveness: never fails because MySQL is down (would cause restart loops in Kubernetes)
def health():
    try:
        ok = store.ping()
    except Exception:
        ok = False
    return {"status": "ok", "service": "order", "store": store.mode, "store_ok": ok}


@app.get("/ready")
def ready():
    store.ping()  # raises 503 if MySQL is unreachable
    return {"ready": True, "store": store.mode}


@app.post("/orders")
def checkout(body: CheckoutIn):
    pay = body.payment.lower()
    if pay not in PAYMENTS:
        raise HTTPException(400, f"Payment must be one of {sorted(PAYMENTS)}")
    try:
        r = httpx.get(f"{CART_URL}/cart/{body.session_id}", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503, "cart-service unreachable, order NOT placed")
    cart = r.json()
    if not cart["items"]:
        raise HTTPException(400, "Cart is empty")
    pricing = price_order(cart["subtotal"], body.coupon)
    if pay == "cod" and pricing["total"] > COD_LIMIT:
        raise HTTPException(400, f"Cash on Delivery is available up to ₹{COD_LIMIT:,}")
    order = {"order_id": "ORD-" + uuid.uuid4().hex[:8].upper(), "session_id": body.session_id,
             "customer": body.name, "address": body.address, "payment": pay,
             "status": "PAID" if pay != "cod" else "PLACED", "created_at": int(time.time()),
             "items": cart["items"], **pricing}
    store.save(order)  # if the database is down this raises 503 and the cart is NOT cleared
    try:  # order is already saved; a failed cart clear must not fail the checkout
        httpx.delete(f"{CART_URL}/cart/{body.session_id}", timeout=5)
    except httpx.HTTPError:
        print("[order] warning: could not clear cart for", body.session_id)
    return order


@app.get("/orders")
def list_orders(session_id: str):
    return store.for_session(session_id)


@app.get("/orders/{oid}")
def get_order(oid: str):
    o = store.get(oid)
    if not o:
        raise HTTPException(404, "Order not found")
    return o


@app.post("/quote")  # price preview (coupon/shipping) without placing an order
def quote(session_id: str, coupon: Optional[str] = None):
    r = httpx.get(f"{CART_URL}/cart/{session_id}", timeout=5)
    r.raise_for_status()
    return price_order(r.json()["subtotal"], coupon)
