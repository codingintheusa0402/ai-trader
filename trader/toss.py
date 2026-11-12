"""토스증권 Open API broker adapter (https://openapi.tossinvest.com). TESTED with real orders.

This is the reference adapter: its response shapes are the ones the engine expects. Every other broker adapter
(trader/brokers/) converts its own API into these shapes, see trader/brokers/base.py.

Toss has NO mock server — every order here is REAL MONEY.
Orders are dry-run unless BOTH:
  - place_order(..., dry_run=False)
  - env REAL_TRADING_CONFIRMED=yes
and each order is capped by MAX_ORDER_KRW / MAX_ORDER_USD.
"""
import json
import os
import threading
import time
import uuid
import requests
from dotenv import load_dotenv
from . import config as C

load_dotenv(C.ROOT / ".env")
BASE = "https://openapi.tossinvest.com"
TOKEN_CACHE = C.STATE_DIR / ".toss_token.json"
# Why Toss refuses this machine's login (e.g. "IP address not allowed" — the Open API has an IP allow list,
# so a backup engine on another network can't trade). Cleared by the next successful token.
AUTH_BLOCK = {"t": 0.0, "msg": "", "ip": ""}


def auth_block():
    return dict(AUTH_BLOCK) if AUTH_BLOCK["msg"] and time.time() - AUTH_BLOCK["t"] < 900 else None


def _public_ip():
    try:
        return requests.get("https://ifconfig.me", timeout=5).text.strip()
    except Exception:
        return ""


from .brokers.base import Broker, BrokerUnavailable

TossUnavailable = BrokerUnavailable   # kept for older imports


