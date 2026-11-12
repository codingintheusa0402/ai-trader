"""Books (per-plan ledgers), executors (mock / Toss real), and the RiskGate every order must pass.

Real-money safety (fail-closed):
  • Real orders need: .env REAL_TRADING_CONFIRMED=yes  AND  the app armed with a typed phrase
  • RiskGate: market open, fresh quote (<90s), price collar, per-order cap, daily order cap,
    cash/buying-power check, sell ≤ bot-owned qty ≤ actual holding, cooldown, kill switch
  • Marketable LIMIT orders only (never market), tick-rounded, idempotent clientOrderId
  • Intent is journaled BEFORE sending; result journaled after (state/audit_real.jsonl)
  • Unfilled remainder is cancelled after 20s; fills use Toss's actual execution data
  • Any unexpected error → the plan halts (never retries blindly)
"""
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime
from dotenv import load_dotenv, dotenv_values
from trader import config as C
from .markets import FEES, SLIPPAGE, CCY, KR_ETF, fee, tick_round, is_open


def sell_cost(market, sym):
    f = FEES[market]
    return f["sell"] + (0.0 if sym in KR_ETF else f["sell_tax"])

load_dotenv(C.ROOT / ".env", override=True)
AUDIT = C.STATE_DIR / "audit_real.jsonl"
_audit_lock = threading.Lock()


