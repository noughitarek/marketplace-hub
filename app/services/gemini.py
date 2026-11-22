"""Gemini REST client + answer generation with a DB cache."""
import base64
import hashlib
import json
import os
import re
import time
from datetime import timedelta

import requests
from flask import current_app

from ..extensions import db
from ..models import AICache, AIResponse, Conversation, ImageAsset, Product, utcnow
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
    """List of Message objects -> Gemini contents with multimodal audio/image support."""
    contents = []
    static_folder = os.path.join(current_app.root_path, "static")

    for m in messages:
        role = "user" if m.sender == "customer" else "model"
        parts = []

        # Customer sent image
        if getattr(m, "image_path", None):
            full_path = os.path.join(static_folder, m.image_path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, "rb") as f:
                        img_b64 = base64.b64encode(f.read()).decode("ascii")
                    mime = getattr(m, "image_mime", None) or "image/jpeg"
                    parts.append({"inlineData": {"mimeType": mime, "data": img_b64}})
                except Exception:
                    pass

        # Customer sent audio / voice note
        if getattr(m, "audio_path", None):
            full_path = os.path.join(static_folder, m.audio_path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, "rb") as f:
                        aud_b64 = base64.b64encode(f.read()).decode("ascii")
                    mime = getattr(m, "audio_mime", None) or "audio/mp4"
                    parts.append({"inlineData": {"mimeType": mime, "data": aud_b64}})
                except Exception:
                    pass

        # Text
        text = (m.text or "").strip()
        if text:
            parts.append({"text": text})
        elif not parts:
            if getattr(m, "audio_path", None):
                parts.append({"text": "(Customer voice message)"})
            elif getattr(m, "image_path", None):
                parts.append({"text": "(Customer image)"})

        if not parts:
            continue

        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "(start of conversation)"}]})
    return contents


def build_system_prompt(worker, conv=None):
    """Returns 1 full prompt set by user: worker account prompt overrides global prompt. No extra/hidden instructions."""
    account_prompt = (worker.custom_prompt or "").strip() if worker else ""
    if account_prompt:
        return account_prompt
    global_prompt = (settings.get("global_prompt") or "").strip()
    if global_prompt:
        return global_prompt
    return "You are a helpful sales assistant answering Facebook Marketplace messages."


def parse_response_actions(response_text):
    """
    Scans response_text for image keywords (e.g. %%image1%%).
    Returns (clean_text, sequence) where sequence is ordered list of text and image actions:
    e.g. [{"type": "text", "text": "Hi how are you"}, {"type": "image", "keyword": "image1", ...}]
    """
    if not response_text:
        return "", []

    assets = ImageAsset.query.all()
    if not assets:
        return response_text.strip(), [{"type": "text", "text": response_text.strip()}] if response_text.strip() else []

    matches = []
    for img in assets:
        clean_kw = img.clean_keyword
        raw_kw = (img.keyword or "").strip()
        patterns = [
            rf"%%{re.escape(clean_kw)}%%",
            rf"{{{{{re.escape(clean_kw)}}}}}",
        ]
        if raw_kw and raw_kw != clean_kw:
            patterns.append(re.escape(raw_kw))
        combined = "|".join(patterns)
        for m in re.finditer(combined, response_text, flags=re.IGNORECASE):
            matches.append((m.start(), m.end(), img))

    if not matches:
        return response_text.strip(), [{"type": "text", "text": response_text.strip()}] if response_text.strip() else []

    # Sort matches by position and resolve any overlaps
    matches.sort(key=lambda x: x[0])
    non_overlapping = []
    last_end = 0
    for start, end, img in matches:
        if start >= last_end:
            non_overlapping.append((start, end, img))
            last_end = end

    sequence = []
    clean_parts = []
    last_idx = 0

    for start, end, img in non_overlapping:
        text_before = response_text[last_idx:start]
        if text_before.strip():
            sequence.append({"type": "text", "text": text_before.strip()})
            clean_parts.append(text_before.strip())

        sequence.append({
            "type": "image",
            "keyword": img.clean_keyword,
            "image_id": img.id,
            "filename": img.filename,
            "url": f"/static/uploads/images/{img.filename}",
        })
        last_idx = end

    text_after = response_text[last_idx:]
    if text_after.strip():
        sequence.append({"type": "text", "text": text_after.strip()})
        clean_parts.append(text_after.strip())

    clean_text = " ".join(clean_parts).strip()
    return clean_text, sequence


def generate_answer(worker, conv, force=False):
    """Generate (or fetch from cache) a reply for the conversation's current state."""
    limit = settings.get_int("ai_history_limit", 30)
    # Include messages that have text, audio, or image
    valid_msgs = [m for m in conv.messages if (m.text or "").strip() or m.audio_path or m.image_path][-limit:]
    if not valid_msgs or valid_msgs[-1].sender != "customer":
        raise GeminiError("The last message is not from the customer; nothing to answer.")
    last_msg = next(m for m in reversed(valid_msgs) if m.sender == "customer")

    model = worker.model_override or settings.get("gemini_model")
    temperature = worker.temperature if worker.temperature is not None else settings.get_float("gemini_temperature", 0.6)
    system = build_system_prompt(worker, conv)
    contents = _to_contents(valid_msgs)
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
