"""Cached reference data from EcoTrack (communes, fees) with graceful fallbacks."""
from datetime import timedelta

from ..extensions import db
from ..models import RefCache, utcnow
from .ecotrack import EcotrackClient, EcotrackError
from .geo import WILAYA_BY_CODE


def _cached(key, loader, max_age_hours=24):
    row = db.session.get(RefCache, key)
    if row and row.data is not None and utcnow() - row.updated_at < timedelta(hours=max_age_hours):
        return row.data
    try:
        data = loader()
    except EcotrackError:
        return row.data if row else None
    if row is None:
        row = RefCache(key=key)
        db.session.add(row)
    row.data, row.updated_at = data, utcnow()
    db.session.commit()
    return data


def communes(wilaya_id):
    data = _cached(f"communes:{wilaya_id}", lambda: EcotrackClient.from_settings().communes(wilaya_id)) or []
    rows = list(data.values()) if isinstance(data, dict) else data
    return [{"nom": r.get("nom"), "code_postal": r.get("code_postal"),
             "has_stop_desk": r.get("has_stop_desk", 0)} for r in rows if r.get("is_active", 1)]


def fees():
    """{wilaya_id: {"home": int, "desk": int}}"""
    data = _cached("fees", lambda: EcotrackClient.from_settings().fees()) or {}
    out = {}
    for r in data.get("livraison", []):
        try:
            out[int(r["wilaya_id"])] = {"home": int(float(r.get("tarif") or 0)),
                                        "desk": int(float(r.get("tarif_stopdesk") or 0))}
        except (KeyError, ValueError):
            continue
    return out


def wilayas():
    def load():
        return EcotrackClient.from_settings().wilayas()
    data = _cached("wilayas", load, 24 * 7)
    if data:
        return [{"id": int(w["wilaya_id"]), "name": w["wilaya_name"]} for w in data]
    return [{"id": c, "name": n} for c, n in WILAYA_BY_CODE.items()]
