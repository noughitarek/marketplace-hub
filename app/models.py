from datetime import datetime, timezone

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- order status model --------------------------------------------------
ORDER_STATUSES = ["draft", "confirmed", "in_delivery", "delivered", "returned", "cancelled"]
STOCK_HOLDING = {"confirmed", "in_delivery", "delivered"}  # statuses whose items are out of stock

ECO_DELIVERED = {"livre_non_encaisse", "encaisse_non_paye", "paiements_prets", "paye_et_archive"}
ECO_RETURNED = {"retour_chez_livreur", "retour_transit_entrepot", "retour_en_traitement",
                "retour_recu", "retour_archive"}
ECO_RETURN_RECEIVED = {"retour_recu", "retour_archive"}
ECO_CANCELLED = {"annule"}
ECO_MODIFIABLE = {"prete_a_expedier"}
ECO_FINAL = ECO_DELIVERED | ECO_CANCELLED | {"retour_recu", "retour_archive", "paye_et_archive"}


class Setting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, default="")


class TaskLock(db.Model):
    name = db.Column(db.String(80), primary_key=True)
    locked_until = db.Column(db.DateTime, default=utcnow)


class TaskRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), index=True)
    started_at = db.Column(db.DateTime, default=utcnow)
    finished_at = db.Column(db.DateTime)
    ok = db.Column(db.Boolean, default=True)
    message = db.Column(db.Text, default="")


class RefCache(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    data = db.Column(db.JSON)
    updated_at = db.Column(db.DateTime, default=utcnow)


# ---- catalogue & stock ---------------------------------------------------
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    sku = db.Column(db.String(80), index=True)
    description = db.Column(db.Text, default="")
    price = db.Column(db.Integer, default=0)          # selling price (DA)
    cost = db.Column(db.Integer, default=0)           # average purchase cost (DA)
    stock_qty = db.Column(db.Integer, default=0)
    low_stock_threshold = db.Column(db.Integer, default=3)
    weight = db.Column(db.Float)
    fragile = db.Column(db.Boolean, default=False)
    image_url = db.Column(db.String(500))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def low(self):
        return (self.stock_qty or 0) <= (self.low_stock_threshold or 0)


class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), index=True, nullable=False)
    qty = db.Column(db.Integer, nullable=False)       # + in / - out
    reason = db.Column(db.String(30))                 # purchase | sale | adjustment
    ref_type = db.Column(db.String(30))               # order | purchase
    ref_id = db.Column(db.Integer, index=True)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow)
    product = db.relationship("Product")


class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier = db.Column(db.String(200))
    purchased_at = db.Column(db.Date, default=lambda: utcnow().date(), index=True)
    shipping_cost = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow)
    items = db.relationship("PurchaseItem", cascade="all, delete-orphan", backref="purchase")

    @property
    def total(self):
        return sum(i.qty * i.unit_cost for i in self.items) + (self.shipping_cost or 0)


class PurchaseItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("purchase.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    qty = db.Column(db.Integer, default=1)
    unit_cost = db.Column(db.Integer, default=0)
    product = db.relationship("Product")


# ---- messaging -----------------------------------------------------------
class Worker(db.Model):
    """One Facebook account running the userscript. id = Facebook user id."""
    id = db.Column(db.String(64), primary_key=True)
    name = db.Column(db.String(200))
    enabled = db.Column(db.Boolean, default=False)            # master switch
    collect = db.Column(db.Boolean, default=True)             # store conversations
    reply_mode = db.Column(db.String(10), default="off")      # off | draft | auto
    custom_prompt = db.Column(db.Text, default="")
    model_override = db.Column(db.String(80), default="")
    temperature = db.Column(db.Float)
    poll_interval = db.Column(db.Integer, default=60)         # seconds
    delay_min = db.Column(db.Integer, default=8)              # seconds before answering
    delay_max = db.Column(db.Integer, default=25)
    max_replies_per_hour = db.Column(db.Integer, default=20)
    registered_at = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime)
    tab_id = db.Column(db.String(64))
    tab_seen_at = db.Column(db.DateTime)
    script_version = db.Column(db.String(20))
    last_page = db.Column(db.String(300))

    def config(self):
        return {
            "enabled": bool(self.enabled), "collect": bool(self.collect),
            "reply_mode": self.reply_mode or "off", "poll_interval": self.poll_interval or 60,
            "delay_min": self.delay_min or 0, "delay_max": max(self.delay_max or 0, self.delay_min or 0),
            "max_replies_per_hour": self.max_replies_per_hour or 20,
        }

    @property
    def online(self):
        return bool(self.last_seen and (utcnow() - self.last_seen).total_seconds() < 120)


