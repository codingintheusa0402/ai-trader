"""End-to-end dry-run check that works at any hour, with no broker keys and no LLM.

    .venv/bin/python tests/smoke_test.py

It starts a throwaway engine in a temp folder, creates 대길 and 아귀 from traders.toml (dry-run), buys and sells
one real US stock at its current Yahoo price through the normal risk gate and executor (the market-hours check is
switched on for the test only), then checks that every real-money door stays shut. Nothing is sent to a broker.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["AITRADER_STATE_DIR"] = tempfile.mkdtemp(prefix="aitrader-smoke-")
os.environ["AITRADER_LLM_CMD"] = "off"
for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "KIS_APP_KEY", "KIS_APP_SECRET"):
    os.environ[k] = ""          # never talk to a real broker from the test, even if .env has keys
os.environ["BROKER"] = "toss"
(Path(os.environ["AITRADER_STATE_DIR"]) / "app").mkdir(parents=True)

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


from app.server import engine, plan_view          # noqa: E402  (builds the engine on the temp state)
from app.core import accounts, bootstrap, data, live  # noqa: E402
from app.core.accounts import RealState, Halt, TossExecutor  # noqa: E402

check("no broker client without keys (dry-run uses Yahoo)", data.toss() is None)
bootstrap.ensure_plans(engine)
plans = {p.pid: p for p in engine.plans.values() if p.mode == "real" and not p.league}
check("traders.toml created 대길 and 아귀", set(plans) == {"vector", "blaze"}, ", ".join(plans))
check("both start in dry-run", all(p.dry_run for p in plans.values()))
bootstrap.ensure_plans(engine)
check("a second start does not duplicate plans",
      len([p for p in engine.plans.values() if p.mode == "real" and not p.league]) == 2)

p = plans["vector"]
sym = "AAPL"
q = data.quotes([sym]).get(sym)
check("live Yahoo quote", bool(q and q["price"] > 0), f"{sym} {q and q['price']}")
px = q["price"]
quote = q                                          # the engine's own quote, as a trader would get it
check("without a broker, Yahoo counts as a live price for dry-run", q["age"] <= 90, f"age {q['age']}, src {q.get('src')}")
accounts.is_open = lambda market, *a, **k: True        # test only: pretend the market is open
before = p.book.cash
ok = engine.buy(p, sym, 1, quote, px, {"trend": 0.5, "momentum": 0.4}, 0.6)
check("dry-run BUY filled through the risk gate", ok and sym in p.book.positions,
      f"cash {before:,.2f} -> {p.book.cash:,.2f}")
ok = engine.sell(p, sym, 1, quote, px, "take profit (test)")
check("dry-run SELL filled", ok and sym not in p.book.positions, f"trades {len(p.book.trades)}")
audit = Path(os.environ["AITRADER_STATE_DIR"], "audit_real.jsonl").read_text()
check("order journal shows dry fills only", '"dry_fill"' in audit and '"sent"' not in audit)

try:
    engine.update_plan(p.id, dry_run=False)
    check("going live is refused while locked", False)
except ValueError as e:
    check("going live is refused while locked", "arm" in str(e) or "locked" in str(e), str(e))
try:
    engine.arm("REAL MONEY")
    check("arming is refused without REAL_TRADING_CONFIRMED=yes", False)
except ValueError as e:
    check("arming is refused without REAL_TRADING_CONFIRMED=yes", True, str(e))
try:
    engine.arm("real money")
    check("arming needs the exact phrase", False)
except ValueError:
    check("arming needs the exact phrase", True)
try:
    TossExecutor(None, dry_run=False).execute("US", "BUY", sym, 1, px, "test")
    check("a real order cannot be sent while disarmed", False)
except Halt as e:
    check("a real order cannot be sent while disarmed", True, str(e))

feed = live.feed(engine, "vector", 0, 50)
msgs = feed.get("messages", feed) if isinstance(feed, dict) else feed
text = " ".join(" ".join(m.get("parts") or [m.get("text", "")]) for m in msgs)
check("Live chat shows the trade in Korean", "샀" in text or "팔" in text or "익절" in text, text[:80])
rooms = [r["pid"] for r in live.rooms(engine)]
check("Live rooms: 단톡방 + 대길 + 아귀", rooms[:1] == ["group"] and {"vector", "blaze"} <= set(rooms), str(rooms))

print()
print(f"{sum(results)}/{len(results)} passed", flush=True)
os._exit(0 if all(results) else 1)
