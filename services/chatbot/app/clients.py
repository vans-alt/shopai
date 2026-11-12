"""HTTP clients: how chatbot-service talks to the other microservices.
Exposes the SAME names the old monolith imported (BY_ID, CATEGORY_META, search_products, recommend,
category_details) so chatbot.py needed only a one-line import change."""
import os
import time
from typing import List

import httpx

CATALOG_URL = os.getenv("CATALOG_URL", "http://localhost:8001")
SEARCH_URL = os.getenv("SEARCH_URL", "http://localhost:8002")
_http = httpx.Client(timeout=5.0)

# Keyword hints used by the rule-based intent parser (chat logic, so it lives with the chatbot).
CATEGORY_META = {
    "Electronics": {"emoji": "🔌", "description": "Phones, laptops, audio and smart gadgets.",
                    "words": ["electronic", "gadget", "phone", "mobile", "laptop", "headphone", "earphone", "earbud",
                              "speaker", "watch", "keyboard"]},
    "Fashion": {"emoji": "👗", "description": "Footwear, clothing and accessories.",
                "words": ["fashion", "cloth", "shirt", "shoe", "sneaker", "bag", "handbag", "purse"]},
    "Home": {"emoji": "🏠", "description": "Kitchen, cleaning and everyday home essentials.",
             "words": ["home", "kitchen", "cook", "vacuum", "bottle", "house"]},
    "Sports": {"emoji": "🏅", "description": "Fitness and workout gear.",
               "words": ["sport", "fitness", "gym", "yoga", "dumbbell", "workout", "exercise"]},
}


class _TTL:
    """Tiny cache so one chat turn doesn't hit catalog-service dozens of times."""
    def __init__(self, url_fn, ttl=30):
        self.url_fn, self.ttl, self.at, self.val = url_fn, ttl, 0.0, None

    def get(self):
        if self.val is None or time.time() - self.at > self.ttl:
            try:
                r = _http.get(self.url_fn())
                r.raise_for_status()
                self.val, self.at = r.json(), time.time()
            except Exception:
                if self.val is None:  # nothing cached to fall back on
                    raise
        return self.val


_products = _TTL(lambda: f"{CATALOG_URL}/products")
_details = _TTL(lambda: f"{CATALOG_URL}/categories/details")


class _ById:
    """dict-like view of the catalog: supports `i in BY_ID`, `BY_ID[i]`, `BY_ID.values()`."""
    def _d(self):
        return {p["id"]: p for p in _products.get()}

    def __contains__(self, k):
        return k in self._d()

    def __getitem__(self, k):
        return self._d()[k]

    def values(self):
        return self._d().values()


BY_ID = _ById()


def category_details() -> List[dict]:
    return _details.get()


def search_products(q="", category=None, min_price=None, max_price=None, sort="relevance", limit=24) -> List[dict]:
    params = {"q": q or "", "sort": sort, "limit": limit}
    if category:
        params["category"] = category
    if min_price is not None:
        params["min_price"] = int(min_price)
    if max_price is not None:
        params["max_price"] = int(max_price)
    r = _http.get(f"{SEARCH_URL}/search", params=params)
    r.raise_for_status()
    return r.json()


def recommend(ids: List[int], k=4) -> List[dict]:
    r = _http.get(f"{SEARCH_URL}/recommend", params={"ids": ",".join(map(str, ids)), "k": k})
    r.raise_for_status()
    return r.json()