class Conversation(db.Model):
    __table_args__ = (db.UniqueConstraint("worker_id", "thread_id"),)
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.String(64), db.ForeignKey("worker.id"), index=True, nullable=False)
    thread_id = db.Column(db.String(128), nullable=False)
    customer_name = db.Column(db.String(200))
    customer_fb_id = db.Column(db.String(64))
    thread_url = db.Column(db.String(500))
    item_title = db.Column(db.String(300))
    item_id = db.Column(db.String(64))
    last_sender = db.Column(db.String(10))
    last_message_at = db.Column(db.DateTime, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    worker = db.relationship("Worker")
    messages = db.relationship("Message", backref="conversation", order_by="Message.id",
                               cascade="all, delete-orphan")


class Message(db.Model):
    __table_args__ = (db.UniqueConstraint("conversation_id", "uid"),)
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), index=True, nullable=False)
    uid = db.Column(db.String(80), nullable=False)
    sender = db.Column(db.String(10), nullable=False)         # customer | me | bot
    text = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    phone_scanned = db.Column(db.Boolean, default=False, index=True)
    phones = db.Column(db.JSON)


class AICache(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    response_text = db.Column(db.Text)
    model = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=utcnow)
    last_hit_at = db.Column(db.DateTime)
    hits = db.Column(db.Integer, default=0)


class AIResponse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), index=True)
    worker_id = db.Column(db.String(64), index=True)
    trigger_message_id = db.Column(db.Integer, index=True)
    cache_key = db.Column(db.String(64))
    model = db.Column(db.String(80))
    request_json = db.Column(db.Text)
    response_text = db.Column(db.Text)
    cached = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(12), default="generated")   # generated|drafted|sent|skipped|failed
    error = db.Column(db.Text)
    latency_ms = db.Column(db.Integer)
    prompt_tokens = db.Column(db.Integer)
    completion_tokens = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    sent_at = db.Column(db.DateTime)
    conversation = db.relationship("Conversation")


# ---- orders ----------------------------------------------------------------
class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(200), default="")
    phone = db.Column(db.String(20), index=True)
    phone2 = db.Column(db.String(20), index=True)
    address = db.Column(db.String(255), default="")
    commune = db.Column(db.String(120), default="")
    wilaya_code = db.Column(db.Integer)
    postal_code = db.Column(db.String(10))
    stop_desk = db.Column(db.Boolean, default=False)
    fragile = db.Column(db.Boolean, default=False)
    weight = db.Column(db.Float)
    gps_link = db.Column(db.String(500))
    notes = db.Column(db.String(255), default="")
    shipping_fee = db.Column(db.Integer, default=0)   # charged to the customer
    discount = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="draft", index=True)
    source = db.Column(db.String(10), default="manual")  # manual | auto
    needs_review = db.Column(db.Boolean, default=False)
    ai_note = db.Column(db.Text)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), index=True)

    ecotrack_tracking = db.Column(db.String(40), index=True)
    ecotrack_status = db.Column(db.String(40))
    ecotrack_payload_hash = db.Column(db.String(40))
    ecotrack_error = db.Column(db.Text)
    ecotrack_activity = db.Column(db.JSON)
    ecotrack_checked_at = db.Column(db.DateTime)
    delivery_cost = db.Column(db.Integer, default=0)  # carrier fee we pay
    return_cost = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    confirmed_at = db.Column(db.DateTime, index=True)
    delivered_at = db.Column(db.DateTime, index=True)
    closed_at = db.Column(db.DateTime, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    items = db.relationship("OrderItem", cascade="all, delete-orphan", backref="order",
                            order_by="OrderItem.id")
    conversation = db.relationship("Conversation")

    @property
    def items_total(self):
        return sum((i.qty or 0) * (i.unit_price or 0) for i in self.items)

    @property
    def total(self):
        return max(0, self.items_total - (self.discount or 0) + (self.shipping_fee or 0))

    @property
    def cogs(self):
        return sum((i.qty or 0) * (i.unit_cost or 0) for i in self.items)

    @property
    def profit(self):
        return self.total - self.cogs - (self.delivery_cost or 0)

    @property
    def remote_modifiable(self):
        return bool(self.ecotrack_tracking) and self.ecotrack_status in (None, "", *ECO_MODIFIABLE)


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    name = db.Column(db.String(200), default="")
    qty = db.Column(db.Integer, default=1)
    unit_price = db.Column(db.Integer, default=0)
    unit_cost = db.Column(db.Integer, default=0)
    product = db.relationship("Product")
