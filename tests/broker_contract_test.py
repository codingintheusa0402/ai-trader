"""The real-money path, end to end, against an in-memory fake broker (no network, no real account).

    .venv/bin/python tests/broker_contract_test.py

Checks the plug-in contract in trader/brokers/base.py the way the engine uses it: arming, going live, a real BUY
and SELL sent through submit_order / get_order, the order journal, the combined loss limit, the kill switch.
A new broker adapter should pass the same calls (swap FakeBroker for it against its paper server).
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["AITRADER_STATE_DIR"] = tempfile.mkdtemp(prefix="aitrader-contract-")
os.environ["AITRADER_LLM_CMD"] = "off"
for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "KIS_APP_KEY", "KIS_APP_SECRET"):
    os.environ[k] = ""
(Path(os.environ["AITRADER_STATE_DIR"]) / "app").mkdir(parents=True)

from trader.brokers.base import Broker  # noqa: E402


class FakeBroker(Broker):
    name, label, tested = "fake", "fake", True

    def __init__(self, cash=1000.0):
        self.cash, self.pos, self.orders, self.sent = cash, {}, {}, []

    def token(self): return "fake"
    def prices(self, symbols):
        now = datetime.now(timezone.utc).isoformat()
        return {"result": [{"symbol": s, "lastPrice": 100.0, "timestamp": now} for s in symbols]}
    def holdings(self): return {"result": {"items": [{"symbol": s, "quantity": q} for s, q in self.pos.items() if q]}}
    def buying_power(self, currency="USD"): return {"result": {"cashBuyingPower": str(self.cash)}}

    def submit_order(self, body):
        oid = uuid.uuid4().hex
        q, px = float(body["quantity"]), float(body["price"])
        self.sent.append(body)
        if body["side"] == "BUY":
            self.cash -= q * px + 0.1
            self.pos[body["symbol"]] = self.pos.get(body["symbol"], 0) + q
        else:
            self.cash += q * px - 0.1
            self.pos[body["symbol"]] = self.pos.get(body["symbol"], 0) - q
        self.orders[oid] = {"orderId": oid, "symbol": body["symbol"], "side": body["side"], "quantity": q, "price": px,
                            "orderedAt": datetime.now(timezone.utc).isoformat(), "status": "FILLED",
                            "execution": {"filledQuantity": str(q), "averageFilledPrice": str(px),
                                          "filledAmount": str(q * px), "commission": "0.1", "tax": "0"}}
        return oid

    def list_orders(self, status):
        return [o for o in self.orders.values() if (o["status"] == "OPEN") == (status == "OPEN")]
    def get_order(self, oid): return self.orders[oid]
    def cancel_order(self, oid): self.orders[oid]["status"] = "CANCELED"


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


from app.server import engine          # noqa: E402
from app.core import accounts, bootstrap  # noqa: E402
from app.core.accounts import RealState  # noqa: E402

fake = FakeBroker()
engine.toss = fake
bootstrap.ensure_plans(engine)
p = next(x for x in engine.plans.values() if x.pid == "vector" and x.mode == "real")
engine.attach_executor(p)

# the test unlocks what .env, the typed phrase and the readiness bar normally guard
RealState.env_unlocked = classmethod(lambda cls: True)
RealState.caps = classmethod(lambda cls: {"budget": {"USD": 300.0, "KRW": 0}, "plan": {"USD": 200.0, "KRW": 0},
                                          "order": {"USD": 150.0, "KRW": 0}, "orders_per_day": 40})
engine.require_ready = lambda *a, **k: None

try:
    engine.update_plan(p.id, dry_run=False, budget=150)
    check("going live without arming is refused", False)
except ValueError as e:
    check("going live without arming is refused", True, str(e))
engine.arm("REAL MONEY")
try:
    engine.update_plan(p.id, dry_run=False, budget=250)
    check("per-trader cap refuses $250", False)
except ValueError as e:
    check("per-trader cap refuses $250", True, str(e))
engine.update_plan(p.id, dry_run=False, budget=150)
check("trader is live with $150", not p.dry_run and p.budget == 150)
lim = engine.prefs.get("real_limits") or {}
check("combined loss limit started", lim.get("base") == 150 and lim.get("floor") == 127.5, str(lim))

accounts.is_open = lambda *a, **k: True
quote = {"price": 100.0, "age": 1.0, "src": "toss"}
check("per-order cap blocks a $200 order", accounts.risk_gate(p, "BUY", "AAPL", 2, quote, 100.0) is not None)
ok = engine.buy(p, "AAPL", 1, quote, 100.0, {"trend": .5}, 0.6)
check("real BUY sent through the broker and filled", ok and fake.sent and p.book.positions.get("AAPL", {}).get("qty") == 1,
      str(fake.sent[-1] if fake.sent else None))
check("order is a marketable LIMIT, never market", fake.sent[-1]["orderType"] == "LIMIT" and float(fake.sent[-1]["price"]) > 100)
ok = engine.sell(p, "AAPL", 1, quote, 100.0, "take profit +0.0%")
check("real SELL filled", ok and "AAPL" not in p.book.positions)
check("cannot sell more than the trader owns", accounts.risk_gate(p, "SELL", "AAPL", 1, quote, 100.0) is not None)
audit = Path(os.environ["AITRADER_STATE_DIR"], "audit_real.jsonl").read_text()
check("journal: intent before sent before result", audit.index('"intent"') < audit.index('"sent"') < audit.index('"result"'))
check("a Yahoo price is never used for real money",
      accounts.risk_gate(p, "BUY", "AAPL", 1, dict(quote, src="yf"), 100.0) is not None)
engine.kill(False)
check("kill switch disarms", RealState.killed and not RealState.armed)
try:
    engine.buy(p, "AAPL", 1, quote, 100.0, {"trend": .5}, 0.6)
    check("no buys after the kill switch", len(fake.sent) == 2)
except accounts.Halt:
    check("no buys after the kill switch", True)

print(f"\n{sum(results)}/{len(results)} passed", flush=True)
os._exit(0 if all(results) else 1)
