"""FastAPI backend for the desktop app."""
import io
import ipaddress
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .core import data, live, news, profiles, universe
import json
from .core.accounts import RealState, TossExecutor, AUDIT, audit
from .core.brain import PERSONAS
from .core.engine import Engine
from .core.markets import CCY, NAMES, is_open, session

app = FastAPI()
engine = Engine()


@app.exception_handler(Exception)
async def _unhandled(request, exc):   # the apps show detail; a bare text 500 only produced a cryptic browser error
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": f"{type(exc).__name__}: {str(exc)[:300]}"}, status_code=500)
STATIC = Path(__file__).parent / "static"
# app version (vN.NN) — the VERSION file at the repo root is the single source of truth; shown in the app header
try:
    VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
except OSError:
    VERSION = "0.00"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ── Phone access (optional): loopback (this computer) is trusted. To open the app from your phone, set
#    AITRADER_MOBILE_HOST in .env to this computer's private VPN address (e.g. its Tailscale 100.x IP); the engine
#    then also listens there on port 8765 and every request needs the pairing token. LAN and internet are refused.
from trader import config as _C
TOKEN_FILE = _C.STATE_DIR / "app" / "mobile_token"
TAILNET = ipaddress.ip_network("100.64.0.0/10")
MOBILE_PORT = 8765


def mobile_token(new=False):
    if new or not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(24))
        os.chmod(TOKEN_FILE, 0o600)
    return TOKEN_FILE.read_text().strip()


def tailscale_ip():
    """The private address for phone access, or None (off, the default)."""
    from dotenv import dotenv_values
    return (os.environ.get("AITRADER_MOBILE_HOST") or dotenv_values(_C.ROOT / ".env").get("AITRADER_MOBILE_HOST") or "").strip() or None


ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MY_NAME = "localhost"


@app.middleware("http")
async def guard(request: Request, call_next):
    hosthdr = (request.headers.get("host") or "").split(":")[0]
    if hosthdr not in ALLOWED_HOSTS and hosthdr != (tailscale_ip() or "") and hosthdr.split(".")[0] != MY_NAME:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)       # DNS-rebinding defence
    if request.method != "GET" and request.headers.get("x-aitrader") != "1":
        return JSONResponse({"detail": "missing app header"}, status_code=403)   # web pages can't send this
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):
        return await call_next(request)
    try:
        ok_net = ipaddress.ip_address(host) in TAILNET
    except ValueError:
        ok_net = False
    path = request.url.path
    if not ok_net:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    if path.startswith("/api/"):
        tok = request.headers.get("x-token") or request.cookies.get("aitoken") or ""
        if not secrets.compare_digest(tok, mobile_token()):
            return JSONResponse({"detail": "not paired — scan the QR code on your Mac"}, status_code=401)
    return await call_next(request)


@app.get("/api/mobile/pair")
def pair():
    import qrcode
    import qrcode.image.svg
    ip = tailscale_ip()
    if not ip:
        bad("Phone access is off: set AITRADER_MOBILE_HOST in .env and restart")
    url = f"http://{ip}:{MOBILE_PORT}/?pair={mobile_token()}"
    buf = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2).save(buf)
    return {"url": url, "svg": buf.getvalue().decode()}


@app.post("/api/mobile/revoke")
def revoke():
    mobile_token(new=True)
    return {"ok": True}


@app.get("/manifest.json")
def manifest():
    return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")


def bad(e):
    raise HTTPException(400, str(e))