def audit(event: str, **kw):
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
    with _audit_lock:
        AUDIT.parent.mkdir(exist_ok=True)
        with open(AUDIT, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Book:
    """Cash + positions owned by one plan. Positions: sym -> {qty, cost (incl. buy fee), peak_ret, sig, opened}."""

    def __init__(self, market, cash, state=None):
        s = state or {}
        self.market = market
        self.cash = s.get("cash", cash)
        self.positions = s.get("positions", {})
        self.trades = s.get("trades", [])
        self.equity_log = s.get("equity_log", [])
        self.on_cash = None   # real USD plans: tells the engine's cash ledger about every fill (see Engine.transfer_check)

    def to_dict(self):
        return dict(cash=self.cash, positions=self.positions, trades=self.trades[-500:],
                    equity_log=self.equity_log[-2000:])

    def gross(self, prices):
        return self.cash + sum(p["qty"] * prices.get(s, p["cost"] / p["qty"]) for s, p in self.positions.items())

    def net(self, prices):
        """Liquidation value after sell fees/taxes — what you'd really walk away with."""
        return self.cash + sum(p["qty"] * prices.get(s, p["cost"] / p["qty"]) * (1 - sell_cost(self.market, s))
                               for s, p in list(self.positions.items()))

    def pos_net_ret(self, sym, price):
        p = self.positions[sym]
        return p["qty"] * price * (1 - sell_cost(self.market, sym)) / p["cost"] - 1

    def apply_buy(self, sym, qty, avg, fees, sig, when):
        cost = qty * avg + fees
        self.cash -= cost
        if self.on_cash:
            self.on_cash(-cost)
        p = self.positions.get(sym)
        if p:
            p["qty"] += qty; p["cost"] += cost
        else:
            self.positions[sym] = {"qty": qty, "cost": cost, "peak_ret": 0.0, "sig": sig, "opened": when, "t_open": time.time()}
        self.trades.append(dict(time=when, side="BUY", symbol=sym, qty=qty, price=round(avg, 4), fee=round(fees, 4)))

    def apply_sell(self, sym, qty, avg, fees, reason, when):
        p = self.positions[sym]
        frac = qty / p["qty"]
        cost_part = p["cost"] * frac
        proceeds = qty * avg - fees
        self.cash += proceeds
        if self.on_cash:
            self.on_cash(proceeds)
        net_ret = proceeds / cost_part - 1
        p["qty"] -= qty; p["cost"] -= cost_part
        sig = p.get("sig", {})
        if p["qty"] <= 1e-9:
            del self.positions[sym]
        tr = dict(time=when, side="SELL", symbol=sym, qty=qty, price=round(avg, 4), fee=round(fees, 4),
                  pnl=round(proceeds - cost_part, 4), net_ret=round(net_ret, 5), reason=reason)
        self.trades.append(tr)
        return tr, sig, net_ret


class MockExecutor:
    """Fills at the live quote ± slippage, with Toss's real fee schedule."""
    real = False

    def execute(self, market, side, sym, qty, ref_price, plan_id=""):
        px = ref_price * (1 + SLIPPAGE if side == "BUY" else 1 - SLIPPAGE)
        return {"qty": qty, "avg": px, "fees": fee(market, side, qty * px, sym)}


class Halt(Exception):
    pass


class OrderUnknown(Halt):
    """An order reached Toss but its final state couldn't be read. The plan halts and the order is
    resolved later (reconcile) so real shares are never left untracked."""
    def __init__(self, msg, order):
        super().__init__(msg)
        self.order = order


ARMED_FILE = C.STATE_DIR / "app" / "armed"
# Two engines trading one account would double every order: run one engine per account (lease.py).
STANDBY = os.environ.get("AITRADER_ROLE") == "standby"
from .lease import LEASE  # noqa: E402


class RealState:
    armed = ARMED_FILE.exists()   # stays armed across engine restarts until you disarm / use the kill switch
    killed = False
    flattening = False     # kill switch "sell everything": only SELLs may pass while this is set
    orders_today: dict = {}

    # read the .env file itself every time: removing/commenting a line takes effect immediately
    @classmethod
    def set_armed(cls, on: bool):
        cls.armed = on
        try:
            if on:
                ARMED_FILE.write_text(datetime.now().isoformat(timespec="seconds"))
            else:
                ARMED_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    @classmethod
    def env_unlocked(cls):
        return (dotenv_values(C.ROOT / ".env").get("REAL_TRADING_CONFIRMED") or "").strip() == "yes"

    @classmethod
    def caps(cls):
        env = dotenv_values(C.ROOT / ".env")
        g = lambda k, d: float((env.get(k) or "").split("#")[0].strip() or d)
        return {"budget": {"USD": g("MAX_BUDGET_USD", 200), "KRW": g("MAX_BUDGET_KRW", 200000)},   # all traders
                "plan": {"USD": g("MAX_PLAN_USD", 200), "KRW": g("MAX_PLAN_KRW", 200000)},        # per investor
                "order": {"USD": g("MAX_ORDER_USD", 100), "KRW": g("MAX_ORDER_KRW", 100000)},
                "orders_per_day": int(g("MAX_ORDERS_PER_DAY", 40))}

    @classmethod
    def count_order(cls):
        """Real orders in the last 24 h (the US session crosses midnight KST) — rebuilt from the order
        journal, so restarting can't reset it."""
        now = time.time()
        if "sent" not in cls.orders_today:
            ts = []
            if AUDIT.exists():
                with open(AUDIT) as f:
                    for line in f:
                        if '"event": "sent"' in line:
                            try:
                                ts.append(datetime.fromisoformat(json.loads(line)["ts"]).timestamp())
                            except Exception:
                                pass
            cls.orders_today = {"sent": ts}
        cls.orders_today["sent"] = [t for t in cls.orders_today["sent"] if now - t < 86400] + [now]
        return len(cls.orders_today["sent"])


class TossExecutor:
    real = True

    def __init__(self, client, dry_run=True):
        self.c, self.dry_run = client, dry_run

    def execute(self, market, side, sym, qty, ref_price, plan_id=""):
        if RealState.killed and not (RealState.flattening and side == "SELL"):
            raise Halt("kill switch active")
        if not self.dry_run and not LEASE.allow(side):   # last line of defence (risk_gate checks first)
            raise Halt(f"no real-money lease ({LEASE.status()}) — the other engine is trading")
        if not self.dry_run and not RealState.flattening and not (RealState.armed and RealState.env_unlocked()):
            raise Halt("real trading not armed")
        limit = tick_round(ref_price * (1.003 if side == "BUY" else 0.997), market, up=side == "BUY", sym=sym)
        q = float(qty)   # whole shares as "8", fractions as "0.1036" — never "8.0"
        qty_s = str(int(q)) if q.is_integer() else f"{q:.6f}".rstrip("0")
        body = {"clientOrderId": str(uuid.uuid4()), "symbol": sym, "side": side, "orderType": "LIMIT",
                "timeInForce": "DAY", "quantity": qty_s, "price": f"{limit:.4f}".rstrip("0").rstrip(".")}
        audit("intent", plan=plan_id, dry_run=self.dry_run, **body)
        if self.dry_run:
            px = ref_price * (1 + SLIPPAGE if side == "BUY" else 1 - SLIPPAGE)
            audit("dry_fill", plan=plan_id, symbol=sym, qty=qty, price=px)
            return {"qty": qty, "avg": px, "fees": fee(market, side, qty * px, sym), "dry_run": True}

        caps = RealState.caps()
        if RealState.count_order() > caps["orders_per_day"]:
            raise Halt("daily order limit reached")
        sent_at = time.time()
        try:
            oid = self.c.submit_order(body)
        except Exception as e:
            # the request may still have reached Toss (e.g. a timeout) — look before deciding
            audit("send_error", plan=plan_id, error=str(e)[:300])
            oid = self.find_order(sym, side, qty, limit, sent_at)
            if not oid:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code and 400 <= code < 500:
                    audit("rejected", plan=plan_id, symbol=sym, error=str(e)[:300])
                    return None          # Toss refused it (e.g. not enough cash) — nothing was placed
                raise Halt(f"order send failed and no matching order found: {e}")
            audit("recovered", plan=plan_id, orderId=oid)
        audit("sent", plan=plan_id, orderId=oid, clientOrderId=body["clientOrderId"])
        return self.settle_order(oid, plan_id, sym, side)

    TERMINAL = ("FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED")

    def find_order(self, sym, side, qty, limit, since):
        """Find an order we just sent (matched on symbol/side/qty/price, placed in the last 2 min)."""
        for st in ("OPEN", "CLOSED"):
            try:
                orders = self.c.list_orders(st)
            except Exception:
                continue
            for o in orders:
                try:
                    t = datetime.fromisoformat(o["orderedAt"]).timestamp()
                except Exception:
                    continue
                if (o["symbol"] == sym and o["side"] == side and t >= since - 120
                        and abs(float(o.get("quantity") or 0) - float(qty)) < 1e-6
                        and (o.get("price") is None or abs(float(o["price"]) - float(limit)) < 1e-6)):
                    return o["orderId"]
        return None

    def get_order(self, oid):
        return self.c.get_order(oid)

    def settle_order(self, oid, plan_id, sym, side):
        """Wait up to ~20 s for a fill, cancel any unfilled remainder, return the actual fill."""
        o = None
        try:
            for _ in range(20):
                time.sleep(1)
                o = self.get_order(oid)
                if o["status"] in self.TERMINAL:
                    break
            if o["status"] not in self.TERMINAL:
                try:
                    self.c.cancel_order(oid)
                    audit("cancel_remainder", plan=plan_id, orderId=oid)
                except Exception as e:
                    audit("cancel_error", plan=plan_id, orderId=oid, error=str(e)[:300])
                time.sleep(1.5)
                o = self.get_order(oid)
        except Exception as e:
            audit("order_state_unknown", plan=plan_id, orderId=oid, error=str(e)[:300])
            raise OrderUnknown(f"couldn't confirm order {oid[:10]}… ({e})",
                               {"orderId": oid, "symbol": sym, "side": side, "t": time.time()})
        return self.fill_of(o, plan_id)

    def fill_of(self, o, plan_id=""):
        ex = o.get("execution") or {}
        filled = float(ex.get("filledQuantity") or 0)
        audit("result", plan=plan_id, orderId=o["orderId"], status=o["status"], execution=ex)
        if filled <= 0:
            return None
        return {"qty": filled, "avg": float(ex["averageFilledPrice"]),
                "fees": float(ex.get("commission") or 0) + float(ex.get("tax") or 0), "orderId": o["orderId"],
                "final": o["status"] in self.TERMINAL}

    def holdings(self) -> dict:
        items = self.c.holdings()["result"]["items"]
        return {i["symbol"]: float(i["quantity"]) for i in items}

    def buying_power(self, ccy) -> float:
        return float(self.c.buying_power(ccy)["result"]["cashBuyingPower"])

    def open_orders(self) -> list:
        return self.c.list_orders("OPEN")

    def cancel_open(self):
        n = 0
        for o in self.c.list_orders("OPEN"):
            try:
                self.c.cancel_order(o["orderId"])
                audit("kill_cancel", orderId=o["orderId"], symbol=o["symbol"]); n += 1
            except Exception as e:
                audit("kill_cancel_error", orderId=o["orderId"], error=str(e)[:200])
        return n


def risk_gate(plan, side, sym, qty, quote, bar_close) -> str | None:
    """Return None if OK, else the reason the order is blocked. Applied to every order, both modes."""
    if qty <= 0:
        return "zero quantity"
    if not is_open(plan.market):
        return "market closed"
    if quote is None or quote["age"] > 90:
        return "stale quote"
    if plan.mode == "real" and not plan.dry_run and str(quote.get("src", "")).startswith("yf"):
        return "no live Toss quote (real money never trades on a Yahoo price)"
    if plan.mode == "real" and not plan.dry_run and not LEASE.allow(side):
        return f"waiting for the real-money lease ({LEASE.status()}) — only one engine may trade"
    if plan.mode == "real" and not plan.dry_run and quote.get("src") == "yf":
        return "no live Toss price (backup prices can be delayed)"
    px = quote["price"]
    if bar_close and abs(px / bar_close - 1) > 0.05:
        return f"price collar: quote {px} vs last bar {bar_close:.4g}"
    ccy = CCY[plan.market]
    if side == "BUY":
        need = qty * px * (1.003 + FEES[plan.market]["buy"])
        if need > plan.book.cash:
            return "insufficient plan cash"
        if plan.mode == "real" and not plan.dry_run:   # caps guard real money; dry-run is sized by its own budget
            cap = RealState.caps()["order"][ccy]
            if need > cap:
                return f"per-order cap {cap} {ccy}"
            if not plan.dry_run and need > plan.executor.buying_power(ccy):
                return "insufficient Toss buying power"
        if sym in plan.cooldown and plan.cooldown[sym] > time.time():
            return "cooldown"
        from .universe import ADV
        if sym in ADV and qty * px > 0.01 * ADV[sym]:  # small caps: never more than 1% of a day's trading
            return "too large for this stock's daily liquidity"
    else:
        own = plan.book.positions.get(sym, {}).get("qty", 0)
        if qty > own + 1e-9:
            return "sell exceeds bot-owned quantity"
    return None
