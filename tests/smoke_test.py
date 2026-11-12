"""End-to-end check of the RUNNING system through the gateway. No extra libraries needed.
Usage:  python tests/smoke_test.py [base_url]     (default http://localhost:8080 ; AKS: http://<EXTERNAL-IP>)"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
SID = "smoketest-session"
fails = 0


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(name, ok, extra=""):
    global fails
    print(("PASS" if ok else "FAIL"), "-", name, extra)
    fails += 0 if ok else 1


c, d = call("GET", "/api/status");                       check("all services up", c == 200 and d["all_up"], str({k: v["up"] for k, v in d["services"].items()}))
c, d = call("GET", "/api/products");                     check("catalog: 14 products", c == 200 and len(d) == 14)
c, d = call("GET", "/api/categories/details");           check("catalog: category details", c == 200 and len(d) >= 4)
c, d = call("GET", "/api/search?q=hedphones");           check("search: typo tolerant", c == 200 and "Headphones" in d[0]["name"])
c, d = call("GET", "/api/recommendations?ids=1&k=3");    check("search: recommendations", c == 200 and len(d) == 3)
c, d = call("POST", "/api/chat", {"message": "running shoes under 3000", "history": [], "context": {}}); check("chatbot: product search", c == 200 and d["products"])
call("DELETE", f"/api/cart/{SID}")
c, d = call("POST", f"/api/cart/{SID}/items", {"product_id": 5, "qty": 2}); check("cart: add item", c == 200 and d["count"] == 2)
c, d = call("POST", f"/api/quote?session_id={SID}&coupon=WELCOME10");       check("order: coupon quote", c == 200 and d["discount"] > 0)
c, d = call("POST", "/api/orders", {"session_id": SID, "name": "Smoke Test", "address": "1 Test Street", "payment": "upi", "coupon": "WELCOME10"})
check("order: checkout", c == 200 and d["order_id"].startswith("ORD-"), d.get("order_id", ""))
c, d = call("GET", f"/api/cart/{SID}");                  check("cart: emptied after checkout", c == 200 and d["count"] == 0)
c, d = call("GET", f"/api/orders?session_id={SID}");     check("order: history", c == 200 and len(d) >= 1)
print("\nALL PASSED" if not fails else f"\n{fails} CHECK(S) FAILED")
sys.exit(1 if fails else 0)
