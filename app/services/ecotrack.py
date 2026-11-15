"""Client for the EcoTrack public API (see ECOTRACK_API_postman_collection.json).

Auth: Bearer token header (+ api_token query param, which two endpoints require).
Limits: 50 requests / minute (HTTP 429) - the client waits and retries.
Orders are editable/deletable only while status == prete_a_expedier.
"""
import time

import requests

from . import settings


class EcotrackError(Exception):
    def __init__(self, message, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class EcotrackClient:
    def __init__(self, base_url, token):
        if not base_url or not token:
            raise EcotrackError("EcoTrack URL / token are not configured (Settings → Carrier).")
        self.base = base_url.strip().rstrip("/")
        self.token = token.strip()

    @classmethod
    def from_settings(cls):
        return cls(settings.get("ecotrack_url"), settings.get("ecotrack_token"))

    # -- transport -----------------------------------------------------
    def _request(self, method, path, params=None):
        query = {"api_token": self.token}
        query.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = f"{self.base}/api/v1/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        resp = None
        for _ in range(3):
            try:
                resp = requests.request(method, url, params=query, headers=headers, timeout=40)
            except requests.RequestException as e:
                raise EcotrackError(f"Cannot reach EcoTrack: {e}")
            if resp.status_code == 429:
                time.sleep(20)
                continue
            break
        if resp.status_code == 429:
            raise EcotrackError("EcoTrack rate limit (50 req/min) reached, try again shortly.", http_status=429)
        try:
            data = resp.json()
        except ValueError:
            raise EcotrackError(f"Unexpected EcoTrack response (HTTP {resp.status_code}).", http_status=resp.status_code)
        if resp.status_code == 422 and isinstance(data, dict):
            errs = data.get("errors") or {}
            first = next(iter(errs.values()), [data.get("message", "Invalid data")])
            raise EcotrackError(" | ".join(f"{k}: {v[0]}" for k, v in errs.items()) or str(first), http_status=422)
        if resp.status_code in (401, 403):
            raise EcotrackError("EcoTrack refused the token (invalid or API access disabled).", http_status=resp.status_code)
        if resp.status_code >= 400:
            raise EcotrackError(f"EcoTrack HTTP {resp.status_code}: {str(data)[:200]}", http_status=resp.status_code)
        if isinstance(data, dict) and data.get("success") is False:
            raise EcotrackError(data.get("message") or "EcoTrack error", code=data.get("error"))
        return data

    # -- authorisation -------------------------------------------------
    def validate_token(self):
        return self._request("GET", "validate/token")

    # -- orders --------------------------------------------------------
    def create_order(self, payload):
        data = self._request("POST", "create/order", payload)
        if not data.get("tracking"):
            raise EcotrackError(f"EcoTrack did not return a tracking number: {data}")
        return data["tracking"]

    def update_order(self, tracking, payload):
        return self._request("POST", "update/order", {"tracking": tracking, **payload})

    def delete_order(self, tracking):
        return self._request("DELETE", "delete/order", {"tracking": tracking})

    def ship_order(self, tracking, ask_collection=0):
        return self._request("POST", "valid/order", {"tracking": tracking, "ask_collection": int(ask_collection)})

    def list_orders(self, start_date=None, end_date=None, page=1, tracking=None):
        return self._request("GET", "get/orders", {"page": page, "start_date": start_date,
                                                    "end_date": end_date, "tracking": tracking})

    def get_order(self, tracking):
        data = self.list_orders(tracking=tracking)
        for row in (data.get("data") or []) if isinstance(data, dict) else []:
            if row.get("tracking") == tracking:
                return row
        return None

    def orders_status(self, trackings):
        """Up to 100 trackings per call -> {tracking: {status, activity, ...}}"""
        out = {}
        for i in range(0, len(trackings), 100):
            chunk = trackings[i:i + 100]
            data = self._request("GET", "get/orders/status", {"trackings": ",".join(chunk), "status": "all"})
            out.update((data or {}).get("data") or {})
        return out

    # -- reference data ---------------------------------------------------
    def wilayas(self):
        return self._request("GET", "get/wilayas")

    def communes(self, wilaya_id=None):
        return self._request("GET", "get/communes", {"wilaya_id": wilaya_id})

    def fees(self):
        return self._request("GET", "get/fees")
