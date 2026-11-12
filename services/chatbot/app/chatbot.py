"""ShopAI chatbot.

Two modes, same API:
  * AI mode    - Azure OpenAI with tool-calling (search_products, compare_products, list_categories, add_to_cart)
                 Enabled when AZURE_OPENAI_ENDPOINT / _API_KEY / _DEPLOYMENT are set.
  * Smart mode - rule-based assistant with conversation memory (no API key needed). Used as fallback.

The server is stateless: the browser sends back the `context` returned by the previous reply.
"""
import json
import os
import re
from typing import Dict, List

# In the monolith these were Python imports from catalog.py. Now they are HTTP calls (see clients.py).
from .clients import BY_ID, CATEGORY_META, category_details, recommend, search_products

# ---- Sample store policies: EDIT these to match your real store ------------------------------------------------
POLICIES = {
    "shipping": "Delivery takes 3-5 business days across India. It is free above ₹999, otherwise ₹49.",
    "returns": "You can return unused items within 7 days in original packaging. Refunds reach your original payment method in 5-7 days.",
    "payment": "We accept UPI, credit/debit cards, net banking and Cash on Delivery (up to ₹10,000).",
    "warranty": "Electronics carry a 1-year manufacturer warranty.",
    "tracking": "You can track your order from the confirmation email or the 'My orders' page.",
    "offers": "Use code WELCOME10 for 10% off your first order.",
    "support": "Our support team is available 9am-9pm IST at support@shopai.example.",
}
FAQ_PATTERNS = [
    ("shipping", r"\b(ship|shipping|shipped|deliver|delivery|dispatch)\b"),
    ("returns", r"\b(return|returns|refund|refunds|exchange|replacement)\b"),
    ("payment", r"\b(payment|payments|cod|cash on delivery|upi|emi|credit card|debit card|net banking)\b|\bhow (do|can) i pay\b"),
    ("warranty", r"\b(warranty|guarantee)\b"),
    ("tracking", r"\btrack(ing)? (my|the|an) order\b|\border status\b|\bwhere is my order\b"),
    ("offers", r"\b(discount|discounts|offer|offers|coupon|coupons|promo|promo code|deals?)\b"),
    ("support", r"\b(contact|customer (care|support)|talk to (a )?(human|agent)|complaint)\b"),
]

inr = lambda n: f"₹{int(n):,}"  # noqa: E731

# ---- Intent parsing --------------------------------------------------------------------------------------------
STOP = set("""show me find get want need looking look for a an the some any of to in on with and or is are under below
above over less more than within upto up budget rs inr rupees good best buy please can you suggest recommend i my gift
something products product item items what which between least most cheap cheapest cheaper affordable inexpensive premium
expensive costlier pricier another other options option similar ones one it this that them these compare add cart tell
about details k thousand ok okay yes no sure hmm hmmm thanks thank do does have has any there give list display let us
lets also too just only really very like would could should will am im i'm it's its essentials stuff things gear""".split())

NUM = r"([\d][\d,]*(?:\.\d+)?)\s*(k|thousand)?"
CUR = r"\s*(?:rs\.?|inr)?\s*"
MAX_RE = re.compile(r"(?:under|below|less than|within|upto|up to|max(?:imum)?|budget(?: of| is)?|not more than|at most)" + CUR + NUM)
MIN_RE = re.compile(r"(?:above|over|more than|min(?:imum)?|at least|starting (?:from|at))" + CUR + NUM)
BETWEEN_RE = re.compile(r"between" + CUR + NUM + r"\s*(?:and|to|-)" + CUR + NUM)

ADD_RE = re.compile(r"\b(add|put)\b.*\b(cart|basket)\b|\badd (it|this|that|them|these)\b|\b(buy|order|purchase) (it|this|that|them|the)\b")
COMPARE_RE = re.compile(r"\b(compare|comparison|difference between|versus|vs\.?)\b")
CHEAPER_RE = re.compile(r"\b(cheaper|less expensive|lower price|more affordable|budget friendly|reduce the price)\b")
PRICIER_RE = re.compile(r"\b(more expensive|pricier|costlier|higher end|better quality|upgrade|premium)\b")
MORE_RE = re.compile(r"\b(more options|another|other options|other ones|show more|anything else|something else|different ones?)\b")
DETAILS_RE = re.compile(r"\b(tell me (more )?about|more (info|details)|details|specs|describe|features of|is it good|worth it)\b")
SIMILAR_RE = re.compile(r"\b(similar|alternatives?|like (it|this|that)|something like)\b")
GREET_RE = re.compile(r"(hi+|hello+|hey+|namaste|good (morning|afternoon|evening))\W*")
CATS_RE = re.compile(r"categor|what do you (sell|have)|what.s available|browse|catalog")
ORD = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "last": -1}


