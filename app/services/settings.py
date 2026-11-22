import secrets
from zoneinfo import ZoneInfo

from ..extensions import db
from ..models import Setting

DEFAULTS = {
    # general
    "business_name": "My Shop",
    "currency": "DA",
    "timezone": "Africa/Algiers",
    "default_shipping_fee": "0",
    # assistant persona
    "global_prompt": "You are a sales assistant answering Facebook Marketplace messages. "
                     "Reply in the customer's language (Algerian Darija, Arabic, or French). "
                     "Be warm, concise, and direct. Help the customer place their order.\n"
                     "To send a picture of an item or details, write its keyword like %%keyword%% in your message.",
    "business_description": "We sell quality products on Facebook Marketplace.",
    "reply_style": "Reply in the customer's language (Algerian Darija, Arabic or French). "
                   "Be short, warm and direct. Never invent prices, stock or delivery promises.",
    "delivery_info": "Cash on delivery to all wilayas. Delivery takes 2-5 days.",
    # carrier
    "ecotrack_url": "",
    "ecotrack_token": "",
    "ecotrack_boutique": "",
    "ecotrack_default_weight": "1",
    "ecotrack_ask_collection": "0",
    # gemini
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "gemini_temperature": "0.6",
    "ai_cache_ttl_hours": "168",
    "ai_history_limit": "30",
    "auto_extract_with_ai": "1",
    # tasks
    "phone_scan_minutes": "10",
    "ecotrack_sync_minutes": "60",
    # userscript auth
    "agent_api_key": "",
}
SECRET_KEYS = {"ecotrack_token", "gemini_api_key", "agent_api_key"}


def get(key, default=None):
    row = db.session.get(Setting, key)
    if row is not None and row.value not in (None, ""):
        return row.value
    return DEFAULTS.get(key, "") if default is None else default


def get_int(key, default=0):
    try:
        return int(float(get(key)))
    except (TypeError, ValueError):
        return default


def get_float(key, default=0.0):
    try:
        return float(get(key))
    except (TypeError, ValueError):
        return default


def get_bool(key):
    return str(get(key)).lower() in ("1", "true", "yes", "on")


def set_value(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value=value))
    else:
        row.value = value


def all_settings():
    return {k: get(k) for k in DEFAULTS}


def ensure_agent_key():
    if not get("agent_api_key"):
        set_value("agent_api_key", secrets.token_urlsafe(32))
        db.session.commit()


def tz():
    try:
        return ZoneInfo(get("timezone"))
    except Exception:
        return ZoneInfo("UTC")
