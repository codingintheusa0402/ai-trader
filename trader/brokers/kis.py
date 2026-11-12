"""한국투자증권 KIS Open API adapter: US stocks (해외주식).

NOT TESTED. Written from KIS's public API documentation; it has never placed an order or read a real account.
Before you trust it with money:
  1. Run it against KIS's paper server first (KIS_ENV=paper, a 모의투자 account) with a dry-run plan, then a tiny
     real plan. Read state/audit_real.jsonl after every order.
  2. Expect to fix field names. KIS returns different fields per endpoint and the docs change.
Known gaps: US stocks only (Korean stocks are not wired up), commission is not reported per fill (P&L uses the
engine's fee estimate), quotes carry no exchange timestamp (the time of the request is used instead).

.env:
    BROKER=kis
    KIS_APP_KEY=...  KIS_APP_SECRET=...
    KIS_ACCOUNT=12345678-01        (계좌번호 8자리-상품코드 2자리)
    KIS_ENV=paper                  (paper = 모의투자 server, real = 실전)
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from trader import config as C
from .base import Broker, BrokerUnavailable

load_dotenv(C.ROOT / ".env")
URLS = {"real": "https://openapi.koreainvestment.com:9443", "paper": "https://openapivts.koreainvestment.com:29443"}
TOKEN_CACHE = C.STATE_DIR / ".kis_token.json"
EXCHANGES = ("NASD", "NYSE", "AMEX")                 # order/balance exchange codes
QUOTE_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}   # quote exchange codes
TR = {   # (real, paper)
    "price": ("HHDFS00000300", "HHDFS00000300"),
    "buy": ("TTTT1002U", "VTTT1002U"),
    "sell": ("TTTT1006U", "VTTT1001U"),
    "cancel": ("TTTT1004U", "VTTT1004U"),
    "ccnl": ("TTTS3035R", "VTTS3035R"),
    "nccs": ("TTTS3018R", "VTTS3018R"),
    "balance": ("TTTS3012R", "VTTS3012R"),
    "psamount": ("TTTS3007R", "VTTS3007R"),
}


class KisClient(Broker):
    name = "kis"
    label = "한국투자증권"
    tested = False

    def __init__(self):
        self.key = os.environ.get("KIS_APP_KEY", "").strip()
        self.secret = os.environ.get("KIS_APP_SECRET", "").strip()
        acct = os.environ.get("KIS_ACCOUNT", "").strip()
        if not (self.key and self.secret and "-" in acct):
            raise RuntimeError("KIS keys not set (KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT=12345678-01 in .env)")
        self.cano, self.prdt = acct.split("-", 1)
        self.env = "paper" if os.environ.get("KIS_ENV", "paper").strip().lower() != "real" else "real"
        self.base = URLS[self.env]
        self._tok, self._lock, self._last = None, threading.Lock(), 0.0
        self._gap = 0.55 if self.env == "paper" else 0.06    # paper server allows ~2 calls/s
        self._excd: dict = {}                                # symbol -> NASD/NYSE/AMEX, learned from quotes
        self._orders: dict = {}                              # orderId -> {"symbol", "excd"}

    # ── auth ──
    def token(self) -> str:
        with self._lock:
            t = self._tok
            if not t and TOKEN_CACHE.exists():
                try:
                    t = json.loads(TOKEN_CACHE.read_text())
                    t = t if t.get("env") == self.env else None
                except (ValueError, OSError):
                    t = None
            if t and t["expires_at"] - time.time() > 600:
                self._tok = t
                return t["access_token"]
            # KIS allows one new token per minute; it is cached on disk across restarts
            r = requests.post(f"{self.base}/oauth2/tokenP", timeout=10, json={
                "grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
            r.raise_for_status()
            t = r.json()
            t.update(expires_at=time.time() + int(t.get("expires_in", 86400)), env=self.env)
            self._tok = t
            TOKEN_CACHE.parent.mkdir(exist_ok=True)
            fd = os.open(TOKEN_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(t))
            return t["access_token"]

    def _req(self, method, path, tr, params=None, body=None, retries=None):
        retries = retries or (4 if method == "GET" else 1)
        last = None
        for attempt in range(retries):
            wait = self._last + self._gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            h = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self.token()}",
                 "appkey": self.key, "appsecret": self.secret, "tr_id": TR[tr][self.env == "paper"], "custtype": "P"}
            try:
                r = requests.request(method, f"{self.base}{path}", headers=h, params=params, json=body, timeout=10)
            except requests.RequestException as e:
                if method != "GET":
                    raise
                last = e; time.sleep(1 + attempt); continue
            if r.status_code >= 500 and method == "GET":
                last = RuntimeError(f"{r.status_code} {r.text[:200]}"); time.sleep(1 + attempt); continue
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} {r.text[:300]}", response=r)
            j = r.json()
            if str(j.get("rt_cd", "0")) != "0":
                if "초당" in str(j.get("msg1", "")) and method == "GET":   # rate limited
                    time.sleep(1 + attempt); continue
                raise requests.HTTPError(f"KIS {j.get('msg_cd')}: {j.get('msg1')}", response=r)
            return j
        raise BrokerUnavailable(f"KIS API unavailable: {path} ({last or 'rate-limited'})")

    def _acct(self, **kw):
        return {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt, **kw}

    @staticmethod
    def _us(symbol):
        if symbol.isdigit():
            raise NotImplementedError("KIS adapter: Korean stocks are not wired up (US stocks only)")

    # ── read-only ──
    def prices(self, symbols):
        out = []
        for s in symbols:
            self._us(s)
            for ex in ([self._excd[s]] if s in self._excd else EXCHANGES):
                o = self._req("GET", "/uapi/overseas-price/v1/quotations/price", "price",
                              params={"AUTH": "", "EXCD": QUOTE_EXCD[ex], "SYMB": s}).get("output") or {}
                last = float(o.get("last") or 0)
                if last > 0:
                    self._excd[s] = ex
                    # no exchange timestamp in this endpoint: the request time stands in for it
                    out.append({"symbol": s, "lastPrice": last, "timestamp": datetime.now(timezone.utc).isoformat()})
                    break
        return {"result": out}

    def _exchange(self, symbol):
        if symbol not in self._excd:
            self.prices([symbol])
        if symbol not in self._excd:
            raise ValueError(f"KIS: no US exchange found for {symbol}")
        return self._excd[symbol]

    def holdings(self):
        items = {}
        for ex in EXCHANGES:
            j = self._req("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", "balance", params=self._acct(
                OVRS_EXCG_CD=ex, TR_CRCY_CD="USD", CTX_AREA_FK200="", CTX_AREA_NK200=""))
            for r in j.get("output1") or []:
                q = float(r.get("ovrs_cblc_qty") or 0)
                if q > 0:
                    sym = r.get("ovrs_pdno")
                    self._excd[sym] = ex
                    items[sym] = {"symbol": sym, "quantity": q, "averagePrice": float(r.get("pchs_avg_pric") or 0)}
        return {"result": {"items": list(items.values())}}

    def buying_power(self, currency="USD"):
        if currency != "USD":
            raise NotImplementedError("KIS adapter: USD only")
        j = self._req("GET", "/uapi/overseas-stock/v1/trading/inquire-psamount", "psamount", params=self._acct(
            OVRS_EXCG_CD="NASD", OVRS_ORD_UNPR="1", ITEM_CD="AAPL"))
        o = j.get("output") or {}
        return {"result": {"cashBuyingPower": o.get("ord_psbl_frcr_amt") or o.get("ovrs_ord_psbl_amt") or "0"}}

    # ── orders (REAL MONEY on KIS_ENV=real) ──
    def submit_order(self, body) -> str:
        sym = body["symbol"]
        self._us(sym)
        ex = self._exchange(sym)
        j = self._req("POST", "/uapi/overseas-stock/v1/trading/order", "buy" if body["side"] == "BUY" else "sell",
                      body=self._acct(OVRS_EXCG_CD=ex, PDNO=sym, ORD_QTY=str(int(float(body["quantity"]))),
                                      OVRS_ORD_UNPR=body["price"], ORD_SVR_DVSN_CD="0", ORD_DVSN="00"))
        oid = (j.get("output") or {}).get("ODNO")
        if not oid:
            raise RuntimeError(f"KIS order: no order number in reply {j}")
        self._orders[oid] = {"symbol": sym, "excd": ex}
        return oid

    def _history(self):
        day = datetime.now(timezone(timedelta(hours=9)))
        j = self._req("GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", "ccnl", params=self._acct(
            PDNO="%", ORD_STRT_DT=f"{day - timedelta(days=1):%Y%m%d}", ORD_END_DT=f"{day:%Y%m%d}", SLL_BUY_DVSN="00",
            CCLD_NCCS_DVSN="00", OVRS_EXCG_CD="%", SORT_SQN="DS", ORD_DT="", ORD_GNO_BRNO="", ODNO="",
            CTX_AREA_NK200="", CTX_AREA_FK200=""))
        return [self._norm(r) for r in j.get("output") or []]

    @staticmethod
    def _norm(r):
        qty, done = float(r.get("ft_ord_qty") or 0), float(r.get("ft_ccld_qty") or 0)
        left = float(r.get("nccs_qty") or 0)
        name = str(r.get("prcs_stat_name") or "") + str(r.get("rvse_cncl_dvsn_name") or "")
        if "거부" in name:
            status = "REJECTED"
        elif done >= qty > 0:
            status = "FILLED"
        elif "취소" in name or (left == 0 and done < qty):
            status = "CANCELED"
        else:
            status = "OPEN"
        t = f"{r.get('ord_dt', '')}{r.get('ord_tmd', '')}"
        try:
            ordered = datetime.strptime(t, "%Y%m%d%H%M%S").replace(tzinfo=timezone(timedelta(hours=9))).isoformat()
        except ValueError:
            ordered = datetime.now(timezone.utc).isoformat()
        return {"orderId": r.get("odno"), "symbol": r.get("pdno"),
                "side": "SELL" if r.get("sll_buy_dvsn_cd") == "01" else "BUY",
                "quantity": qty, "price": float(r.get("ft_ord_unpr3") or 0) or None, "orderedAt": ordered,
                "status": status, "excd": r.get("ovrs_excg_cd"),
                "execution": {"filledQuantity": str(done), "averageFilledPrice": str(r.get("ft_ccld_unpr3") or 0),
                              "commission": "0", "tax": "0"}}

    def list_orders(self, status) -> list:
        rows = self._history()
        return [o for o in rows if (o["status"] == "OPEN") == (status == "OPEN")]

    def get_order(self, oid) -> dict:
        for o in self._history():
            if o["orderId"] == oid:
                return o
        return {"orderId": oid, "status": "OPEN", "execution": {}}

    def cancel_order(self, oid):
        info = self._orders.get(oid) or next(({"symbol": o["symbol"], "excd": o.get("excd")}
                                              for o in self._history() if o["orderId"] == oid), None)
        if not info:
            raise ValueError(f"KIS cancel: unknown order {oid}")
        return self._req("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", "cancel", body=self._acct(
            OVRS_EXCG_CD=info["excd"] or self._exchange(info["symbol"]), PDNO=info["symbol"], ORGN_ODNO=oid,
            RVSE_CNCL_DVSN_CD="02", ORD_QTY="0", OVRS_ORD_UNPR="0", ORD_SVR_DVSN_CD="0"))
