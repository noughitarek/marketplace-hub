"""Gemini REST client + answer generation with a DB cache."""
import hashlib
import json
import re
import time
from datetime import timedelta

import requests

from ..extensions import db
from ..models import AICache, AIResponse, Conversation, Product, utcnow
from . import settings

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
NO_REPLY = "[NO_REPLY]"


class GeminiError(Exception):
    pass


def _call(model, system, contents, temperature, json_mode=False, max_tokens=2048):
    key = settings.get("gemini_api_key")
    if not key:
        raise GeminiError("Gemini API key is not configured (Settings → Assistant).")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    try:
        r = requests.post(ENDPOINT.format(model=model), json=body, timeout=60,
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise GeminiError(f"Cannot reach Gemini: {e}")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:200])
        except ValueError:
            msg = r.text[:200]
        raise GeminiError(f"Gemini HTTP {r.status_code}: {msg}")
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        raise GeminiError(f"Gemini returned no candidates: {data.get('promptFeedback', '')}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    usage = data.get("usageMetadata") or {}
    return text, usage.get("promptTokenCount"), usage.get("candidatesTokenCount")


def _to_contents(messages):
    """[(sender, text)] -> Gemini contents: merged same-role turns, starting and ending with 'user'."""
    contents = []
    for sender, text in messages:
        role = "user" if sender == "customer" else "model"
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n" + text
        else:
            contents.append({"role": role, "parts": [{"text": text}]})
    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "(start of conversation)"}]})
    return contents


def catalog_text():
    cur = settings.get("currency")
    lines = []
    for p in Product.query.filter_by(active=True).order_by(Product.name).all():
        stock = "in stock" if (p.stock_qty or 0) > 0 else "OUT OF STOCK"
        desc = f" - {p.description.strip()[:300]}" if p.description else ""
        lines.append(f"* {p.name}: {p.price} {cur} ({stock}){desc}")
    return "\n".join(lines) or "(no products configured)"


def build_system_prompt(worker, conv):
    parts = [
        f"You are the sales assistant of \"{settings.get('business_name')}\", answering Facebook Marketplace "
        f"messages on behalf of the seller.",
        settings.get("business_description"),
        "STYLE: " + settings.get("reply_style"),
        "DELIVERY: " + settings.get("delivery_info"),
        "CATALOGUE (only sell what is listed; prices are final unless stated):\n" + catalog_text(),
        "TO CLOSE A SALE collect: full name, phone number, wilaya, commune and address. "
        "Ask only for what is still missing. Confirm the product, quantity and total price.",
        f"Output ONLY the message to send to the customer, as plain text, no markdown. "
        f"If no answer is needed (e.g. the customer only says thanks/ok), output exactly {NO_REPLY}.",
    ]
    if conv.item_title:
        parts.append(f"The customer is messaging about this listing: \"{conv.item_title}\".")
    if worker.custom_prompt:
        parts.append("ADDITIONAL SELLER INSTRUCTIONS:\n" + worker.custom_prompt)
    return "\n\n".join(p for p in parts if p)


def generate_answer(worker, conv, force=False):
    """Generate (or fetch from cache) a reply for the conversation's current state."""
    limit = settings.get_int("ai_history_limit", 30)
    history = [(m.sender, m.text) for m in conv.messages if (m.text or "").strip()][-limit:]
    if not history or history[-1][0] != "customer":
        raise GeminiError("The last message is not from the customer; nothing to answer.")
    last_msg = next(m for m in reversed(conv.messages) if m.sender == "customer")

    model = worker.model_override or settings.get("gemini_model")
    temperature = worker.temperature if worker.temperature is not None else settings.get_float("gemini_temperature", 0.6)
    system = build_system_prompt(worker, conv)
    contents = _to_contents(history)
    request_blob = json.dumps({"model": model, "t": temperature, "system": system, "contents": contents},
                              ensure_ascii=False, sort_keys=True)
    cache_key = hashlib.sha256(request_blob.encode()).hexdigest()

    rec = AIResponse(conversation_id=conv.id, worker_id=worker.id, trigger_message_id=last_msg.id,
                     cache_key=cache_key, model=model, request_json=request_blob)
    ttl = timedelta(hours=settings.get_int("ai_cache_ttl_hours", 168))
    hit = None if force else db.session.get(AICache, cache_key)
    if hit and utcnow() - hit.created_at < ttl:
        hit.hits = (hit.hits or 0) + 1
        hit.last_hit_at = utcnow()
        rec.response_text, rec.cached = hit.response_text, True
    else:
        t0 = time.time()
        try:
            text, pt, ct = _call(model, system, contents, temperature)
        except GeminiError as e:
            rec.status, rec.error = "failed", str(e)
            db.session.add(rec)
            db.session.commit()
            raise
        rec.latency_ms = int((time.time() - t0) * 1000)
        rec.prompt_tokens, rec.completion_tokens = pt, ct
        rec.response_text = text
        if not text:
            rec.status, rec.error = "failed", "Gemini returned an empty answer"
            db.session.add(rec)
            db.session.commit()
            raise GeminiError("Gemini returned an empty answer")
        entry = hit or AICache(key=cache_key, hits=0)
        entry.response_text, entry.model, entry.created_at = text, model, utcnow()
        db.session.merge(entry)
    if NO_REPLY in (rec.response_text or ""):
        rec.status = "skipped"
    db.session.add(rec)
    db.session.commit()
    return rec


def extract_order_fields(conv):
    """Ask Gemini to pull delivery details out of a conversation. Returns dict (possibly empty)."""
    transcript = "\n".join(f"{'CUSTOMER' if m.sender == 'customer' else 'SELLER'}: {m.text}"
                           for m in conv.messages[-60:] if m.text)
    system = ("Extract order details from this sales conversation. Reply with JSON only using keys: "
              "customer_name, address, commune, wilaya (name or number 1-58), product_name, quantity (int), "
              "notes. Use null for anything not stated. Never guess.")
    contents = [{"role": "user", "parts": [{"text": f"Listing: {conv.item_title or '-'}\n\n{transcript}"}]}]
    text, _, _ = _call(settings.get("gemini_model"), system, contents, 0.0, json_mode=True)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text)
    return data if isinstance(data, dict) else {}