@app.get("/")
def index():
    # never let a desktop window / phone keep an old copy of the interface after an update
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.middleware("http")
async def no_stale_assets(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def plan_view(p, full=False):
    q = data.quotes(list(p.book.positions)) if p.book.positions else {}
    prices = {s: v["price"] for s, v in q.items()}
    net = p.book.net(prices)
    pos = []
    for s, v in p.book.positions.items():
        px = prices.get(s, v["cost"] / v["qty"])
        pos.append({"symbol": s, "name": NAMES.get(s, s), "qty": v["qty"], "avg": v["cost"] / v["qty"],
                    "price": px, "value": v["qty"] * px, "net_ret": p.book.pos_net_ret(s, px)})
    d = {"id": p.id, "mode": p.mode, "pid": p.pid, "player": PERSONAS[p.pid]["name"], "market": p.market,
         "ccy": CCY[p.market], "risk": p.risk, "horizon": p.horizon, "risk_name": p.profile.risk_name,
         "horizon_label": p.profile.horizon_label + (" · Monthly cycle" if p.monthly else ""), "budget": p.budget,
         "monthly": p.monthly, "renewed_to": p.renewed_to,
         "universe": p.universe, "universe_label": "Respected daily movers" if p.style == "swing" else universe.MODES[p.universe], "inverse": p.inverse, "style": p.style, "adopted": p.adopted, "min_pct": p.min_pct,
         "max_pct": p.max_pct, "dry_run": p.dry_run, "status": p.status, "reason": p.reason,
         "started": p.started, "deadline": p.deadline, "cash": p.book.cash, "net": net,
         "ret": net / p.budget - 1 if p.budget else 0, "floor_armed": p.floor_armed,
         "take_profit": p.profile.take_profit, "stop_loss": p.profile.stop_loss, "max_loss": p.profile.max_loss,
         "positions": pos, "curve": [e["net"] for e in p.book.equity_log[-300:]],
         "scan": getattr(p, "scan", None), "fractional": p.market == "US" and (p.mode == "mock" or p.dry_run)}
    if full:
        d["events"] = p.events[-150:]
        d["trades"] = p.book.trades[-100:]
    return d


_bp = {"t": 0, "v": {}}


def real_budget():
    """How much real money can be given to traders: min(Toss cash, cap − already assigned)."""
    if time.time() - _bp["t"] > 20 and data.toss():   # Toss ACCOUNT calls are rate-limited (1/s)
        _bp["t"] = time.time()                              # also on failure: retry in 20 s, not every poll
        try:
            ex = TossExecutor(data.toss())
            _bp["v"] = {"USD": ex.buying_power("USD"), "KRW": ex.buying_power("KRW")}
            _bp["ok"] = time.time()
        except Exception:
            pass
    caps = RealState.caps()["budget"]
    out = {}
    for ccy in ("USD", "KRW"):
        used = [{"id": p.id, "player": PERSONAS[p.pid]["name"], "budget": p.budget - p.adopted}
                for p in list(engine.plans.values()) if p.mode == "real" and not p.dry_run
                and p.status in engine.ACTIVE and CCY[p.market] == ccy]
        assigned = sum(u["budget"] for u in used)
        cash = _bp["v"].get(ccy)
        room = caps[ccy] - assigned
        out[ccy] = {"cap": caps[ccy], "plan_cap": RealState.caps()["plan"][ccy], "assigned": assigned, "by": used, "cash": cash,
                    "available": max(0.0, min(room, cash) if cash is not None else room)}
    return out


def mock_account():
    """The fake-money account as one Toss-style view: the amount you set, free wallet cash, what the traders
    virtually hold right now (all running mock plans), and their recent virtual orders."""
    ps = [p for p in list(engine.plans.values()) if p.mode == "mock" and not p.league]
    active = [p for p in ps if p.status in ("running", "waiting", "closing", "paused")]
    cash = {c: float(engine.mock_wallet.get(c, 0)) for c in ("USD", "KRW")}
    stocks = {"USD": 0.0, "KRW": 0.0}
    cost = {"USD": 0.0, "KRW": 0.0}
    hold = []
    for p in active:
        ccy = CCY[p.market]
        cash[ccy] += p.book.cash
        q = data.quotes(list(p.book.positions)) if p.book.positions else {}
        for s, v in p.book.positions.items():
            px = (q.get(s) or {}).get("price") or v["cost"] / v["qty"]
            val = v["qty"] * px
            stocks[ccy] += val; cost[ccy] += v["cost"]
            hold.append({"symbol": s, "name": NAMES.get(s, s), "player": PERSONAS[p.pid]["name"], "ccy": ccy,
                         "qty": v["qty"], "avg": v["cost"] / v["qty"], "price": px, "value": val,
                         "ret": p.book.pos_net_ret(s, px)})
    orders = []
    for p in ps:
        for t in p.book.trades[-60:]:
            orders.append({**t, "player": PERSONAS[p.pid]["name"], "ccy": CCY[p.market], "plan": p.id})
    orders.sort(key=lambda o: (o.get("time") or ""), reverse=True)
    base = {c: float(engine.mock_base.get(c, 0)) for c in ("USD", "KRW")}
    return {"base": base, "wallet": engine.mock_wallet, "cash": cash, "stocks": stocks, "cost": cost,
            "holdings": sorted(hold, key=lambda h: -h["value"]), "orders": orders[:40], "active": len(active)}


@app.get("/api/overview")
def overview():
    user = [p for p in list(engine.plans.values()) if not p.league]
    user.sort(key=lambda p: -p.started)
    act = [p for p in user if p.status in engine.ACTIVE]   # never let an active (e.g. selling) plan fall off the list
    user = act + [p for p in user if p.status not in engine.ACTIVE][:max(0, 12 - len(act))]
    return {"version": VERSION, "monitor": data.monitor(), "open": {"US": is_open("US"), "KR": is_open("KR")}, "session": {"US": session("US"), "KR": session("KR")},
            "wallet": engine.mock_wallet, "mock": mock_account(), "fx": data.usdkrw(),
            "plans": [plan_view(p, full=True) for p in user],
            "role": "standby" if os.environ.get("AITRADER_ROLE") == "standby" else "primary",
            "plan_order": plan_order(),
            "lease": __import__("app.core.lease", fromlist=["LEASE"]).LEASE.view(),
            "real": {"armed": RealState.armed, "killed": RealState.killed, "budget": real_budget(),
                     "env_unlocked": RealState.env_unlocked(), "caps": RealState.caps(),
                     "toss_block": __import__("trader.toss", fromlist=["auth_block"]).auth_block()},
            "league": {pid: engine.player_cfg(pid) for pid in PERSONAS},
            "engine_ok": time.time() - getattr(engine, "heartbeat", time.time()) < 30,
            "time": time.time()}


@app.get("/api/players")
def players():
    return {"board": engine.leaderboard(), "history": engine.league_history[-20:], "cfg": engine.league_cfg,
            "plans": [plan_view(p) for p in list(engine.plans.values()) if p.league]}


# Each AI investor remembers its own Trade-panel settings (defaults fit its personality)
PREF_DEFAULTS = {
    "atlas":  {"market": "US", "universe": "large", "risk": 2, "horizon": "1w"},
    "blaze":  {"market": "US", "universe": "all",   "risk": 3, "horizon": "1d"},
    "echo":   {"market": "US", "universe": "large", "risk": 3, "horizon": "3d"},
    "herald": {"market": "US", "universe": "all",   "risk": 3, "horizon": "1d"},
    "vector": {"market": "US", "universe": "all",   "risk": 4, "horizon": "3d"},
}


ORDER_FILE = _C.STATE_DIR / "app" / "plan_order.json"   # card order on My plans, shared by every open window


def plan_order():
    try:
        return json.loads(ORDER_FILE.read_text())
    except (OSError, ValueError):
        return []


_toss_cache = {"t": 0, "v": None}


@app.get("/api/toss")
def toss_portfolio():
    """Cached 10 s: every open window polls this, and Toss account calls are rate-limited."""
    if _toss_cache["v"] and time.time() - _toss_cache["t"] < 10:
        return _toss_cache["v"]
    if not _toss_cache["v"] and time.time() - _toss_cache["t"] < 10:
        return {"connected": False, "error": "Toss temporarily unavailable — retrying"}
    v = _toss_portfolio()
    _toss_cache.update(t=time.time(), v=v if v.get("connected") else _toss_cache["v"])
    return v


def _toss_portfolio():
    t = data.toss()
    if not t:
        return {"connected": False}
    try:
        ex = TossExecutor(t)
        h = t.holdings()["result"]
        orders = {}
        for st in ("OPEN", "CLOSED"):
            try:
                orders[st] = t.list_orders(st)[:20]
            except Exception:
                orders[st] = []
        bot_ids = set()
        if AUDIT.exists():
            for line in AUDIT.read_text().splitlines()[-2000:]:
                if '"event": "sent"' in line:
                    bot_ids.add(json.loads(line).get("orderId"))
        bot_syms = {s for p in list(engine.plans.values()) if p.mode == "real" and not p.dry_run for s in p.book.positions}
        for o in orders["OPEN"] + orders["CLOSED"]:
            o["bot"] = o["orderId"] in bot_ids
        for i in h["items"]:
            i["bot"] = i["symbol"] in bot_syms
        return {"connected": True, "items": h["items"], "summary": {k: h[k] for k in h if k != "items"},
                "cash": {"USD": ex.buying_power("USD"), "KRW": ex.buying_power("KRW")},
                "open_orders": orders["OPEN"], "recent_orders": orders["CLOSED"], "fx": data.usdkrw(),
                "time": time.time()}
    except Exception as e:
        return {"connected": False, "error": str(e)}


class Start(BaseModel):
    mode: str
    player: str
    market: str
    risk: int
    horizon: str
    budget: float
    min_pct: float | None = None
    max_pct: float | None = None
    dry_run: bool = True
    universe: str = "large"
    inverse: bool = True
    style: str = "normal"
    monthly: bool = False


@app.post("/api/plan/start")
def start(s: Start):
    if s.mode not in ("mock", "real") or s.market not in ("US", "KR") or s.risk not in profiles.RISK \
            or s.horizon not in profiles.HORIZON or s.budget <= 0:
        bad("invalid plan settings")
    for v in (s.min_pct, s.max_pct):
        if v is not None and not (0 < v <= 500):
            bad("profit % must be between 0 and 500")
    try:
        if s.universe not in universe.MODES:
            bad("invalid stock universe")
        p = engine.start_plan(s.mode, s.player, s.market, s.risk, s.horizon, s.budget,
                              s.min_pct, s.max_pct, s.dry_run, s.universe, s.inverse, s.style, monthly=s.monthly)
    except ValueError as e:
        bad(e)
    p.last_tick = 0
    return plan_view(p, full=True)


class Update(BaseModel):
    dry_run: bool | None = None
    market: str | None = None
    risk: int | None = None
    horizon: str | None = None
    universe: str | None = None
    budget: float | None = None
    min_pct: float | None = None
    max_pct: float | None = None
    inverse: bool | None = None
    style: str | None = None
    monthly: bool | None = None


@app.post("/api/plan/{pid}/update")
def update(pid: str, u: Update):
    if pid not in engine.plans:
        bad("no such plan")
    if (u.risk and u.risk not in profiles.RISK) or (u.horizon and u.horizon not in profiles.HORIZON) \
            or (u.universe and u.universe not in universe.MODES) or (u.budget is not None and u.budget <= 0):
        bad("invalid settings")
    for v in (u.min_pct, u.max_pct):
        if v is not None and not (0 < v <= 500):
            bad("profit % must be between 0 and 500")
    sent = u.model_fields_set
    try:
        if u.market not in (None, "US", "KR"):
            bad("invalid market")
        p = engine.update_plan(pid, u.risk, u.horizon, u.universe, u.budget,
                               u.min_pct if "min_pct" in sent else "keep", u.max_pct if "max_pct" in sent else "keep",
                               u.market, u.dry_run, u.inverse, u.style, monthly=u.monthly)
    except ValueError as e:
        bad(e)
    return plan_view(p, full=True)


@app.post("/api/plan/{pid}/pause")
def pause(pid: str):
    if pid not in engine.plans:
        bad("no such plan")
    try:
        return plan_view(engine.pause_plan(pid), full=True)
    except ValueError as e:
        bad(e)


@app.post("/api/plan/{pid}/resume")
def resume(pid: str):
    if pid not in engine.plans:
        bad("no such plan")
    try:
        return plan_view(engine.resume_plan(pid), full=True)
    except ValueError as e:
        bad(e)


@app.post("/api/plan/{pid}/extend")
def extend(pid: str):
    if pid not in engine.plans:
        bad("no such plan")
    try:
        return plan_view(engine.extend_plan(pid), full=True)
    except ValueError as e:
        bad(e)


@app.post("/api/plan/{pid}/remove")
def remove(pid: str):
    if pid not in engine.plans:
        bad("no such plan")
    try:
        engine.remove_plan(pid)
    except ValueError as e:
        bad(e)
    return {"ok": True}


@app.post("/api/plan/{pid}/stop")
def stop(pid: str):
    if pid not in engine.plans:
        bad("no such plan")
    return plan_view(engine.stop_plan(pid), full=True)


class Arm(BaseModel):
    phrase: str


@app.get("/api/transfers")
def transfers():
    return engine.transfer_status()


class Allocate(BaseModel):
    amounts: dict[str, float]


@app.post("/api/real/allocate")
def real_allocate(a: Allocate):
    try:
        return engine.allocate(a.amounts)
    except ValueError as e:
        bad(str(e))


@app.get("/api/live/rooms")
def live_rooms():
    return {"rooms": live.rooms(engine), "armed": RealState.armed, "killed": RealState.killed, "t": time.time()}


@app.get("/api/live/{pid}")
def live_feed(pid: str, since: float = 0.0):
    if pid == live.GROUP:
        return {"messages": live.group_feed(engine, since), "t": time.time()}
    if pid not in live.REAL_PIDS:
        bad("no such real trader")
    return {"messages": live.feed(engine, pid, since), "t": time.time()}


class Ask(BaseModel):
    text: str


@app.post("/api/live/{pid}/ask")
def live_ask(pid: str, a: Ask):
    """Ask-only: never touches orders. A confirm card can only point at the existing pause/resume/stop endpoints."""
    try:
        if pid == live.GROUP:
            return live.ask_group(engine, a.text)
        return live.ask(engine, pid, a.text)
    except ValueError as e:
        bad(str(e))


@app.get("/api/daily-budget")
def daily_budget_get():
    return engine.prefs.get("daily_budget") or {"enabled": False}


class OnOff(BaseModel):
    enabled: bool


@app.post("/api/daily-budget")
def daily_budget_set(o: OnOff):
    """Switch the daily full-account budget on or off (it runs within a minute, once per US trading day)."""
    cfg = engine.prefs.setdefault("daily_budget", {"enabled": False})
    cfg["enabled"] = o.enabled
    audit("daily_budget_switch", enabled=o.enabled)
    engine.save()
    return cfg


@app.post("/api/real/arm")
def arm(a: Arm):
    try:
        engine.arm(a.phrase)
    except ValueError as e:
        bad(e)
    return {"armed": True}


@app.post("/api/real/disarm")
def disarm():
    RealState.set_armed(False)
    return {"armed": False}


class Kill(BaseModel):
    flatten: bool = False


@app.post("/api/real/kill")
def kill(k: Kill):
    return {"cancelled_orders": engine.kill(k.flatten)}


@app.post("/api/train")
def train_now(pid: str | None = None):
    """Run night school now (normally 06:10 KST daily). Takes a minute or two; runs in the background."""
    if engine._training:
        return {"ok": False, "why": "already training"}
    engine._training = True
    engine.bg.submit(engine.train_now, [pid] if pid else None)
    return {"ok": True}


@app.post("/api/report/send")
def report_send(dry: bool = False, full: bool = False):
    """Daily report to the private Chat room now (dry=true only builds it; full=true the old long report)."""
    from app.core import report
    if dry:
        return {"texts": report.send(engine, force=True, dry=True, full=full)}
    return {"texts": engine._send_report(force=True)}


def start_engine():
    from app.core.bootstrap import ensure_plans
    ensure_plans(engine)
    threading.Thread(target=engine.run_forever, daemon=True, name="engine").start()
