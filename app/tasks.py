"""Scheduled jobs. Every job takes a DB lock so only one process runs it even with several workers."""
import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import or_, update

from .extensions import db
from .models import (ECO_FINAL, Conversation, Message, Order, OrderItem, Product, TaskLock, TaskRun, utcnow)
from .services import gemini, geo, orders as order_svc, ref, settings
from .services.ecotrack import EcotrackClient, EcotrackError
from .services.phones import extract_phones

log = logging.getLogger("tasks")
scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})


def _acquire(name, ttl_minutes):
    now = utcnow()
    if db.session.get(TaskLock, name) is None:
        try:
            db.session.add(TaskLock(name=name, locked_until=now - timedelta(seconds=1)))
            db.session.commit()
        except Exception:
            db.session.rollback()
    res = db.session.execute(
        update(TaskLock).where(TaskLock.name == name, TaskLock.locked_until < now)
        .values(locked_until=now + timedelta(minutes=ttl_minutes)))
    db.session.commit()
    return res.rowcount == 1


def _release(name):
    db.session.execute(update(TaskLock).where(TaskLock.name == name).values(locked_until=utcnow()))
    db.session.commit()


def run_task(app, name, fn, ttl_minutes=30):
    with app.app_context():
        if not _acquire(name, ttl_minutes):
            return None
        run = TaskRun(name=name)
        db.session.add(run)
        db.session.commit()
        try:
            run.message = fn() or ""
            run.ok = True
        except Exception as e:  # noqa
            db.session.rollback()
            log.exception("task %s failed", name)
            run.ok, run.message = False, str(e)[:1000]
        run.finished_at = utcnow()
        db.session.commit()
        _release(name)
        return run.message


# ---- 1. phone numbers -> draft orders -----------------------------------------
def _match_product(name):
    if not name:
        return None
    n = name.strip().lower()
    best = None
    for p in Product.query.filter_by(active=True).all():
        pn = p.name.lower()
        if pn == n:
            return p
        if pn in n or n in pn:
            if best is None or len(pn) > len(best.name):
                best = p
    return best


def _create_draft_order(conv, phone):
    o = Order(status="draft", source="auto", needs_review=True, conversation_id=conv.id,
              customer_name=conv.customer_name or "", phone=phone,
              shipping_fee=settings.get_int("default_shipping_fee", 0))
    note, product, qty = [], _match_product(conv.item_title), 1
    if settings.get_bool("auto_extract_with_ai") and settings.get("gemini_api_key"):
        try:
            data = gemini.extract_order_fields(conv)
            if data.get("customer_name"): o.customer_name = str(data["customer_name"])[:200]
            if data.get("address"): o.address = str(data["address"])[:255]
            if data.get("commune"): o.commune = str(data["commune"])[:120]
            o.wilaya_code = geo.wilaya_code_from(data.get("wilaya")) or o.wilaya_code
            if data.get("notes"): o.notes = str(data["notes"])[:255]
            product = _match_product(data.get("product_name")) or product
            try:
                qty = max(1, int(data.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            note.append("Fields pre-filled by AI from the conversation - please verify.")
        except Exception as e:  # extraction is best effort
            note.append(f"AI extraction failed: {e}")
    if product:
        o.items.append(OrderItem(product_id=product.id, name=product.name, qty=qty,
                                 unit_price=product.price or 0, unit_cost=product.cost or 0))
    else:
        note.append("No product matched automatically - choose the product(s).")
    if o.wilaya_code:
        fee = ref.fees().get(o.wilaya_code)
        if fee:
            o.shipping_fee = fee["desk" if o.stop_desk else "home"]
    o.ai_note = " ".join(note)
    db.session.add(o)
    db.session.commit()
    return o


def scan_phones():
    msgs = (Message.query.filter_by(phone_scanned=False, sender="customer")
            .order_by(Message.id).limit(1000).all())
    created = scanned = 0
    for m in msgs:
        phones = extract_phones(m.text)
        m.phone_scanned, m.phones = True, phones
        scanned += 1
        for p in phones:
            exists = Order.query.filter(or_(Order.phone == p, Order.phone2 == p)).first()
            if exists:
                continue
            _create_draft_order(m.conversation, p)
            created += 1
    db.session.commit()
    return f"scanned {scanned} message(s), created {created} draft order(s)"


# ---- 2. carrier status sync ---------------------------------------------------
def sync_ecotrack():
    client = EcotrackClient.from_settings()
    pending = Order.query.filter(Order.ecotrack_tracking.isnot(None),
                                 or_(Order.ecotrack_status.is_(None), Order.ecotrack_status.notin_(list(ECO_FINAL)))).all()
    if not pending:
        return "no pending orders"
    by_tracking = {o.ecotrack_tracking: o for o in pending}
    data = client.orders_status(list(by_tracking))
    changed = 0
    for tracking, info in data.items():
        o = by_tracking.get(tracking)
        if not o or not isinstance(info, dict):
            continue
        before = o.ecotrack_status
        order_svc.apply_ecotrack_status(o, info.get("status"), info.get("activity"))
        changed += before != o.ecotrack_status
    # fees: the list endpoint carries tarif_prestation / tarif_retour
    oldest = min(o.created_at for o in pending).date().isoformat()
    page = 1
    while page <= 10:
        try:
            res = client.list_orders(start_date=oldest, page=page)
        except EcotrackError:
            break
        for row in res.get("data") or []:
            o = by_tracking.get(row.get("tracking"))
            if o:
                o.delivery_cost = int(float(row.get("tarif_prestation") or 0))
                o.return_cost = int(float(row.get("tarif_retour") or 0))
        if page >= (res.get("last_page") or 1):
            break
        page += 1
    db.session.commit()
    return f"checked {len(pending)} order(s), {changed} status change(s)"


# ---- scheduler wiring ---------------------------------------------------------
def schedule(app):
    with app.app_context():
        scan_min = max(1, settings.get_int("phone_scan_minutes", 10))
        sync_min = max(5, settings.get_int("ecotrack_sync_minutes", 60))
    scheduler.add_job(run_task, "interval", minutes=scan_min, id="scan_phones", replace_existing=True,
                      args=[app, "scan_phones", scan_phones, 20])
    scheduler.add_job(run_task, "interval", minutes=sync_min, id="sync_ecotrack", replace_existing=True,
                      args=[app, "sync_ecotrack", sync_ecotrack, 30])


def start(app):
    if scheduler.running:
        return
    schedule(app)
    scheduler.start()
