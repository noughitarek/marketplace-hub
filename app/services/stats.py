from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from ..extensions import db
from ..models import (AIResponse, Message, Order, OrderItem, Product, Purchase, PurchaseItem)
from . import settings


def local_today():
    return datetime.now(settings.tz()).date()


def bounds(d1, d2):
    """Local dates (inclusive) -> naive UTC datetimes [start, end)."""
    z = settings.tz()
    s = datetime(d1.year, d1.month, d1.day, tzinfo=z).astimezone(timezone.utc).replace(tzinfo=None)
    e = datetime(d2.year, d2.month, d2.day, tzinfo=z).astimezone(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    return s, e


def to_local_date(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(settings.tz()).date()


def dashboard(d1, d2):
    s, e = bounds(d1, d2)
    rng = lambda col: (col >= s, col < e)

    created = Order.query.filter(*rng(Order.created_at)).all()
    confirmed = Order.query.filter(*rng(Order.confirmed_at), Order.status != "cancelled").all()
    delivered = Order.query.filter(*rng(Order.delivered_at)).all()
    returned = Order.query.filter(*rng(Order.closed_at), Order.status == "returned").all()
    in_transit = Order.query.filter(Order.status.in_(["confirmed", "in_delivery"])).all()
    drafts = Order.query.filter_by(status="draft").count()

    sales = sum(o.total for o in confirmed)
    revenue = sum(o.total for o in delivered)
    cogs = sum(o.cogs for o in delivered)
    delivery_costs = sum(o.delivery_cost or 0 for o in delivered)
    return_losses = sum(o.return_cost or 0 for o in returned)
    gross_profit = revenue - cogs - delivery_costs
    net_profit = gross_profit - return_losses

    purchases = Purchase.query.filter(Purchase.purchased_at >= d1, Purchase.purchased_at <= d2).all()
    purchases_total = sum(p.total for p in purchases)

    # daily series (local days)
    days = {}
    d = d1
    while d <= d2 and (d - d1).days < 400:
        days[d] = {"sales": 0, "profit": 0, "orders": 0}
        d += timedelta(days=1)
    for o in confirmed:
        k = to_local_date(o.confirmed_at)
        if k in days:
            days[k]["sales"] += o.total
            days[k]["orders"] += 1
    for o in delivered:
        k = to_local_date(o.delivered_at)
        if k in days:
            days[k]["profit"] += o.profit
    for o in returned:
        k = to_local_date(o.closed_at)
        if k in days:
            days[k]["profit"] -= o.return_cost or 0

    top = defaultdict(lambda: {"qty": 0, "revenue": 0})
    for o in confirmed:
        for it in o.items:
            top[it.name]["qty"] += it.qty or 0
            top[it.name]["revenue"] += (it.qty or 0) * (it.unit_price or 0)
    top_products = sorted(({"name": k, **v} for k, v in top.items()), key=lambda x: -x["revenue"])[:6]

    by_status = defaultdict(int)
    for o in created:
        by_status[o.status] += 1

    stock_value = db.session.query(func.coalesce(func.sum(Product.stock_qty * Product.cost), 0)).filter(
        Product.stock_qty > 0).scalar()
    low = Product.query.filter(Product.active.is_(True), Product.stock_qty <= Product.low_stock_threshold) \
        .order_by(Product.stock_qty).limit(8).all()

    return {
        "orders_created": len(created), "auto_orders": sum(1 for o in created if o.source == "auto"),
        "by_status": dict(by_status), "drafts_waiting": drafts,
        "confirmed_count": len(confirmed), "sales": sales,
        "delivered_count": len(delivered), "revenue": revenue, "cogs": cogs,
        "delivery_costs": delivery_costs, "gross_profit": gross_profit,
        "returned_count": len(returned), "return_losses": return_losses, "net_profit": net_profit,
        "in_transit_count": len(in_transit), "in_transit_value": sum(o.total for o in in_transit),
        "in_transit_profit": sum(o.profit for o in in_transit),
        "purchases_total": purchases_total, "purchases_count": len(purchases),
        "stock_value": int(stock_value or 0), "low_stock": low,
        "msgs_in": Message.query.filter(*rng(Message.created_at), Message.sender == "customer").count(),
        "ai_total": AIResponse.query.filter(*rng(AIResponse.created_at)).count(),
        "ai_cached": AIResponse.query.filter(*rng(AIResponse.created_at), AIResponse.cached.is_(True)).count(),
        "ai_sent": AIResponse.query.filter(*rng(AIResponse.created_at), AIResponse.status.in_(["sent", "drafted"])).count(),
        "daily": [{"date": k.isoformat(), **v} for k, v in days.items()],
        "chart_max": max([1] + [max(v["sales"], v["profit"]) for v in days.values()]),
        "top_products": top_products,
    }
