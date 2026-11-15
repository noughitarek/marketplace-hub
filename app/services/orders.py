"""Order lifecycle: stock bookkeeping, EcoTrack push/update/delete, status mapping."""
import hashlib
import json

from ..extensions import db
from ..models import (ECO_CANCELLED, ECO_DELIVERED, ECO_MODIFIABLE, ECO_RETURN_RECEIVED, ECO_RETURNED,
                      STOCK_HOLDING, Order, Product, StockMovement, utcnow)
from . import settings
from .ecotrack import EcotrackClient, EcotrackError


# ---- stock -------------------------------------------------------------------
def sync_order_stock(order):
    """Make stock movements for this order match what its status requires (idempotent)."""
    holds = order.status in STOCK_HOLDING or (
        order.status == "returned" and order.ecotrack_status not in ECO_RETURN_RECEIVED)
    desired = {}
    if holds:
        for it in order.items:
            if it.product_id:
                desired[it.product_id] = desired.get(it.product_id, 0) + (it.qty or 0)
    current = {}
    if order.id:
        for m in StockMovement.query.filter_by(ref_type="order", ref_id=order.id):
            current[m.product_id] = current.get(m.product_id, 0) - m.qty   # movement is negative
    for pid in set(desired) | set(current):
        diff = desired.get(pid, 0) - current.get(pid, 0)   # extra units that must leave stock
        if diff:
            p = db.session.get(Product, pid)
            if p is None:
                continue
            p.stock_qty = (p.stock_qty or 0) - diff
            db.session.add(StockMovement(product_id=pid, qty=-diff, reason="sale", ref_type="order",
                                         ref_id=order.id, note=f"Order #{order.id} ({order.status})"))


# ---- payload mapping -----------------------------------------------------
def products_label(order):
    return ", ".join(f"{i.name} x{i.qty}" if i.qty != 1 else i.name for i in order.items)[:255]


def create_payload(o):
    weight = o.weight or settings.get_float("ecotrack_default_weight", 1)
    return {
        "reference": f"ORD-{o.id}", "nom_client": o.customer_name, "telephone": o.phone,
        "telephone_2": o.phone2, "adresse": o.address, "code_postal": o.postal_code,
        "commune": o.commune, "code_wilaya": o.wilaya_code, "montant": o.total,
        "remarque": (o.notes or "")[:255], "produit": products_label(o), "stock": 0, "type": 1,
        "stop_desk": 1 if o.stop_desk else 0, "weight": weight, "fragile": 1 if o.fragile else 0,
        "gps_link": o.gps_link, "boutique": settings.get("ecotrack_boutique"),
    }


def update_payload(o):
    p = create_payload(o)
    return {
        "reference": p["reference"], "client": p["nom_client"], "tel": p["telephone"], "tel2": p["telephone_2"],
        "adresse": p["adresse"], "code_postal": p["code_postal"], "commune": p["commune"],
        "wilaya": p["code_wilaya"], "montant": p["montant"], "remarque": p["remarque"],
        "product": p["produit"], "boutique": p["boutique"], "type": 1, "stop_desk": p["stop_desk"],
        "fragile": p["fragile"], "gps_link": p["gps_link"],
    }


def payload_hash(o):
    return hashlib.sha1(json.dumps(create_payload(o), sort_keys=True, default=str).encode()).hexdigest()


def missing_fields(o):
    miss = []
    if not (o.customer_name or "").strip(): miss.append("customer name")
    if not o.phone: miss.append("phone")
    if not (o.address or "").strip(): miss.append("address")
    if not (o.commune or "").strip(): miss.append("commune")
    if not o.wilaya_code: miss.append("wilaya")
    if not o.items: miss.append("at least one product")
    return miss


# ---- status mapping --------------------------------------------------------
def apply_ecotrack_status(o, status, activity=None):
    if not status:
        return
    o.ecotrack_status = status
    o.ecotrack_checked_at = utcnow()
    if activity is not None:
        o.ecotrack_activity = activity
    if status in ECO_DELIVERED:
        o.status = "delivered"
        o.delivered_at = o.delivered_at or utcnow()
        o.closed_at = o.closed_at or utcnow()
    elif status in ECO_RETURNED:
        o.status = "returned"
        o.closed_at = o.closed_at or utcnow()
    elif status in ECO_CANCELLED:
        o.status = "cancelled"
        o.closed_at = o.closed_at or utcnow()
    elif status in ECO_MODIFIABLE:
        o.status = "confirmed"
    else:
        o.status = "in_delivery"
    sync_order_stock(o)


# ---- EcoTrack operations -----------------------------------------------------
def confirm_order(o):
    """Create the order at EcoTrack and mark it confirmed. Raises EcotrackError / ValueError."""
    miss = missing_fields(o)
    if miss:
        raise ValueError("Missing: " + ", ".join(miss))
    if not o.ecotrack_tracking:
        db.session.flush()  # make sure o.id exists for the reference
        tracking = EcotrackClient.from_settings().create_order(create_payload(o))
        o.ecotrack_tracking = tracking
        o.ecotrack_status = "prete_a_expedier"
        o.ecotrack_payload_hash = payload_hash(o)
        o.ecotrack_error = None
    o.status = "confirmed"
    o.confirmed_at = o.confirmed_at or utcnow()
    o.needs_review = False
    sync_order_stock(o)


def push_update_if_needed(o):
    """Edit the order at EcoTrack when its data changed and the carrier still allows it.
    Returns one of: 'none', 'unchanged', 'updated', 'locked'."""
    if not o.ecotrack_tracking or o.status in ("draft", "cancelled"):
        return "none"
    new_hash = payload_hash(o)
    if new_hash == o.ecotrack_payload_hash:
        return "unchanged"
    client = EcotrackClient.from_settings()
    live = client.get_order(o.ecotrack_tracking)
    if live:
        apply_ecotrack_status(o, live.get("status"))
    if o.ecotrack_status not in ECO_MODIFIABLE:
        o.ecotrack_error = (f"Not sent to EcoTrack: order already '{o.ecotrack_status}' "
                            "and can no longer be modified there.")
        return "locked"
    try:
        client.update_order(o.ecotrack_tracking, update_payload(o))
    except EcotrackError as e:
        if e.code == 10001:
            o.ecotrack_error = "Not sent to EcoTrack: order is no longer modifiable."
            return "locked"
        raise
    o.ecotrack_payload_hash = new_hash
    o.ecotrack_error = None
    return "updated"


def delete_remote(o):
    """Delete at EcoTrack. Raises EcotrackError if the carrier refuses."""
    if o.ecotrack_tracking:
        EcotrackClient.from_settings().delete_order(o.ecotrack_tracking)


def ship(o):
    EcotrackClient.from_settings().ship_order(o.ecotrack_tracking, settings.get_int("ecotrack_ask_collection", 0))
    live = EcotrackClient.from_settings().get_order(o.ecotrack_tracking)
    if live:
        apply_ecotrack_status(o, live.get("status"))
