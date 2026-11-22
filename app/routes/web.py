import hmac
import os
import secrets
from datetime import date, timedelta
from functools import wraps

from flask import (Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for)
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import (ORDER_STATUSES, AIResponse, Conversation, ImageAsset, Order, OrderItem, Product, Purchase, PurchaseItem,
                      StockMovement, TaskRun, Worker, utcnow)
from ..services import gemini, geo, orders as order_svc, ref, settings, stats
from ..services.ecotrack import EcotrackClient, EcotrackError
from ..services.phones import normalize

bp = Blueprint("web", __name__)


# ---- auth ----------------------------------------------------------------------
@bp.before_request
def require_login():
    if request.endpoint in ("web.login", "static") or request.endpoint is None:
        return None
    if not session.get("auth"):
        return redirect(url_for("web.login", next=request.path))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), current_app.config["ADMIN_PASSWORD"]):
            session.clear()
            session["auth"] = True
            session["_csrf"] = secrets.token_hex(16)
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(request.script_root + nxt)
            return redirect(url_for("web.dashboard"))
        flash("Wrong password.", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


# ---- helpers -------------------------------------------------------------------
def to_int(v, default=0):
    try:
        return int(float(str(v).replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        return default


def parse_date(s, default):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return default


# ---- dashboard -----------------------------------------------------------------
@bp.get("/")
def dashboard():
    today = stats.local_today()
    d1 = parse_date(request.args.get("from"), today)
    d2 = max(parse_date(request.args.get("to"), today), d1)
    data = stats.dashboard(d1, d2)
    return render_template("dashboard.html", d=data, d1=d1, d2=d2, today=today,
                           workers=Worker.query.order_by(Worker.last_seen.desc()).all())


# ---- products ------------------------------------------------------------------
@bp.get("/products")
def products():
    q = request.args.get("q", "").strip()
    query = Product.query
    if q:
        query = query.filter(or_(Product.name.ilike(f"%{q}%"), Product.sku.ilike(f"%{q}%")))
    return render_template("products.html", products=query.order_by(Product.active.desc(), Product.name).all(), q=q)


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:pid>/edit", methods=["GET", "POST"])
def product_form(pid=None):
    p = db.get_or_404(Product, pid) if pid else Product(active=True)
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            flash("Product name is required.", "error")
        else:
            p.name, p.sku = f["name"].strip(), f.get("sku", "").strip() or None
            p.description = f.get("description", "").strip()
            p.price, p.cost = to_int(f.get("price")), to_int(f.get("cost"))
            p.low_stock_threshold = to_int(f.get("low_stock_threshold"), 3)
            p.weight = float(f["weight"]) if f.get("weight") else None
            p.fragile, p.active = bool(f.get("fragile")), bool(f.get("active"))
            p.image_url = f.get("image_url", "").strip() or None
            new = p.id is None
            if new:
                db.session.add(p)
                db.session.flush()
                opening = to_int(f.get("opening_stock"))
                if opening:
                    p.stock_qty = opening
                    db.session.add(StockMovement(product_id=p.id, qty=opening, reason="adjustment", note="Opening stock"))
            db.session.commit()
            flash("Product saved.", "ok")
            return redirect(url_for("web.products"))
    return render_template("product_form.html", p=p)


@bp.post("/products/<int:pid>/delete")
def product_delete(pid):
    p = db.get_or_404(Product, pid)
    used = (OrderItem.query.filter_by(product_id=pid).first() or PurchaseItem.query.filter_by(product_id=pid).first())
    if used:
        p.active = False
        flash("Product is used in orders/purchases, so it was archived instead of deleted.", "warn")
    else:
        StockMovement.query.filter_by(product_id=pid).delete()
        db.session.delete(p)
        flash("Product deleted.", "ok")
    db.session.commit()
    return redirect(url_for("web.products"))


# ---- orders --------------------------------------------------------------------
@bp.get("/orders")
def orders():
    status, q = request.args.get("status", ""), request.args.get("q", "").strip()
    query = Order.query
    if status:
        query = query.filter_by(status=status)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Order.customer_name.ilike(like), Order.phone.ilike(like),
                                 Order.ecotrack_tracking.ilike(like), Order.commune.ilike(like)))
    rows = query.order_by(Order.needs_review.desc(), Order.id.desc()).limit(300).all()
    counts = dict(db.session.query(Order.status, db.func.count()).group_by(Order.status).all())
    return render_template("orders.html", orders=rows, status=status, q=q, counts=counts, statuses=ORDER_STATUSES)


def _apply_order_form(o, f):
    o.customer_name = f.get("customer_name", "").strip()
    o.phone = normalize(f.get("phone", "")) or f.get("phone", "").strip() or None
    o.phone2 = normalize(f.get("phone2", "")) or f.get("phone2", "").strip() or None
    o.address, o.commune = f.get("address", "").strip()[:255], f.get("commune", "").strip()[:120]
    o.wilaya_code = to_int(f.get("wilaya_code"), 0) or None
    o.postal_code = f.get("postal_code", "").strip() or None
    o.stop_desk, o.fragile = bool(f.get("stop_desk")), bool(f.get("fragile"))
    o.weight = float(f["weight"]) if f.get("weight") else None
    o.gps_link = f.get("gps_link", "").strip() or None
    o.notes = f.get("notes", "").strip()[:255]
    o.shipping_fee, o.discount = to_int(f.get("shipping_fee")), to_int(f.get("discount"))
    old_cost = {i.product_id: i.unit_cost for i in o.items if i.product_id}
    pids, names = f.getlist("item_product_id"), f.getlist("item_name")
    qtys, prices = f.getlist("item_qty"), f.getlist("item_price")
    o.items.clear()
    for pid, name, qty, price in zip(pids, names, qtys, prices):
        pid = to_int(pid, 0) or None
        prod = db.session.get(Product, pid) if pid else None
        name = (name or (prod.name if prod else "")).strip()
        if not name or to_int(qty) <= 0:
            continue
        cost = old_cost.get(pid, prod.cost if prod else 0)
        o.items.append(OrderItem(product_id=pid, name=name, qty=to_int(qty, 1), unit_price=to_int(price), unit_cost=cost or 0))


@bp.route("/orders/new", methods=["GET", "POST"])
@bp.route("/orders/<int:oid>", methods=["GET", "POST"])
def order_form(oid=None):
    o = db.get_or_404(Order, oid) if oid else Order(status="draft", source="manual",
                                                    shipping_fee=settings.get_int("default_shipping_fee"))
    if request.method == "POST":
        action = request.form.get("action", "save")
        _apply_order_form(o, request.form)
        o.needs_review = False
        if o.id is None:
            db.session.add(o)
            db.session.flush()
        try:
            if action == "confirm" and o.status == "draft":
                order_svc.confirm_order(o)
                flash(f"Order confirmed - EcoTrack tracking {o.ecotrack_tracking}.", "ok")
            elif o.ecotrack_tracking:
                res = order_svc.push_update_if_needed(o)
                order_svc.sync_order_stock(o)
                msgs = {"updated": ("Saved and updated at EcoTrack.", "ok"),
                        "unchanged": ("Saved (nothing changed for the carrier).", "ok"),
                        "locked": ("Saved here, but EcoTrack no longer allows edits: " + (o.ecotrack_error or ""), "warn")}
                flash(*msgs.get(res, ("Saved.", "ok")))
            else:
                flash("Draft saved.", "ok")
            db.session.commit()
            return redirect(url_for("web.order_form", oid=o.id))
        except ValueError as e:
            db.session.commit()
            flash(f"Saved as draft, cannot confirm yet. {e}", "warn")
        except EcotrackError as e:
            db.session.commit()
            flash(f"Saved locally. EcoTrack said: {e}", "error")
        return redirect(url_for("web.order_form", oid=o.id))
    was_review = bool(o.id and o.needs_review)
    if was_review:  # opening an auto-created order counts as having seen it
        o.needs_review = False
        db.session.commit()
    return render_template("order_form.html", o=o, was_review=was_review, products=Product.query.filter_by(active=True).order_by(Product.name).all(),
                           wilayas=ref.wilayas() if settings.get("ecotrack_token") else
                           [{"id": c, "name": n} for c, n in geo.WILAYA_BY_CODE.items()],
                           eco_ready=bool(settings.get("ecotrack_token") and settings.get("ecotrack_url")))


@bp.post("/orders/<int:oid>/delete")
def order_delete(oid):
    o = db.get_or_404(Order, oid)
    force = request.form.get("force") == "1"
    if o.ecotrack_tracking and not force:
        try:
            order_svc.delete_remote(o)
        except EcotrackError as e:
            flash(f"EcoTrack refused to delete {o.ecotrack_tracking}: {e}. You can still remove it only here.", "error")
            return redirect(url_for("web.order_form", oid=oid, delete_failed=1))
    o.status = "cancelled"
    order_svc.sync_order_stock(o)  # gives any reserved stock back before the order disappears
    StockMovement.query.filter_by(ref_type="order", ref_id=o.id).update({"ref_id": None})
    db.session.delete(o)
    db.session.commit()
    flash("Order deleted.", "ok")
    return redirect(url_for("web.orders"))


@bp.post("/orders/<int:oid>/ship")
def order_ship(oid):
    o = db.get_or_404(Order, oid)
    try:
        order_svc.ship(o)
        db.session.commit()
        flash("Order dispatched to the carrier. It can no longer be edited at EcoTrack.", "ok")
    except EcotrackError as e:
        flash(f"EcoTrack: {e}", "error")
    return redirect(url_for("web.order_form", oid=oid))


@bp.post("/orders/<int:oid>/refresh")
def order_refresh(oid):
    o = db.get_or_404(Order, oid)
    try:
        info = EcotrackClient.from_settings().orders_status([o.ecotrack_tracking]).get(o.ecotrack_tracking)
        if info:
            order_svc.apply_ecotrack_status(o, info.get("status"), info.get("activity"))
            db.session.commit()
            flash(f"EcoTrack status: {o.ecotrack_status}", "ok")
    except EcotrackError as e:
        flash(f"EcoTrack: {e}", "error")
    return redirect(url_for("web.order_form", oid=oid))


@bp.post("/orders/<int:oid>/cancel")
def order_cancel(oid):
    o = db.get_or_404(Order, oid)
    o.status, o.closed_at = "cancelled", utcnow()
    order_svc.sync_order_stock(o)
    db.session.commit()
    flash("Order marked as cancelled (stock restored).", "ok")
    return redirect(url_for("web.order_form", oid=oid))


# ---- stock ---------------------------------------------------------------------
@bp.get("/stock")
def stock():
    pid = request.args.get("product", type=int)
    mq = StockMovement.query
    if pid:
        mq = mq.filter_by(product_id=pid)
    prods = Product.query.order_by(Product.active.desc(), Product.name).all()
    return render_template("stock.html", products=prods, movements=mq.order_by(StockMovement.id.desc()).limit(100).all(),
                           pid=pid, value=sum(max(p.stock_qty or 0, 0) * (p.cost or 0) for p in prods))


@bp.post("/stock/adjust")
def stock_adjust():
    p = db.get_or_404(Product, to_int(request.form.get("product_id")))
    delta = to_int(request.form.get("delta"))
    if delta:
        p.stock_qty = (p.stock_qty or 0) + delta
        db.session.add(StockMovement(product_id=p.id, qty=delta, reason="adjustment",
                                     note=request.form.get("note", "").strip()[:255] or "Manual adjustment"))
        db.session.commit()
        flash(f"{p.name}: stock is now {p.stock_qty}.", "ok")
    return redirect(url_for("web.stock"))


# ---- purchases -----------------------------------------------------------------
def _apply_purchase_stock(purchase, sign):
    for it in purchase.items:
        it.product.stock_qty = (it.product.stock_qty or 0) + sign * it.qty
        db.session.add(StockMovement(product_id=it.product_id, qty=sign * it.qty, reason="purchase",
                                     ref_type="purchase", ref_id=purchase.id,
                                     note=f"Purchase #{purchase.id}" + (" (reversed)" if sign < 0 else "")))


@bp.get("/purchases")
def purchases():
    rows = Purchase.query.order_by(Purchase.purchased_at.desc(), Purchase.id.desc()).limit(200).all()
    return render_template("purchases.html", purchases=rows)


@bp.route("/purchases/new", methods=["GET", "POST"])
@bp.route("/purchases/<int:pid>/edit", methods=["GET", "POST"])
def purchase_form(pid=None):
    pu = db.get_or_404(Purchase, pid) if pid else Purchase()
    if request.method == "POST":
        f = request.form
        if pu.id:
            _apply_purchase_stock(pu, -1)
            pu.items.clear()
            db.session.flush()
        pu.supplier = f.get("supplier", "").strip()
        pu.purchased_at = parse_date(f.get("purchased_at"), stats.local_today())
        pu.shipping_cost, pu.notes = to_int(f.get("shipping_cost")), f.get("notes", "").strip()
        new = pu.id is None
        for prod_id, qty, cost in zip(f.getlist("item_product_id"), f.getlist("item_qty"), f.getlist("item_cost")):
            prod = db.session.get(Product, to_int(prod_id))
            if prod and to_int(qty) > 0:
                pu.items.append(PurchaseItem(product_id=prod.id, qty=to_int(qty), unit_cost=to_int(cost)))
        if not pu.items:
            flash("Add at least one product line.", "error")
        else:
            db.session.add(pu)
            db.session.flush()
            if new:  # weighted-average cost only when the purchase is first recorded
                for it in pu.items:
                    p, have = it.product, max(it.product.stock_qty or 0, 0)
                    p.cost = round((have * (p.cost or 0) + it.qty * it.unit_cost) / (have + it.qty)) if have + it.qty else it.unit_cost
            _apply_purchase_stock(pu, +1)
            db.session.commit()
            flash("Purchase saved, stock updated.", "ok")
            return redirect(url_for("web.purchases"))
        db.session.rollback()
    return render_template("purchase_form.html", pu=pu, products=Product.query.filter_by(active=True).order_by(Product.name).all(),
                           today=stats.local_today())


@bp.post("/purchases/<int:pid>/delete")
def purchase_delete(pid):
    pu = db.get_or_404(Purchase, pid)
    _apply_purchase_stock(pu, -1)
    StockMovement.query.filter_by(ref_type="purchase", ref_id=pid).update({"ref_id": None})
    db.session.delete(pu)
    db.session.commit()
    flash("Purchase deleted and its stock removed.", "ok")
    return redirect(url_for("web.purchases"))


# ---- message workers & conversations -----------------------------------------------
@bp.get("/workers")
def workers():
    ws = Worker.query.order_by(Worker.last_seen.desc().nullslast()).all()
    counts = dict(db.session.query(Conversation.worker_id, db.func.count()).group_by(Conversation.worker_id).all())
    return render_template("workers.html", workers=ws, counts=counts, ai_ready=bool(settings.get("gemini_api_key")))


@bp.route("/workers/<wid>", methods=["GET", "POST"])
def worker_form(wid):
    w = db.get_or_404(Worker, wid)
    if request.method == "POST":
        f = request.form
        w.name = f.get("name", "").strip()[:200] or w.name
        w.enabled, w.collect = bool(f.get("enabled")), bool(f.get("collect"))
        w.reply_mode = f.get("reply_mode") if f.get("reply_mode") in ("off", "draft", "auto") else "off"
        w.custom_prompt = f.get("custom_prompt", "").strip()
        w.model_override = f.get("model_override", "").strip()
        w.temperature = float(f["temperature"]) if f.get("temperature") else None
        w.poll_interval = max(20, to_int(f.get("poll_interval"), 60))
        w.delay_min, w.delay_max = max(0, to_int(f.get("delay_min"), 8)), max(0, to_int(f.get("delay_max"), 25))
        w.max_replies_per_hour = max(1, to_int(f.get("max_replies_per_hour"), 20))
        db.session.commit()
        flash("Worker saved. The script picks changes up on its next heartbeat.", "ok")
        return redirect(url_for("web.workers"))
    return render_template("worker_form.html", w=w)


@bp.post("/workers/<wid>/delete")
def worker_delete(wid):
    w = db.get_or_404(Worker, wid)
    if Conversation.query.filter_by(worker_id=wid).first():
        flash("This worker has stored conversations - disable it instead of deleting.", "warn")
    else:
        db.session.delete(w)
        db.session.commit()
        flash("Worker removed (it re-registers if the script is still running).", "ok")
    return redirect(url_for("web.workers"))


@bp.get("/conversations")
def conversations():
    wid = request.args.get("worker")
    q = Conversation.query
    if wid:
        q = q.filter_by(worker_id=wid)
    rows = q.order_by(Conversation.last_message_at.desc().nullslast()).limit(150).all()
    order_ids = {c: o for c, o in db.session.query(Order.conversation_id, Order.id).filter(Order.conversation_id.isnot(None))}
    return render_template("conversations.html", convs=rows, order_ids=order_ids, wid=wid)


@bp.get("/conversations/<int:cid>")
def conversation_view(cid):
    c = db.get_or_404(Conversation, cid)
    ai = AIResponse.query.filter_by(conversation_id=cid).order_by(AIResponse.id.desc()).limit(20).all()
    orders_ = Order.query.filter_by(conversation_id=cid).all()
    return render_template("conversation.html", c=c, ai=ai, orders=orders_)


@bp.post("/conversations/<int:cid>/test-reply")
def conversation_test(cid):
    c = db.get_or_404(Conversation, cid)
    try:
        rec = gemini.generate_answer(c.worker, c, force=request.form.get("force") == "1")
        rec.status = "skipped" if rec.status == "skipped" else "generated"
        db.session.commit()
        flash("Generated (from cache)." if rec.cached else "Generated with Gemini.", "ok")
    except gemini.GeminiError as e:
        flash(str(e), "error")
    return redirect(url_for("web.conversation_view", cid=cid))


@bp.post("/conversations/<int:cid>/create-order")
def conversation_order(cid):
    from ..tasks import _create_draft_order
    c = db.get_or_404(Conversation, cid)
    o = _create_draft_order(c, normalize(request.form.get("phone", "")) or None)
    return redirect(url_for("web.order_form", oid=o.id))


# ---- images --------------------------------------------------------------------
@bp.get("/images")
def images():
    q = request.args.get("q", "").strip()
    query = ImageAsset.query
    if q:
        query = query.filter(ImageAsset.keyword.ilike(f"%{q}%"))
    rows = query.order_by(ImageAsset.id.desc()).all()
    return render_template("images.html", images=rows, q=q)


@bp.route("/images/new", methods=["GET", "POST"])
def image_new():
    if request.method == "POST":
        keyword = request.form.get("keyword", "").strip()
        f = request.files.get("file")
        if not keyword:
            flash("Keyword is required.", "error")
        elif not f or not f.filename:
            flash("Image file is required.", "error")
        else:
            clean_kw = keyword.strip()
            existing = ImageAsset.query.filter(ImageAsset.keyword.ilike(clean_kw)).first()
            if existing:
                flash(f"An image with keyword '{clean_kw}' already exists.", "error")
            else:
                ext = (os.path.splitext(f.filename)[1] or ".jpg").lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    ext = ".jpg"
                fname = f"{secrets.token_hex(8)}_{secure_filename(f.filename)}"
                save_dir = os.path.join(current_app.root_path, "static", "uploads", "images")
                os.makedirs(save_dir, exist_ok=True)
                f.save(os.path.join(save_dir, fname))
                img = ImageAsset(keyword=clean_kw, filename=fname, original_filename=f.filename[:255])
                db.session.add(img)
                db.session.commit()
                flash(f"Image for keyword '{clean_kw}' uploaded successfully.", "ok")
                return redirect(url_for("web.images"))
    return render_template("image_form.html", img=None)


@bp.route("/images/<int:iid>/edit", methods=["GET", "POST"])
def image_edit(iid):
    img = db.get_or_404(ImageAsset, iid)
    if request.method == "POST":
        keyword = request.form.get("keyword", "").strip()
        f = request.files.get("file")
        if not keyword:
            flash("Keyword is required.", "error")
        else:
            existing = ImageAsset.query.filter(ImageAsset.keyword.ilike(keyword), ImageAsset.id != iid).first()
            if existing:
                flash(f"An image with keyword '{keyword}' already exists.", "error")
            else:
                img.keyword = keyword
                if f and f.filename:
                    ext = (os.path.splitext(f.filename)[1] or ".jpg").lower()
                    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                        ext = ".jpg"
                    fname = f"{secrets.token_hex(8)}_{secure_filename(f.filename)}"
                    save_dir = os.path.join(current_app.root_path, "static", "uploads", "images")
                    os.makedirs(save_dir, exist_ok=True)
                    f.save(os.path.join(save_dir, fname))
                    old_path = os.path.join(save_dir, img.filename)
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except Exception:
                            pass
                    img.filename = fname
                    img.original_filename = f.filename[:255]
                db.session.commit()
                flash("Image updated successfully.", "ok")
                return redirect(url_for("web.images"))
    return render_template("image_form.html", img=img)


@bp.post("/images/<int:iid>/delete")
def image_delete(iid):
    img = db.get_or_404(ImageAsset, iid)
    save_dir = os.path.join(current_app.root_path, "static", "uploads", "images")
    old_path = os.path.join(save_dir, img.filename)
    if os.path.exists(old_path):
        try:
            os.remove(old_path)
        except Exception:
            pass
    db.session.delete(img)
    db.session.commit()
    flash(f"Image '{img.keyword}' deleted.", "ok")
    return redirect(url_for("web.images"))


# ---- settings ------------------------------------------------------------------
@bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        f, action = request.form, request.form.get("action", "save")
        if action == "regen_key":
            settings.set_value("agent_api_key", secrets.token_urlsafe(32))
            db.session.commit()
            flash("New script key generated - update it in the Tampermonkey script.", "warn")
            return redirect(url_for("web.settings_page"))
        for key in settings.DEFAULTS:
            if key == "agent_api_key" or key not in f:
                continue
            val = f[key].strip()
            if key in settings.SECRET_KEYS and not val:
                continue  # blank = keep current secret
            settings.set_value(key, val)
        db.session.commit()
        if current_app.config["SCHEDULER_ENABLED"]:
            from .. import tasks
            tasks.schedule(current_app._get_current_object())
        if action == "test_ecotrack":
            try:
                res = EcotrackClient.from_settings().validate_token()
                flash(f"EcoTrack: {res.get('message')}", "ok" if res.get("success") else "error")
            except EcotrackError as e:
                flash(f"EcoTrack: {e}", "error")
        else:
            flash("Settings saved.", "ok")
        return redirect(url_for("web.settings_page"))
    s = settings.all_settings()
    masked = {k: bool(s[k]) for k in settings.SECRET_KEYS}
    for k in settings.SECRET_KEYS:
        if k != "agent_api_key":
            s[k] = ""
    runs = TaskRun.query.order_by(TaskRun.id.desc()).limit(12).all()
    return render_template("settings.html", s=s, masked=masked, runs=runs, base_url=request.url_root.rstrip("/"))


@bp.post("/tasks/<name>/run")
def run_task_now(name):
    from .. import tasks
    fn = {"scan_phones": tasks.scan_phones, "sync_ecotrack": tasks.sync_ecotrack}.get(name)
    if not fn:
        flash("Unknown task.", "error")
    else:
        msg = tasks.run_task(current_app._get_current_object(), name, fn)
        flash(f"{name}: {msg}" if msg is not None else f"{name} is already running.", "ok")
    return redirect(request.referrer or url_for("web.settings_page"))


# ---- small JSON helpers for forms ----------------------------------------------------
@bp.get("/ref/communes")
def ref_communes():
    try:
        return jsonify(ref.communes(request.args.get("wilaya", type=int)))
    except Exception:
        return jsonify([])


@bp.get("/ref/fees")
def ref_fees():
    try:
        return jsonify({str(k): v for k, v in ref.fees().items()})
    except Exception:
        return jsonify({})
