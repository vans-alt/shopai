"""search-service: typo-tolerant search + content-based recommendations.
It does NOT own product data: it downloads it from catalog-service and builds the TF-IDF index in memory."""
import os
import re
import threading
import time
from typing import List, Optional

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

CATALOG_URL = os.getenv("CATALOG_URL", "http://localhost:8001")
app = FastAPI(title="search-service")

SYNONYMS = {"earphones": "headphones", "earbuds": "headphones", "headset": "headphones",
            "mobile": "phone smartphone", "cellphone": "phone smartphone", "notebook": "laptop",
            "trainers": "shoes", "sneakers": "shoes", "tshirt": "shirt", "purse": "bag handbag",
            "workout": "gym fitness exercise", "cookware": "kitchen cooking", "cleaner": "vacuum cleaning"}

_lock = threading.Lock()
_state = {"products": [], "pos": {}, "by_id": {}, "docs": [], "vec": None, "matrix": None, "loaded_at": None}


def _expand(q: str) -> str:
    words = re.findall(r"[a-z0-9]+", q.lower())
    return " ".join(f"{w} {SYNONYMS[w]}" if w in SYNONYMS else w for w in words)


def _doc(p) -> str:  # name repeated so it weighs more than the description
    return f'{p["name"]} {p["name"]} {p["category"]} {p["tags"]} {p["description"]}'.lower()


def build_index(products: list) -> None:
    docs = [_doc(p) for p in products]
    # char n-grams make search tolerant to typos ("hedphones" still finds headphones)
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True)
    matrix = vec.fit_transform(docs)
    with _lock:
        _state.update(products=products, pos={p["id"]: i for i, p in enumerate(products)},
                      by_id={p["id"]: p for p in products}, docs=docs, vec=vec, matrix=matrix,
                      loaded_at=time.time())


def load_from_catalog(retries: int = 10, delay: float = 2.0) -> bool:
    for attempt in range(retries):
        try:
            r = httpx.get(f"{CATALOG_URL}/products", timeout=5)
            r.raise_for_status()
            build_index(r.json())
            print(f"[search] indexed {len(_state['products'])} products from {CATALOG_URL}")
            return True
        except Exception as e:  # catalog may still be starting (docker/k8s start order is random)
            print(f"[search] catalog not ready ({e}); retry {attempt + 1}/{retries}")
            if delay:
                time.sleep(delay)
    return False


@app.on_event("startup")
def startup():
    threading.Thread(target=load_from_catalog, daemon=True).start()  # don't block /health while waiting


def _ready():
    if _state["vec"] is None:
        if not load_from_catalog(retries=1, delay=0):
            raise HTTPException(503, "Search index not ready (catalog-service unreachable)")


@app.get("/health")
def health():
    return {"status": "ok", "service": "search", "indexed": len(_state["products"])}


@app.get("/ready")  # used by the k8s readiness probe: only ready once the index exists
def ready():
    if _state["vec"] is None:
        raise HTTPException(503, "index not built yet")
    return {"ready": True, "indexed": len(_state["products"])}


@app.post("/reload")
def reload_index():
    if not load_from_catalog(retries=1, delay=0):
        raise HTTPException(503, "catalog-service unreachable")
    return {"indexed": len(_state["products"])}


@app.get("/search")
def search(q: str = "", category: Optional[str] = None, min_price: Optional[int] = None,
           max_price: Optional[int] = None, sort: str = "relevance", limit: int = 24):
    _ready()
    with _lock:
        products, docs, vec, matrix = _state["products"], _state["docs"], _state["vec"], _state["matrix"]
    idx = list(range(len(products)))
    scores = np.zeros(len(products))
    if q and q.strip():
        qx = _expand(q)
        scores = cosine_similarity(vec.transform([qx]), matrix).ravel()
        toks = [t for t in re.findall(r"[a-z0-9]+", qx) if len(t) > 2]
        if toks:  # exact keyword hits get a boost on top of typo-tolerant similarity
            scores = scores + 0.5 * np.array([sum(t in d for t in toks) / len(toks) for d in docs])
        idx = [i for i in idx if scores[i] > 0.15]
    out = []
    for i in idx:
        p = products[i]
        if category and p["category"].lower() != category.lower():
            continue
        if min_price is not None and p["price"] < min_price:
            continue
        if max_price is not None and p["price"] > max_price:
            continue
        out.append((scores[i], p))
    if sort == "price_asc":
        out.sort(key=lambda x: x[1]["price"])
    elif sort == "price_desc":
        out.sort(key=lambda x: -x[1]["price"])
    elif q and q.strip():
        out.sort(key=lambda x: -x[0])
    return [dict(p, score=round(float(s), 3)) for s, p in out[:limit]]


@app.get("/recommend")
def recommend(ids: str = "", k: int = 4):
    _ready()
    with _lock:
        products, pos, by_id, matrix = _state["products"], _state["pos"], _state["by_id"], _state["matrix"]
    wanted: List[int] = [int(x) for x in ids.split(",") if x.strip().isdigit() and int(x) in by_id]
    if not wanted:
        return products[:k]
    profile = np.asarray(matrix[[pos[i] for i in wanted]].mean(axis=0))
    sims = cosine_similarity(profile, matrix).ravel()
    order = [i for i in np.argsort(-sims) if products[i]["id"] not in wanted]
    return [products[i] for i in order[:k]]
