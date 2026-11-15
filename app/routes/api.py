"""JSON API used by the Tampermonkey agent. Auth: header  X-API-Key: <Settings → agent key>."""
import hashlib
import hmac
from datetime import timedelta
from functools import wraps

from flask import Blueprint, jsonify, request

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
              messages:[{uid, sender:'customer'|'me', text}]}]}"""
    d = request.get_json(silent=True) or {}
    w = _worker(d.get("fb_id"))
    if not w:
        return api_error("unknown_worker", 404)
    if not w.enabled or not w.collect:
        return jsonify({"ok": True, "stored": False, "results": []})
    results = []
    for c in (d.get("conversations") or [])[:50]:
        tid = str(c.get("thread_id") or "").strip()
        msgs = [m for m in (c.get("messages") or []) if (m.get("text") or "").strip()]
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
            uid = str(m.get("uid") or hashlib.sha1((m.get("sender", "") + m["text"]).encode()).hexdigest())[:80]
            if uid in known:
                continue
            known.add(uid)
            sender = "customer" if m.get("sender") == "customer" else "me"
            if sender == "me" and m["text"].strip() in recent_sent:
                sender = "bot"
            db.session.add(Message(conversation_id=conv.id, uid=uid, sender=sender, text=m["text"]))
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
    """Body: {fb_id, thread_id, force?:bool}  ->  {reply, response_id, cached, skip}"""
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
    return jsonify({"ok": True, "response_id": rec.id, "cached": rec.cached, "skip": skip, "model": rec.model,
                    "mode": w.reply_mode, "reply": "" if skip else rec.response_text.strip()})


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