def _amount(num: str, unit) -> int:
    n = float(num.replace(",", ""))
    return int(n * 1000) if unit in ("k", "thousand") else int(n)


def parse_intent(m: str) -> Dict:
    m = m.lower().replace("₹", " ")
    max_p = min_p = None
    b = BETWEEN_RE.search(m)
    if b:
        min_p, max_p = _amount(b.group(1), b.group(2)), _amount(b.group(3), b.group(4))
    else:
        r = MAX_RE.search(m)
        if r:
            max_p = _amount(r.group(1), r.group(2))
        r = MIN_RE.search(m)
        if r:
            min_p = _amount(r.group(1), r.group(2))
    category = next((n for n, meta in CATEGORY_META.items() if n.lower() in m or any(w in m for w in meta["words"])), None)
    cat_words = {category.lower(), category.lower().rstrip("s")} if category else set()
    q = " ".join(t for t in re.findall(r"[a-z]+", re.sub(r"[\d,\.]+", " ", m)) if t not in STOP and t not in cat_words)
    return {"q": q, "category": category, "min_price": min_p, "max_price": max_p,
            "cheapest": bool(re.search(r"\b(cheap|cheapest|lowest|inexpensive)\b", m)),
            "premium": bool(re.search(r"\b(premium|expensive|high[- ]?end|luxury)\b", m))}


def find_named(m: str) -> List[int]:
    return [p["id"] for p in BY_ID.values() if p["name"].lower() in m]


def resolve_targets(m: str, last: List[int]) -> List[int]:
    named = find_named(m)
    if named:
        return named
    out = []
    for word, idx in ORD.items():
        if re.search(rf"\b{re.escape(word)}\b", m) and last and (idx == -1 or idx < len(last)):
            out.append(last[idx])
    if not out and last:
        if re.search(r"\b(both|them|these|all)\b", m):
            out = last[:2]
        elif re.search(r"\b(it|this|that|one)\b", m):
            out = [last[0]]
    return list(dict.fromkeys(out))


# ---- Reply helpers ---------------------------------------------------------------------------------------------
def result(reply, products=None, suggestions=None, actions=None, ctx=None, mode="smart"):
    return {"reply": reply, "products": products or [], "suggestions": suggestions or [],
            "actions": actions or [], "context": ctx or {}, "mode": mode}


def ctx_for(hits, q="", category=None, prev=None):
    ids = [h["id"] for h in hits]
    seen = list(dict.fromkeys((prev or {}).get("seen", []) + ids))[-30:]
    return {"last_ids": ids, "q": q, "category": category, "seen": seen}


def overview(ctx, greeting=False):
    lines = "\n".join(f'{c["emoji"]} {c["name"]} – {c["count"]} items ({inr(c["min_price"])} to {inr(c["max_price"])})'
                      for c in category_details())
    intro = "Hi! I'm ShopAI, your shopping assistant. " if greeting else ""
    return result(f"{intro}Here's what we sell:\n{lines}\n\nTell me what you need, for example \"running shoes under 3000\".",
                  suggestions=["Best under ₹3000", "Gym essentials", "Cheapest electronics", "Delivery time?"], ctx=ctx)


def compare_reply(ids):
    ps = [BY_ID[i] for i in ids[:3]]
    lines = "\n".join(f'• {p["name"]} – {inr(p["price"])} ({p["category"]}): {p["description"]}' for p in ps)
    lo, hi = min(ps, key=lambda p: p["price"]), max(ps, key=lambda p: p["price"])
    tail = ""
    if lo is not hi:
        tail = f'\n\n{lo["name"]} is {inr(hi["price"] - lo["price"])} cheaper; {hi["name"]} is the higher-priced option.'
    return f"Here's a side-by-side:\n{lines}{tail}", ps


