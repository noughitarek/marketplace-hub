"""JSON API used by the Tampermonkey agent. Auth: header  X-API-Key: <Settings → agent key>."""
import base64
import hashlib
import hmac
import os
import re
from datetime import timedelta
from functools import wraps

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db
from ..models import AIResponse, Conversation, Message, Worker, utcnow
from ..services import gemini, settings

bp = Blueprint("api", __name__)


def api_error(msg, code=400, **extra):
    return jsonify({"ok": False, "error": msg, **extra}), code


def require_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        sent = request.headers.get("X-API-Key", "")
        if not sent or not hmac.compare_digest(sent, settings.get("agent_api_key")):
            return api_error("invalid_api_key", 401)
        return fn(*a, **kw)
    return wrapper


def _worker(fb_id):
    return db.session.get(Worker, str(fb_id)) if fb_id else None


def _save_base64_media(data_str, subfolder, prefix, default_ext="bin"):
    if not data_str or not isinstance(data_str, str):
        return None, None
    mime = None
    clean_b64 = data_str.strip()
    if "," in clean_b64 and clean_b64.startswith("data:"):
        header, clean_b64 = clean_b64.split(",", 1)
        m = re.search(r"data:([^;]+);", header)
        if m:
            mime = m.group(1).strip()
    try:
        raw_bytes = base64.b64decode(clean_b64)
    except Exception:
        return None, None

    ext = default_ext
    if mime:
        m_lower = mime.lower()
        if "audio/mp4" in m_lower or "m4a" in m_lower: ext = "mp4"
        elif "audio/ogg" in m_lower or "opus" in m_lower: ext = "ogg"
        elif "audio/webm" in m_lower: ext = "webm"
        elif "audio/mpeg" in m_lower or "mp3" in m_lower: ext = "mp3"
        elif "image/png" in m_lower: ext = "png"
        elif "image/jpeg" in m_lower or "image/jpg" in m_lower: ext = "jpg"
        elif "image/webp" in m_lower: ext = "webp"
        elif "image/gif" in m_lower: ext = "gif"

    fname = f"{prefix}_{hashlib.sha1(raw_bytes).hexdigest()[:16]}.{ext}"
    rel_path = f"uploads/{subfolder}/{fname}"
    full_path = os.path.join(current_app.root_path, "static", "uploads", subfolder, fname)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(raw_bytes)
    return rel_path, mime


@bp.get("/health")
def health():
    return jsonify({"ok": True})


@bp.post("/agent/register")
@require_key
def register():
    """Registration + heartbeat. Creates the worker on first call."""
    d = request.get_json(silent=True) or {}
    fb_id, tab_id = str(d.get("fb_id") or "").strip(), str(d.get("tab_id") or "")
    if not fb_id.isdigit():
        return api_error("fb_id_required")
    w = _worker(fb_id)
    created = w is None
    if created:
        w = Worker(id=fb_id, name=(d.get("name") or f"Account {fb_id}")[:200])
        db.session.add(w)
    now = utcnow()
    w.tab_id, w.tab_seen_at, w.last_seen = tab_id, now, now
    w.script_version = (d.get("version") or "")[:20]
    w.last_page = (d.get("page") or "")[:300]
    if d.get("name") and (created or not w.name or w.name.startswith("Account ")):
        w.name = d["name"][:200]
    db.session.commit()
    return jsonify({"ok": True, "created": created, "config": w.config()})


@bp.get("/agent/config")
@require_key
def config():
    w = _worker(request.args.get("fb_id"))
    return (jsonify({"ok": True, "config": w.config()}) if w else api_error("unknown_worker", 404))