class TossClient(Broker):
    name = "toss"
    label = "토스증권"
    tested = True
    def __init__(self):
        self.cid = os.environ.get("TOSS_CLIENT_ID", "").strip()
        self.secret = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
        if not (self.cid and self.secret):
            raise RuntimeError("Toss keys not set (TOSS_CLIENT_ID / TOSS_CLIENT_SECRET in .env)")
        self.account = os.environ.get("TOSS_ACCOUNT", "")

    # ── auth (form-encoded; JSON body fails) ─────────────────
    _tok = {"t": None}
    _tok_lock = threading.Lock()

    def token(self) -> str:
        """Kept in memory (shared by all threads); the file is only a cache across restarts."""
        with self._tok_lock:
            t = self._tok["t"]
            if not t and TOKEN_CACHE.exists():
                try:
                    t = json.loads(TOKEN_CACHE.read_text())
                except (ValueError, OSError):
                    t = None
            if t and t.get("expires_at", 0) - time.time() > 300:
                self._tok["t"] = t
                return t["access_token"]
            r = requests.post(f"{BASE}/oauth2/token", timeout=10, data={
                "grant_type": "client_credentials",
                "client_id": self.cid, "client_secret": self.secret})
            if r.status_code == 403:
                try:
                    desc = r.json().get("error_description") or r.text[:120]
                except ValueError:
                    desc = r.text[:120]
                AUTH_BLOCK.update(t=time.time(), msg=desc, ip=AUTH_BLOCK["ip"] or _public_ip())
            r.raise_for_status()
            AUTH_BLOCK.update(t=0.0, msg="")
            t = r.json()
            t["expires_at"] = time.time() + int(t.get("expires_in", 86400))
            self._tok["t"] = t
            TOKEN_CACHE.parent.mkdir(exist_ok=True)
            tmp = TOKEN_CACHE.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # private from the first byte
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(t))
            os.replace(tmp, TOKEN_CACHE)
            return t["access_token"]

    def drop_token(self):
        with self._tok_lock:
            self._tok["t"] = None
            TOKEN_CACHE.unlink(missing_ok=True)

    # Toss rate-limits per API group (ACCOUNT ≈ 1/s, ORDER_INFO ≈ 6/s). Every open window and
    # the trading engine all share these, so calls are spaced out here instead of hitting 429s.
    _throttle = {"account": (1.1, threading.Lock()), "order_info": (0.2, threading.Lock())}
    _last: dict = {}

    @staticmethod
    def _group(method, path):
        if any(k in path for k in ("/holdings", "/buying-power", "/accounts")):
            return "account"
        if method == "GET" and "/orders" in path:
            return "order_info"
        return None

    def _req(self, method, path, account=False, retries=None, **kw):
        """GETs retry on network errors / 429 / 5xx. POSTs retry ONLY on 429 (request was rejected,
        never processed) — anything else is raised so the caller can check what actually happened."""
        retries = retries or (5 if method == "GET" else 3)
        g = self._group(method, path)
        last_err = None
        for attempt in range(retries):
            if g:
                gap, lock = self._throttle[g]
                with lock:
                    wait = self._last.get(g, 0) + gap - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    self._last[g] = time.time()
            h = {"Authorization": f"Bearer {self.token()}"}
            if account:
                h["X-Tossinvest-Account"] = self.account
            try:
                r = requests.request(method, f"{BASE}{path}", headers=h, timeout=10, **kw)
            except requests.RequestException as e:
                if method != "GET":
                    raise
                last_err = e; time.sleep(1 + attempt); continue
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1)) + 0.2 * attempt); continue
            if r.status_code == 401 and attempt == 0:   # token revoked/expired early → fetch a new one
                self.drop_token(); continue
            if r.status_code == 403:
                raise PermissionError(f"403 — is this machine's IP registered in Toss 허용 IP? {r.text}")
            if r.status_code >= 500 and method == "GET":
                last_err = RuntimeError(f"{r.status_code} {r.text[:200]}"); time.sleep(1 + attempt); continue
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} {r.text[:300]}", response=r)
            return r.json()
        raise TossUnavailable(f"Toss API unavailable: {path} ({last_err or 'rate-limited'})")

    # ── read-only ────────────────────────────────────────────
    def prices(self, symbols):        return self._req("GET", "/api/v1/prices", params={"symbols": ",".join(symbols)})
    def candles(self, symbol, **p):   return self._req("GET", "/api/v1/candles", params={"symbol": symbol, **p})
    def holdings(self):               return self._req("GET", "/api/v1/holdings", account=True)
    def buying_power(self, currency="USD"): return self._req("GET", "/api/v1/buying-power", account=True, params={"currency": currency})

    def market_calendar(self, market): return self._req("GET", f"/api/v1/market-calendar/{market}")["result"]
    def usd_krw(self) -> float:
        r = self._req("GET", "/api/v1/exchange-rate", params={"baseCurrency": "USD", "quoteCurrency": "KRW"})["result"]
        return float(r["midRate"])

    # ── orders (REAL MONEY): the executor in app/core/accounts.py calls these ──
    def submit_order(self, body) -> str:
        return self._req("POST", "/api/v1/orders", account=True, json=body)["result"]["orderId"]

    def list_orders(self, status) -> list:
        return self._req("GET", "/api/v1/orders", account=True, params={"status": status})["result"]["orders"]

    def get_order(self, oid) -> dict:
        return self._req("GET", f"/api/v1/orders/{oid}", account=True)["result"]

    def cancel_order(self, oid):
        return self._req("POST", f"/api/v1/orders/{oid}/cancel", account=True, json={})

    def place_order(self, symbol, side, quantity=None, price=None, order_type="LIMIT",
                    tif="DAY", amount=None, dry_run=True):
        """amount = dollar-amount MARKET order (US regular session only)."""
        body = {"clientOrderId": str(uuid.uuid4()), "symbol": symbol, "side": side.upper(),
                "orderType": order_type, "timeInForce": tif}
        if amount is not None:
            body.update(orderType="MARKET", orderAmount=str(amount))
        else:
            body["quantity"] = str(quantity)
        if price is not None:
            body["price"] = str(price)
        is_kr = symbol.isdigit()
        cap = float(os.environ.get("MAX_ORDER_KRW" if is_kr else "MAX_ORDER_USD", 0))
        notional = float(amount) if amount is not None else float(quantity) * float(price or 0)
        live = (not dry_run) and os.environ.get("REAL_TRADING_CONFIRMED") == "yes"
        if not live:
            print("[DRY-RUN] would send", body); return {"dry_run": True, "order": body}
        if (amount is None and price is None) or notional > cap:
            raise RuntimeError(f"Blocked: notional {notional} exceeds cap {cap} (or market order without price)")
        return self._req("POST", "/api/v1/orders", account=True, json=body)