def results_reply(hits, q, cat, min_p, max_p, note="", label="best matches"):
    parts = []
    if cat:
        parts.append(f"in {cat}")
    if max_p:
        parts.append(f"under {inr(max_p)}")
    if min_p:
        parts.append(f"above {inr(min_p)}")
    head = note or (f"Here are the {label}" + (f' for "{q}"' if q else "") + (" " + " ".join(parts) if parts else "") + ":")
    top = hits[0]
    rest = "\n".join(f'• {p["name"]} – {inr(p["price"])}' for p in hits[1:])
    body = f'{head}\n⭐ {top["name"]} – {inr(top["price"])}\n{top["description"]}'
    if rest:
        body += f"\n\nAlso consider:\n{rest}"
    return body + "\n\nWant me to narrow it down, compare them, or add one to your cart?"


# ---- Smart (rule-based) mode -----------------------------------------------------------------------------------
def rules(message: str, ctx: Dict) -> Dict:
    m = message.lower().strip()
    last = [i for i in ctx.get("last_ids", []) if i in BY_ID]

    if GREET_RE.fullmatch(m):
        return overview(ctx, greeting=True)
    if CATS_RE.search(m):
        return overview(ctx)
    if re.search(r"\b(thanks|thank you|thx)\b", m):
        return result("You're welcome! Anything else I can help you find?", ctx=ctx,
                      suggestions=["What do you sell?", "Show cheaper ones"] if last else ["What do you sell?"])
    if re.fullmatch(r"(bye|goodbye|see you)\W*", m):
        return result("Goodbye! Happy shopping 🛍️", ctx=ctx)
    if re.search(r"what can you do|who are you|\bhelp\b", m):
        return result("I can search products, filter by category and budget, compare items, add things to your cart "
                      "and answer questions about delivery, returns, payments and offers.", ctx=ctx,
                      suggestions=["What do you sell?", "Best under ₹3000", "Return policy"])

    it = parse_intent(m)
    targets = resolve_targets(m, last)

    for key, pat in FAQ_PATTERNS:
        if re.search(pat, m) and not it["category"] and not find_named(m):
            return result(POLICIES[key] + " Anything else I can help with?", ctx=ctx,
                          suggestions=["What do you sell?", "Payment options", "Return policy"])

    if ADD_RE.search(m):
        if not targets:
            return result("Which product should I add? Search for something first, or tell me its name.", ctx=ctx,
                          suggestions=["What do you sell?"])
        names = ", ".join(BY_ID[i]["name"] for i in targets)
        return result(f"Done! Added {names} to your cart 🛒.", products=[BY_ID[i] for i in targets],
                      actions=[{"type": "add_to_cart", "product_id": i} for i in targets], ctx=ctx,
                      suggestions=["Show similar items", "Delivery time?", "Show cheaper ones"])

    if COMPARE_RE.search(m):
        ids = targets if len(targets) >= 2 else last[:2]
        if len(ids) < 2:
            return result("Tell me which two products to compare, or search first and I'll compare the top results.",
                          ctx=ctx, suggestions=["Best under ₹3000"])
        text, ps = compare_reply(ids)
        return result(text, products=ps, ctx={**ctx, "last_ids": [p["id"] for p in ps]},
                      suggestions=[f'Add {ps[0]["name"]} to cart', "Show cheaper ones"])

    refine_cheaper = bool(CHEAPER_RE.search(m))
    refine_pricier = bool(PRICIER_RE.search(m)) and not it["q"] and not it["category"]
    refine_more = bool(MORE_RE.search(m))

    if DETAILS_RE.search(m) and targets and not (refine_cheaper or refine_more):
        p = BY_ID[targets[0]]
        ship = "Free delivery." if p["price"] >= 999 else "Delivery ₹49 (free above ₹999)."
        text = (f'{p["name"]} – {inr(p["price"])}\n{p["description"]}\nCategory: {p["category"]} · '
                f'Keywords: {p["tags"].replace(" ", ", ")}\n{ship}')
        return result(text, products=[p], ctx={**ctx, "last_ids": [p["id"]] + [i for i in last if i != p["id"]]},
                      suggestions=["Add it to cart", "Show similar items", "Show cheaper ones"])

    if SIMILAR_RE.search(m) and (targets or last):
        base = targets or last
        hits = recommend(base, 4)
        return result("Here are similar items you might like:\n" + "\n".join(f'• {p["name"]} – {inr(p["price"])}' for p in hits),
                      products=hits, ctx={**ctx, "last_ids": [p["id"] for p in hits]},
                      suggestions=["Compare the first two", "Show cheaper ones", "Add the first one to cart"])

    # ---- product search (with follow-up memory) ----
    q, cat = it["q"], it["category"]
    max_p, min_p = it["max_price"], it["min_price"]
    has_topic = bool(q or cat)
    reset = bool(re.search(r"\b(best|any|anything|all|overall|everything)\b", m))  # "best under 3000" = whole store
    follow_up = refine_cheaper or refine_pricier or refine_more or (bool(max_p or min_p) and not has_topic and not reset)
    if not has_topic and follow_up:
        q, cat = ctx.get("q", ""), ctx.get("category")
    prices = [BY_ID[i]["price"] for i in last]
    if refine_cheaper and prices and max_p is None:
        max_p = min(prices) - 1
    if refine_pricier and prices and min_p is None:
        min_p = max(prices) + 1
    exclude = (set(last) | set(ctx.get("seen", []))) if refine_more else set()
    sort = "price_asc" if (it["cheapest"] or (refine_cheaper and has_topic)) else \
           "price_desc" if (it["premium"] or refine_pricier) else "relevance"
    label = {"price_asc": "lowest-priced options", "price_desc": "premium options"}.get(sort, "best matches")

    if not (q or cat or max_p or min_p):
        return result("What are you shopping for? Tell me a product, a category or your budget.", ctx=ctx,
                      suggestions=["What do you sell?", "Best under ₹3000", "Gym essentials"])

    def run(qq, cc, lo, hi, ss):
        return [h for h in search_products(qq, cc, lo, hi, ss) if h["id"] not in exclude][:4]

    hits, note = run(q, cat, min_p, max_p, sort), ""
    if not hits and follow_up and cat:  # follow-up: widen from the keywords to the whole category
        what = "cheaper" if refine_cheaper else "pricier" if refine_pricier else "more"
        hits = run("", cat, min_p, max_p, sort)
        note = f"Nothing more specific matched, but here are {what if what != 'more' else 'other'} options in {cat}:"
    if not hits and follow_up:
        msg = ("That's already the lowest-priced option I have for this." if refine_cheaper else
               "That's already the highest-priced option I have for this." if refine_pricier else
               "That's everything I have for this search.")
        return result(msg + " Want to see another category?",
                      ctx=ctx, suggestions=["What do you sell?", "Compare the first two"] if len(last) > 1 else ["What do you sell?"])
    if not hits and q and cat:
        hits, note = run(q, None, min_p, max_p, sort), f'Nothing for "{q}" in {cat}, but these are close:'
    if not hits and (min_p or max_p):
        hits = run(q, cat, None, None, "price_asc")[:3]
        note = "Nothing fits that budget exactly. The lowest-priced options are:" if hits else ""
    if not hits:
        return result(f'I couldn\'t find anything for "{q or message}". Try different words, a budget, or ask '
                      f'"What do you sell?"', ctx=ctx, suggestions=["What do you sell?", "Best under ₹3000"])

    new_ctx = ctx_for(hits, q if not note else "", cat, ctx)
    return result(results_reply(hits, q, cat, min_p, max_p, note, label), products=hits, ctx=new_ctx,
                  suggestions=(["Compare the first two"] if len(hits) > 1 else []) +
                              ["Show cheaper ones", "Add the first one to cart", "Show similar items"])