@bp.post("/agent/sync")
@require_key
def sync():
    """Body: {fb_id, conversations:[{thread_id, customer_name, customer_id, url, item_title, item_id,
              messages:[{uid, sender:'customer'|'me', text, audio_data?, audio_mime?, image_data?, image_mime?}]}]}"""
    d = request.get_json(silent=True) or {}
    w = _worker(d.get("fb_id"))
    if not w:
        return api_error("unknown_worker", 404)
    if not w.enabled or not w.collect:
        return jsonify({"ok": True, "stored": False, "results": []})
    results = []
    for c in (d.get("conversations") or [])[:50]:
        tid = str(c.get("thread_id") or "").strip()
        raw_msgs = c.get("messages") or []
        msgs = [m for m in raw_msgs if (m.get("text") or "").strip() or m.get("audio_data") or m.get("image_data") or m.get("audio_url") or m.get("image_url")]
        if not tid or not msgs:
            continue
        conv = Conversation.query.filter_by(worker_id=w.id, thread_id=tid).first()
        if conv is None:
            conv = Conversation(worker_id=w.id, thread_id=tid)
            db.session.add(conv)
            db.session.flush()
        for field, key, cap in (("customer_name", "customer_name", 200), ("customer_fb_id", "customer_id", 64),
                                ("thread_url", "url", 500), ("item_title", "item_title", 300),
                                ("item_id", "item_id", 64)):
            if c.get(key):
                setattr(conv, field, str(c[key])[:cap])
        known = {u for (u,) in db.session.query(Message.uid).filter_by(conversation_id=conv.id)}
        recent_sent = {r.response_text.strip() for r in AIResponse.query.filter(
            AIResponse.conversation_id == conv.id, AIResponse.status.in_(["sent", "drafted"]),
            AIResponse.created_at > utcnow() - timedelta(days=2)) if r.response_text}
        new = 0
        for m in msgs:
            txt = (m.get("text") or "").strip()
            raw_hash_seed = (m.get("sender", "") + txt + str(m.get("audio_data") or "")[:20] + str(m.get("image_data") or "")[:20]).encode()
            uid = str(m.get("uid") or hashlib.sha1(raw_hash_seed).hexdigest())[:80]
            if uid in known:
                continue
            known.add(uid)
            sender = "customer" if m.get("sender") == "customer" else "me"
            if sender == "me" and txt and txt in recent_sent:
                sender = "bot"

            audio_path, audio_mime = None, m.get("audio_mime")
            if m.get("audio_data"):
                audio_path, mime_detected = _save_base64_media(m["audio_data"], "audio", f"aud_{conv.id}_{uid[:8]}", "mp4")
                audio_mime = mime_detected or audio_mime or "audio/mp4"
            elif m.get("audio_url"):
                try:
                    import urllib.request
                    req = urllib.request.Request(m["audio_url"], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        raw_bytes = resp.read()
                        if raw_bytes:
                            b64 = base64.b64encode(raw_bytes).decode("ascii")
                            content_type = resp.headers.get_content_type() or "audio/mp4"
                            audio_path, mime_detected = _save_base64_media(f"data:{content_type};base64,{b64}", "audio", f"aud_{conv.id}_{uid[:8]}", "mp4")
                            audio_mime = mime_detected or content_type
                except Exception:
                    pass

            image_path, image_mime = None, m.get("image_mime")
            if m.get("image_data"):
                image_path, mime_detected = _save_base64_media(m["image_data"], "chat_images", f"img_{conv.id}_{uid[:8]}", "jpg")
                image_mime = mime_detected or image_mime or "image/jpeg"
            elif m.get("image_url"):
                try:
                    import urllib.request
                    req = urllib.request.Request(m["image_url"], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        raw_bytes = resp.read()
                        if raw_bytes:
                            b64 = base64.b64encode(raw_bytes).decode("ascii")
                            content_type = resp.headers.get_content_type() or "image/jpeg"
                            image_path, mime_detected = _save_base64_media(f"data:{content_type};base64,{b64}", "chat_images", f"img_{conv.id}_{uid[:8]}", "jpg")
                            image_mime = mime_detected or content_type
                except Exception:
                    pass

            msg_row = Message(
                conversation_id=conv.id,
                uid=uid,
                sender=sender,
                text=txt,
                audio_path=audio_path,
                audio_mime=audio_mime,
                image_path=image_path,
                image_mime=image_mime
            )
            db.session.add(msg_row)
            new += 1
        last = msgs[-1]
        conv.last_sender = "customer" if last.get("sender") == "customer" else "me"
        if new:
            conv.last_message_at = utcnow()
        db.session.flush()
        needs_reply = False
        if conv.last_sender == "customer":
            last_row = (Message.query.filter_by(conversation_id=conv.id, sender="customer")
                        .order_by(Message.id.desc()).first())
            handled = AIResponse.query.filter(AIResponse.trigger_message_id == last_row.id,
                                              AIResponse.status.in_(["sent", "drafted", "skipped"])).first()
            needs_reply = handled is None
        results.append({"thread_id": tid, "needs_reply": needs_reply, "new_messages": new})
    db.session.commit()
    return jsonify({"ok": True, "stored": True, "results": results})


def _sent_last_hour(worker_id):
    return AIResponse.query.filter(AIResponse.worker_id == worker_id, AIResponse.status.in_(["sent", "drafted"]),
                                   AIResponse.sent_at > utcnow() - timedelta(hours=1)).count()


@bp.post("/answers/generate")
@require_key
def generate():
    """Body: {fb_id, thread_id, force?:bool}  ->  {reply, sequence, response_id, cached, skip}"""
    d = request.get_json(silent=True) or {}
    w = _worker(d.get("fb_id"))
    if not w:
        return api_error("unknown_worker", 404)
    if not w.enabled or (w.reply_mode or "off") == "off":
        return api_error("replies_disabled", 403)
    conv = Conversation.query.filter_by(worker_id=w.id, thread_id=str(d.get("thread_id") or "")).first()
    if not conv:
        return api_error("unknown_conversation", 404)
    if _sent_last_hour(w.id) >= (w.max_replies_per_hour or 20):
        return api_error("hourly_limit_reached", 429)
    try:
        rec = gemini.generate_answer(w, conv, force=bool(d.get("force")))
    except gemini.GeminiError as e:
        return api_error(str(e), 502)
    skip = rec.status == "skipped"
    clean_text, sequence = gemini.parse_response_actions(rec.response_text or "")
    return jsonify({
        "ok": True,
        "response_id": rec.id,
        "cached": rec.cached,
        "skip": skip,
        "model": rec.model,
        "mode": w.reply_mode,
        "reply": "" if skip else clean_text,
        "raw_reply": "" if skip else (rec.response_text or "").strip(),
        "sequence": [] if skip else sequence
    })


@bp.post("/answers/<int:rid>/result")
@require_key
def answer_result(rid):
    """Body: {status:'sent'|'drafted'|'failed'|'skipped', error?}"""
    d = request.get_json(silent=True) or {}
    rec = db.session.get(AIResponse, rid)
    if not rec:
        return api_error("unknown_response", 404)
    status = d.get("status")
    if status not in ("sent", "drafted", "failed", "skipped"):
        return api_error("bad_status")
    rec.status = status
    rec.error = (d.get("error") or None)
    if status in ("sent", "drafted"):
        rec.sent_at = utcnow()
    db.session.commit()
    return jsonify({"ok": True})
