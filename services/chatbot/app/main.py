"""chatbot-service: the chat brain (rule-based "smart" mode + optional Azure OpenAI tool-calling mode)."""
from typing import List

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from .chatbot import handle_chat, result

app = FastAPI(title="chatbot-service")


class ChatIn(BaseModel):
    message: str
    history: List[dict] = []
    context: dict = {}


@app.get("/health")
def health():
    return {"status": "ok", "service": "chatbot"}


@app.post("/chat")
def chat(body: ChatIn):
    try:
        return handle_chat(body.message, body.history, body.context)
    except (httpx.HTTPError, KeyError) as e:  # catalog/search down -> friendly answer instead of a 500
        print("[chatbot] upstream error:", repr(e))
        return result("I can't reach the product service right now. Please try again in a moment.",
                      ctx=body.context if isinstance(body.context, dict) else {})
