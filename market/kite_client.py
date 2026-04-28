"""
Kite REST API client — Nifty NFO variant.
  - NSE:NIFTY 50 spot
  - NFO monthly option LTPs
  - NRML product order placement on NFO
"""
import time
import requests
from utils.logger import get_logger

log = get_logger("kite_client")

_MAX_RETRIES     = 3
_RETRY_DELAYS    = [5, 15, 30]
_RATE_LIMIT_WAIT = 30


class KiteAuthError(Exception):
    pass


class KiteAPIError(Exception):
    pass


class KiteClient:
    def __init__(self, api_key: str, access_token: str,
                 base_url: str = "https://api.kite.trade"):
        self.api_key  = api_key
        self.base_url = base_url.rstrip("/")
        self.session  = requests.Session()
        self._set_token(access_token)

    def _set_token(self, token: str):
        self.access_token = token
        self.session.headers.update({
            "Authorization":  f"token {self.api_key}:{token}",
            "X-Kite-Version": "3",
            "Content-Type":   "application/x-www-form-urlencoded",
        })

    def update_token(self, token: str):
        self._set_token(token)
        log.info("Token updated: %s...", token[:8])

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.session.request(method, url, timeout=15, **kwargs)
            except requests.exceptions.Timeout:
                log.warning("Timeout %s %s (attempt %d)", method, path, attempt + 1)
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                continue
            except requests.exceptions.RequestException as exc:
                log.warning("Network error %s %s: %s", method, path, exc)
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                continue

            if resp.status_code == 429:
                log.warning("Rate limit — waiting %ds", _RATE_LIMIT_WAIT)
                time.sleep(_RATE_LIMIT_WAIT)
                continue

            if resp.status_code in (401, 403):
                raise KiteAuthError(
                    f"Auth error {resp.status_code} — token expired."
                )

            try:
                data = resp.json()
            except Exception:
                raise KiteAPIError(f"Non-JSON response [{resp.status_code}]: {resp.text[:200]}")

            if resp.status_code != 200:
                msg = data.get("message", resp.text[:200])
                raise KiteAPIError(f"API {resp.status_code}: {msg}")

            return data

        raise KiteAPIError(f"All {_MAX_RETRIES} attempts failed for {method} {path}")

    def get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, data: dict = None) -> dict:
        return self._request("POST", path, data=data)

    def put(self, path: str, data: dict = None) -> dict:
        return self._request("PUT", path, data=data)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # ── Market data ───────────────────────────────────────────────────────────

    def ltp(self, *instruments: str) -> dict:
        resp = self.get("/quote/ltp", params={"i": list(instruments)})
        return resp.get("data", {})

    def quote(self, *instruments: str) -> dict:
        resp = self.get("/quote", params={"i": list(instruments)})
        return resp.get("data", {})

    def nifty_spot(self) -> float:
        """NSE Nifty 50 index last price."""
        data = self.ltp("NSE:NIFTY 50")
        return float(data["NSE:NIFTY 50"]["last_price"])

    def nifty_ohlc(self) -> dict:
        """Full OHLC for NSE:NIFTY 50 — includes day open (for gap detection)."""
        data = self.quote("NSE:NIFTY 50")
        q = data["NSE:NIFTY 50"]
        return {
            "open":  float(q["ohlc"]["open"]),
            "high":  float(q["ohlc"]["high"]),
            "low":   float(q["ohlc"]["low"]),
            "close": float(q["ohlc"]["close"]),
            "ltp":   float(q["last_price"]),
        }

    def option_ltps(self, symbols: list) -> dict:
        """LTP for a list of NFO option tradingsymbols. Returns {symbol: last_price}."""
        if not symbols:
            return {}
        instruments = [f"NFO:{s}" for s in symbols]
        data = self.ltp(*instruments)
        return {
            k.replace("NFO:", ""): float(v["last_price"])
            for k, v in data.items()
        }

    def instruments_nfo(self) -> list:
        """Download full NFO instrument list (CSV → list of dicts)."""
        import csv, io
        resp = requests.get(
            f"{self.base_url}/instruments/NFO",
            headers=self.session.headers,
            timeout=30,
        )
        if resp.status_code != 200:
            raise KiteAPIError(f"Instruments download failed: {resp.status_code}")
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        price: float,
        product: str = "NRML",
        order_type: str = "LIMIT",
        tag: str = "ironfly",
    ) -> str:
        if price <= 0:
            order_type = "MARKET"
        data = {
            "exchange":         "NFO",
            "tradingsymbol":    tradingsymbol,
            "transaction_type": transaction_type,
            "quantity":         str(quantity),
            "price":            str(round(price, 2)) if order_type == "LIMIT" else "0",
            "product":          product,
            "order_type":       order_type,
            "validity":         "DAY",
            "tag":              tag,
        }
        log.info("[ORDER] PLACE %s %s qty=%d price=%.2f type=%s",
                 transaction_type, tradingsymbol, quantity, price, order_type)
        resp = self.post("/orders/regular", data=data)
        order_id = resp["data"]["order_id"]
        log.info("[ORDER] Placed order_id=%s", order_id)
        return order_id

    def modify_order(self, order_id: str, price: float) -> str:
        data = {"order_type": "LIMIT", "price": str(round(price, 2)), "validity": "DAY"}
        resp = self.put(f"/orders/regular/{order_id}", data=data)
        log.info("[ORDER] Modified %s → price=%.2f", order_id, price)
        return resp.get("data", {}).get("order_id", order_id)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.delete(f"/orders/regular/{order_id}")
            log.info("[ORDER] Cancelled %s", order_id)
            return True
        except KiteAPIError as e:
            log.warning("[ORDER] Cancel failed %s: %s", order_id, e)
            return False

    def get_order_status(self, order_id: str) -> dict:
        resp = self.get(f"/orders/{order_id}")
        orders = resp.get("data", [])
        return orders[-1] if orders else {}

    def get_positions(self) -> list:
        data = self.get("/portfolio/positions").get("data", {})
        return data.get("net", [])

    def nfo_nifty_positions(self) -> list:
        return [
            p for p in self.get_positions()
            if p.get("exchange") == "NFO"
            and "NIFTY" in p.get("tradingsymbol", "")
            and int(p.get("quantity", 0)) != 0
        ]

    def available_margin(self) -> float:
        try:
            data = self.get("/user/margins")
            equity = data.get("data", {}).get("equity", {})
            return float(equity.get("available", {}).get("live_balance", 0))
        except Exception as e:
            log.warning("Could not fetch margin: %s", e)
            return 0.0

    def basket_margin_rs(self, orders: list) -> float:
        import json as _json
        url = f"{self.base_url}/margins/basket"
        headers = dict(self.session.headers)
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            url,
            data=_json.dumps(orders),
            headers=headers,
            params={"consider_positions": "true", "mode": "compact"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise KiteAPIError(f"basket_margin failed {resp.status_code}: {resp.text[:200]}")
        data = resp.json().get("data", {})
        return float(data.get("final", {}).get("total", 0.0))