# ---- AI mode (Azure OpenAI tool-calling) -----------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {
        "name": "search_products", "description": "Search the catalog. Use for any product request.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "keywords, e.g. 'running shoes'"},
            "category": {"type": "string", "enum": list(CATEGORY_META)},
            "min_price": {"type": "integer"}, "max_price": {"type": "integer"},
            "sort": {"type": "string", "enum": ["relevance", "price_asc", "price_desc"]}}}}},
    {"type": "function", "function": {
        "name": "compare_products", "description": "Get details of 2-3 products to compare them.",
        "parameters": {"type": "object", "properties": {"product_ids": {"type": "array", "items": {"type": "integer"}}},
                       "required": ["product_ids"]}}},
    {"type": "function", "function": {
        "name": "list_categories", "description": "List store categories with counts and price ranges.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "add_to_cart", "description": "Add a product to the shopper's cart when they ask for it.",
        "parameters": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}}},
]


def _get_client():
    endpoint, key, dep = (os.getenv("AZURE_OPENAI_ENDPOINT"), os.getenv("AZURE_OPENAI_API_KEY"),
                          os.getenv("AZURE_OPENAI_DEPLOYMENT"))
    if not (endpoint and key and dep):
        return None, None
    from openai import AzureOpenAI
    return AzureOpenAI(azure_endpoint=endpoint, api_key=key,
                       api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")), dep


def _slim(p):
    return {k: p[k] for k in ("id", "name", "category", "price", "description")}


def _system_prompt(last: List[int]) -> str:
    shown = "\n".join(f'{n}. {BY_ID[i]["name"]} (id {i}, {inr(BY_ID[i]["price"])})' for n, i in enumerate(last, 1))
    return ("You are ShopAI, a friendly, concise shopping assistant for an Indian online store.\n"
            "Rules:\n- Use the tools to look up products. NEVER invent products, prices or specs.\n"
            "- Prices are in INR (₹). Keep replies under 90 words. Plain text, no markdown.\n"
            "- If the request is vague, ask ONE short clarifying question.\n"
            "- Words like 'the first one', 'it', 'cheaper' refer to the products last shown below.\n"
            "- To put something in the cart, call add_to_cart.\n"
            f"- Store policies: {json.dumps(POLICIES, ensure_ascii=False)}\n"
            f"Products last shown to the shopper:\n{shown or 'none'}")


def llm_chat(message: str, history: List[dict], ctx: Dict):
    client, dep = _get_client()
    if not client:
        return None
    last = [i for i in ctx.get("last_ids", []) if i in BY_ID]
    shown: List[int] = []
    actions: List[dict] = []

    def run_tool(name, args):
        if name == "search_products":
            hits = search_products(args.get("query", ""), args.get("category"), args.get("min_price"),
                                   args.get("max_price"), args.get("sort", "relevance"))[:5]
            shown[:] = [h["id"] for h in hits]
            return [_slim(h) for h in hits]
        if name == "compare_products":
            ids = [i for i in args.get("product_ids", []) if i in BY_ID][:3]
            shown[:] = ids
            return [_slim(BY_ID[i]) for i in ids]
        if name == "list_categories":
            return [{k: c[k] for k in ("name", "description", "count", "min_price", "max_price")} for c in category_details()]
        if name == "add_to_cart":
            pid = int(args.get("product_id", 0))
            if pid in BY_ID:
                actions.append({"type": "add_to_cart", "product_id": pid})
                return {"ok": True, "name": BY_ID[pid]["name"]}
            return {"ok": False}
        return {"error": "unknown tool"}

    msgs = [{"role": "system", "content": _system_prompt(last)}]
    msgs += [{"role": h["role"], "content": str(h.get("content", ""))[:600]}
             for h in history[-8:] if h.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": message})
    try:
        for _ in range(4):
            r = client.chat.completions.create(model=dep, messages=msgs, tools=TOOLS, temperature=0.3, max_tokens=400)
            m = r.choices[0].message
            if not m.tool_calls:
                products = [BY_ID[i] for i in shown[:4]]
                ids = shown or last
                return result((m.content or "").strip() or "Sorry, I couldn't come up with an answer.", products=products,
                              actions=actions, ctx={**ctx, "last_ids": ids},
                              suggestions=(["Compare the first two"] if len(products) > 1 else []) +
                                          ["Show cheaper ones", "Return policy"], mode="llm")
            msgs.append({"role": "assistant", "content": m.content, "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in m.tool_calls]})
            for tc in m.tool_calls:
                try:
                    out = run_tool(tc.function.name, json.loads(tc.function.arguments or "{}"))
                except Exception as e:  # bad tool args must not crash the chat
                    out = {"error": str(e)}
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, ensure_ascii=False)})
    except Exception as e:
        print("LLM error, falling back to smart mode:", e)
    return None


def handle_chat(message: str, history: List[dict], ctx: Dict) -> Dict:
    message = (message or "").strip()[:500]
    ctx = ctx if isinstance(ctx, dict) else {}
    if not message:
        return result("Type a message and I'll help you find something.", ctx=ctx)
    return llm_chat(message, history or [], ctx) or rules(message, ctx)
