"""catalog-service: owns the product data. Nobody else reads products.json."""
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

app = FastAPI(title="catalog-service")

PRODUCTS = json.loads((Path(__file__).parent / "products.json").read_text(encoding="utf-8"))
BY_ID = {p["id"]: p for p in PRODUCTS}

CATEGORY_META = {
    "Electronics": {"emoji": "🔌", "description": "Phones, laptops, audio and smart gadgets."},
    "Fashion": {"emoji": "👗", "description": "Footwear, clothing and accessories."},
    "Home": {"emoji": "🏠", "description": "Kitchen, cleaning and everyday home essentials."},
    "Sports": {"emoji": "🏅", "description": "Fitness and workout gear."},
}


@app.get("/health")
def health():
    return {"status": "ok", "service": "catalog", "products": len(PRODUCTS)}


@app.get("/products")
def products(category: Optional[str] = None, sort: str = "relevance"):
    items = [p for p in PRODUCTS if not category or p["category"].lower() == category.lower()]
    if sort == "price_asc":
        items = sorted(items, key=lambda p: p["price"])
    elif sort == "price_desc":
        items = sorted(items, key=lambda p: -p["price"])
    return items


@app.get("/products/batch")  # declared BEFORE /products/{pid} so "batch" is not parsed as an id
def products_batch(ids: str = ""):
    wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    return [BY_ID[i] for i in wanted if i in BY_ID]


@app.get("/products/{pid}")
def product(pid: int):
    if pid not in BY_ID:
        raise HTTPException(404, "Product not found")
    return BY_ID[pid]


@app.get("/categories")
def categories():
    return sorted({p["category"] for p in PRODUCTS})


@app.get("/categories/details")
def categories_details():
    out = []
    for name in categories():
        items = [p for p in PRODUCTS if p["category"] == name]
        meta = CATEGORY_META.get(name, {})
        out.append({"name": name, "emoji": meta.get("emoji", "🛒"), "description": meta.get("description", ""),
                    "count": len(items), "min_price": min(p["price"] for p in items),
                    "max_price": max(p["price"] for p in items), "top_products": [p["name"] for p in items[:3]]})
    return out


@app.get("/categories/{name}/products")
def category_products(name: str, sort: str = "relevance"):
    items = products(category=name, sort=sort)
    if not items:
        raise HTTPException(404, "Category not found")
    return items
