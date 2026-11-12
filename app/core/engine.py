"""Plans (user mock/real + league players), the tick loop, league rounds, persistence."""
import functools
import json
import math
import subprocess
import traceback
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import numpy as np
from trader import config as C
from trader.toss import TossUnavailable
from .webhook import url as hook_url
from . import data, markets, news, pacing, profiles, universe
from .accounts import Book, MockExecutor, TossExecutor, RealState, Halt, OrderUnknown, risk_gate, audit, STANDBY
from .lease import LEASE
from .brain import Brain, PERSONAS, signals
from . import report, train
from .brain import is_extended, is_thin, RULE_WORDS
from .markets import CCY, FEES, SLIPPAGE, INVERSE, INVERSE_ALL, LEVERAGE, inverse_for, is_open, regular_close, session_close, \
    month_end, last_trading_day_of_month, regular_open_mins, NAMES
from . import alerts, tuning
from .alerts import plain
from zoneinfo import ZoneInfo
MARKET_TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}

STATE = C.STATE_DIR / "app"
STATE.mkdir(parents=True, exist_ok=True)
now_s = lambda: datetime.now().strftime("%m-%d %H:%M:%S")
ERRLOG = C.STATE_DIR / "logs" / "engine_errors.log"


def override_active(v, now=None):
    """REAL_READINESS_OVERRIDE must carry an end date (no override without one):
    "until:YYYY-MM-DD" is honoured through that day (KST), at most 14 days ahead. A bare "yes" no longer works."""
    v = (v or "").strip()
    if not v.startswith("until:"):
        return False
    try:
        end = datetime.strptime(v[6:].strip(), "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Seoul")) + timedelta(days=1)
    except ValueError:
        return False
    now = now or time.time()
    return now < end.timestamp() <= now + 15 * 86400


def log_error(where, e):
    try:   # logging must never be the thing that breaks trading
        ERRLOG.parent.mkdir(exist_ok=True)
        if ERRLOG.exists() and ERRLOG.stat().st_size > 2_000_000:
            ERRLOG.replace(ERRLOG.with_suffix(".log.1"))
        with open(ERRLOG, "a") as f:
            f.write(f"{datetime.now():%F %T} {where}: {e}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def notify(msg):
    """A macOS notification when possible, otherwise the alert webhook (if set)."""
    try:
        msg = plain(msg)
        subprocess.Popen(["osascript", "-e", f'display notification "{msg}" with title "AI Trader"'])
        return
    except Exception:
        pass
    trade_alert(msg if msg.startswith("AI Trader") else f"AI Trader: {msg}")


def trade_alert(msg):
    """Real-money buy/sell and engine notices → ALERT_WEBHOOK_URL (Slack, Discord, Google Chat or any webhook that
    takes JSON text). Sent from a background thread and never raises: an alert can't delay or break a trade.
    Without a webhook it is only printed."""
    def post():
        hook = hook_url()
        if not hook:
            return
        try:
            import requests
            t = plain(msg)[:1900]
            requests.post(hook, json={"text": t, "content": t}, timeout=10)
        except Exception as e:
            log_error("trade alert", e)
    print("ALERT:", plain(msg), flush=True)
    threading.Thread(target=post, daemon=True, name="trade-alert").start()


def plan_locked(fn):
    """Serialise everything that touches one plan: the AI's decision tick and your button presses
    can never trade the same plan at the same time."""
    @functools.wraps(fn)
    def w(self, plan_id, *a, **k):
        p = self.plans.get(plan_id)
        if p is None:
            raise ValueError("no such plan")
        with p.lock:
            return fn(self, plan_id, *a, **k)
    return w


class Plan:
    def __init__(self, mode, pid, market, risk, horizon, budget, min_pct=None, max_pct=None,
                 dry_run=True, state=None, league=False, universe="large", inverse=True, style="normal", monthly=False):
        s = state or {}
        self.id = s.get("id") or uuid.uuid4().hex[:8]
        self.mode, self.pid, self.market = mode, pid, market
        self.risk, self.horizon, self.budget = risk, horizon, budget
        self.min_pct, self.max_pct = min_pct, max_pct
        self.dry_run, self.league = dry_run, league
        self.universe = s.get("universe", universe)
        self.inverse = s.get("inverse", inverse)   # may buy inverse ETFs while the market is falling
        self.style = s.get("style", style)          # normal | swing (profiles.STYLES)
        self.tuning = s.get("tuning")               # real-money overrides (tuning.py), None for everything else
        self.reprofile()
        # monthly cycle (2026-10-27): trade until the month's last regular close, sell everything, start again
        self.monthly = s.get("monthly", monthly)
        self.renewed_to = s.get("renewed_to")      # id of the next cycle's plan once it started
        self.started = s.get("started", time.time())
        self.deadline = s.get("deadline", month_end(market, self.started) if self.monthly else self.started + self.profile.secs)
        self.status = s.get("status", "running")
        self.reason = s.get("reason", "")
        self.floor_armed = s.get("floor_armed", False)
        self.cooldown = s.get("cooldown", {})
        self.stops = s.get("stops", [])
        self.events = s.get("events", [])
        self.skips = s.get("skips", [])
        self.bench_start, self.bench_last = s.get("bench_start"), s.get("bench_last")   # index price at start / now         # buys the safety rules blocked (checked in the daily report)
        self.book = Book(market, budget, s.get("book"))
        done = self.status in ("done", "stopped", "halted")
        self.settled = s.get("settled", done and mode == "mock" and not league and not self.book.positions)
        self.pending = s.get("pending", [])
        self.adopted = s.get("adopted", 0.0)
        self.brain_snap = s.get("brain_snap")   # real plans trade on a frozen copy of the AI's settings   # cost of positions taken over from this investor's earlier plans   # real orders whose final state still needs confirming
        self.last_tick = 0
        self.busy = False
        self.executor = None
        self.lock = threading.RLock()

    def reprofile(self):
        self.profile = tuning.apply(profiles.make(self.risk, self.horizon, self.style), self.tuning)

    def to_dict(self):
        return dict(tuning=self.tuning, id=self.id, mode=self.mode, pid=self.pid, market=self.market, risk=self.risk,
                    horizon=self.horizon, budget=self.budget, min_pct=self.min_pct, max_pct=self.max_pct,
                    dry_run=self.dry_run, league=self.league, universe=self.universe, inverse=self.inverse, style=self.style, monthly=self.monthly, renewed_to=self.renewed_to, started=self.started, deadline=self.deadline,
                    status=self.status, reason=self.reason, floor_armed=self.floor_armed, settled=self.settled,
                    cooldown=self.cooldown, stops=self.stops[-20:], events=self.events[-200:], pending=self.pending, adopted=self.adopted, brain_snap=self.brain_snap,
                    skips=self.skips[-60:], bench_start=self.bench_start, bench_last=self.bench_last, book=self.book.to_dict())

    def log(self, msg, kind="info"):
        if self.events and self.events[-1]["msg"] == msg:
            return  # don't repeat an identical consecutive message
        if len(self.events) > 400:
            del self.events[:-300]
        if len(self.book.equity_log) > 4000:
            del self.book.equity_log[:-3000]
        self.events.append({"t": now_s(), "ts": round(time.time(), 3), "kind": kind, "msg": msg})

    def finish(self, status, reason):
        self.status, self.reason = status, reason
        self.log(f"{status.upper()}: {reason}", "status")
        if self.mode == "real":
            audit("plan_" + status, plan=self.id, reason=reason)


class Engine:
    def __init__(self):
        self.plans: dict[str, Plan] = {}
        self.brains: dict[str, Brain] = {}
        self.league_cfg = {"risk": 3, "horizon": "1d", "running": True, "round": 1,
                           "budget": {"US": 10000, "KR": 10_000_000}}
        self.league_history = []
        self.perf = {}    # pid → market → daily {d, net0, b0, net, b}: the league account vs its index, day by day
        self.mock_wallet = {"USD": 10000.0, "KRW": 10_000_000.0}
        self.mock_base = dict(self.mock_wallet)   # the fake money you set (P&L of the mock account is measured from it)
        self.prefs = {}   # per-investor Trade-panel settings (shared by every open window)
        self.train_date = ""   # last night-school date (train.py)
        self.alive = []        # [[from, to], …] when this engine was running (read by ops/failover.py)
        self._training = False
        self.pool = ThreadPoolExecutor(max_workers=8)     # trading decisions only
        self.bg = ThreadPoolExecutor(max_workers=2)       # slow background work (Claude coach)
        self.lock = threading.RLock()
        self._ledger_lock = threading.Lock()
        self._xfer_busy = False
        self.toss = data.toss()
        self.load()
        self.apply_pending_tuning()
        self.apply_rebase()
        self.slim_v112()

    def slim_v112(self):
        """One-time (v1.12): mock/paper trading is gone. Old mock plans (not the hidden league,
        which keeps teaching the traders and decides readiness) are moved to state/archive/ and dropped from the
        engine. Real plans and league plans are never touched."""
        if self.prefs.get("slim_v112"):
            return
        gone = {k: p for k, p in self.plans.items() if p.mode == "mock" and not p.league}
        if gone:
            arch = STATE.parent / "archive"
            arch.mkdir(exist_ok=True)
            f = arch / f"mock-plans-v112-{time.strftime('%Y%m%d-%H%M%S')}.json"
            f.write_text(json.dumps({k: p.to_dict() for k, p in gone.items()}, default=str))
            for k in gone:
                self.plans.pop(k, None)
        self.prefs["slim_v112"] = {"t": time.time(), "archived": len(gone),
                                   "running_stopped": [k for k, p in gone.items() if p.status in self.ACTIVE]}
        if gone: print(f"{len(gone)} old mock plans archived ({len(self.prefs['slim_v112']['running_stopped'])} were running)", flush=True)
        self.save()

    # ── persistence ─────────────────────────────────────────
    def save(self):
        with self.lock:
            for attempt in range(5):   # a plan may change mid-serialisation; just try again
                try:
                    self._mark_alive()
                    blob = json.dumps({"plans": {k: p.to_dict() for k, p in list(self.plans.items())},
                                       "brains": {k: b.to_dict() for k, b in self.brains.items()},
                                       "league_cfg": self.league_cfg, "league_history": self.league_history[-200:],
                                       "mock_wallet": self.mock_wallet, "mock_base": self.mock_base, "prefs": self.prefs, "perf": self.perf,
                                       "train_date": self.train_date, "alive": self.alive[-60:]},
                                      ensure_ascii=False, default=float)
                    break
                except RuntimeError:
                    time.sleep(0.05)
            else:
                return
            tmp = STATE / "engine.json.tmp"
            tmp.write_text(blob)
            tmp.replace(STATE / "engine.json")

    def _mark_alive(self):
        now = time.time()
        if self.alive and now - self.alive[-1][1] < 120:
            self.alive[-1][1] = now
        else:
            self.alive.append([now, now])

    def load(self):
        f = STATE / "engine.json"
        blob = json.loads(f.read_text()) if f.exists() else {}
        for pid in PERSONAS:
            self.brains[pid] = Brain(pid, blob.get("brains", {}).get(pid))
        self.league_cfg.update(blob.get("league_cfg", {}))
        self.league_history = blob.get("league_history", [])
        self.mock_wallet = blob.get("mock_wallet", self.mock_wallet)
        self.mock_base = blob.get("mock_base", self.mock_base)
        self.train_date = blob.get("train_date", "")
        self.alive = blob.get("alive", [])
        self.prefs = blob.get("prefs", {})
        self.perf = blob.get("perf", {})
        for k, d in blob.get("plans", {}).items():
            p = Plan(d["mode"], d["pid"], d["market"], d["risk"], d["horizon"], d["budget"],
                     d.get("min_pct"), d.get("max_pct"), d.get("dry_run", True), state=d, league=d.get("league"),
                     universe=d.get("universe", "large"), inverse=d.get("inverse", True))
            if p.mode == "real" and not p.dry_run and p.status in ("running", "waiting", "closing") \
                    and not (RealState.armed and RealState.env_unlocked()):
                p.status, p.reason = "paused", "app restarted — re-arm to resume (reconciles with Toss first)"
                notify(f"AI Trader restarted — real-money plan ({PERSONAS[p.pid]['name']}) is paused until you re-arm.")
            self.attach_executor(p)
            self.plans[k] = p
        if not any(p.league for p in list(self.plans.values())):
            self.start_league_round()

    def attach_executor(self, p):
        p.executor = TossExecutor(self.toss, dry_run=p.dry_run) if p.mode == "real" else MockExecutor()
        p.book.on_cash = self._ledger_move if p.mode == "real" and not p.dry_run and p.market == "US" else None

    # ── user plans ──────────────────────────────────────────
    def active_plan(self, pid, mode, exclude=None):
        """The investor's current plan in this mode — each AI trader runs at most one per mode."""
        act = [p for p in list(self.plans.values()) if not p.league and p.pid == pid and p.mode == mode
               and p is not exclude and p.status in self.ACTIVE]
        return max(act, key=lambda p: p.started) if act else None

    BENCH = {"US": "SPY", "KR": "069500"}

    def regime(self, market) -> dict:
        """How the whole market is behaving — the investors read this before every decision."""
        c = getattr(self, "_regime", {})
        if market in c and time.time() - c[market]["t"] < 600:
            return c[market]
        out = {"label": "choppy", "volatile": False, "t": time.time()}
        try:
            px = data.bars([self.BENCH[market]], "1d", ttl=600)[self.BENCH[market]]["Close"].astype(float)
            ma50, last = px.tail(50).mean(), float(px.iloc[-1])
            r20 = last / float(px.iloc[-21]) - 1
            vol20 = float(px.pct_change().tail(20).std()); vol_med = float(px.pct_change().rolling(20).std().tail(250).median())
            out["label"] = "bull" if last > ma50 and r20 > 0 else "bear" if last < ma50 and r20 < 0 else "choppy"
            out["volatile"] = vol20 > 1.5 * vol_med
            if market == "US":
                vix = next((m["price"] for m in data.monitor() if m["name"] == "VIX"), None)
                out["volatile"] = out["volatile"] or (vix is not None and vix > 25)
                out["vix"] = vix
            out.update(ret20=round(r20, 4), vs_ma50=round(last / ma50 - 1, 4))
        except Exception as e:
            log_error("regime", e)
        c[market] = out
        self._regime = c
        return out

    def intraday(self, market):
        """Index move over the last ~2 hours (5-minute bars), cached 5 min."""
        c = getattr(self, "_intra", {})
        if market in c and time.time() - c[market][0] < 300:
            return c[market][1]
        val = None
        try:
            px = data.bars([self.BENCH[market]], "5m", ttl=300)[self.BENCH[market]]["Close"].astype(float).tail(25)
            if len(px) >= 10:
                val = float(px.iloc[-1] / px.iloc[0] - 1)
        except Exception as e:
            log_error("intraday", e)
        c[market] = (time.time(), val); self._intra = c
        return val

    def symbols(self, p):
        if p.style == "swing":   # respected daily movers; for down days plain inverse ETFs only (no 2×/3× leverage)
            syms = universe.swing_symbols(p.market)
            return syms + [s for s in INVERSE[p.market] if s not in syms and s not in LEVERAGE] if p.inverse else syms
        syms = universe.symbols(p.market, p.risk, p.universe)
        if (p.tuning or {}).get("one_x_only"):   # 1× inverse only; no leveraged funds of either direction
            return syms + [s for s in INVERSE[p.market] if s not in syms and s not in LEVERAGE] if p.inverse else syms
        return syms + [s for s in inverse_for(p.market, p.risk) if s not in syms] if p.inverse else syms

    def require_ready(self, pid, market):
        """No NEW real money for an investor until it has met the app's own bar in that market (2026-10-17):
        PROVE_DAYS days and PROVE_TRADES closed trades in the league, beating the index after fees by its risk
        margin. Plans already running are untouched. Override (you take the risk): REAL_READINESS_OVERRIDE=until:YYYY-MM-DD in .env (max 14 days)."""
        from dotenv import dotenv_values
        if override_active(dotenv_values(C.ROOT / ".env").get("REAL_READINESS_OVERRIDE")):
            return
        rd = self.readiness(pid).get(market) or {}
        if not rd.get("ready"):
            raise ValueError(f"{PERSONAS[pid]['name']} isn't proven for real money in {market} yet — "
                             f"{rd.get('why') or 'no track record yet'} (needs {self.PROVE_DAYS} days, "
                             f"{self.PROVE_TRADES} trades and to beat the index after fees). Try a dry-run meanwhile.")

    def start_plan(self, mode, pid, market, risk, horizon, budget, min_pct, max_pct, dry_run=True, uni="large", inverse=True,
                   style="normal", monthly=False, renewal=False):
        """renewal=True: the next cycle of a monthly plan (deadline = end of next month) — the same trader carrying on,
        so the readiness bar (which gates NEW real money) is not asked again; arming, caps and Toss buying power still are.
        Monthly plans renew every month with no limit until you pause or stop them."""
        ccy = CCY[market]
        if pid not in PERSONAS:
            raise ValueError("unknown player")
        if min_pct is not None and max_pct is not None and min_pct >= max_pct:
            raise ValueError("min % must be below max %")
        with self.lock:
            if self.active_plan(pid, mode):
                raise ValueError(f"{PERSONAS[pid]['name']} already has a {mode} plan — update its settings instead")
            if mode == "mock":
                if budget > self.mock_wallet[ccy]:
                    raise ValueError(f"budget exceeds mock wallet ({self.mock_wallet[ccy]:,.2f} {ccy})")
                self.mock_wallet[ccy] -= budget
            else:
                if not dry_run and not RealState.env_unlocked():
                    raise ValueError("real trading locked: add REAL_TRADING_CONFIRMED=yes to .env")
                if not dry_run and not RealState.armed:
                    raise ValueError("arm real trading first")
                if not dry_run and not renewal:   # a monthly renewal carries on without the readiness gate
                    self.require_ready(pid, market)  # auto-renews every month, no expiry, until you pause it
                if not dry_run:   # caps guard real money; a dry-run budget is pretend
                    self.check_real_cap(ccy, budget)
                    bp = TossExecutor(self.toss).buying_power(ccy)
                    if budget > bp:
                        raise ValueError(f"budget exceeds Toss buying power ({bp:,.2f} {ccy})")
            p = Plan(mode, pid, market, risk, horizon, budget, min_pct, max_pct, dry_run, universe=uni, inverse=inverse,
                     style=style if style in profiles.STYLES else "normal", monthly=monthly)
            if monthly and renewal:   # renewed after the month's last close: the cycle runs to the end of NEXT month
                p.deadline = month_end(market, time.time() + 4 * 86400)
            self.attach_executor(p)
            p.log(f"Started {PERSONAS[pid]['name']} · {p.profile.risk_name} risk · {p.profile.horizon_label} · "
                  f"{budget:,.2f} {ccy} · {universe.MODES[uni]}" + (" · inverse & 2×/3× ETFs allowed" if inverse else "")
                  + (" · DRY-RUN" if mode == "real" and dry_run else "")
                  + (f" · MONTHLY cycle to {datetime.fromtimestamp(p.deadline):%m-%d %H:%M} KST (then sells all and starts again)"
                     if monthly else ""), "status")
            if mode == "real":
                audit("plan_start", plan=p.id, renewal=renewal, **{k: v for k, v in p.to_dict().items() if k in
                      ("pid", "market", "risk", "horizon", "budget", "min_pct", "max_pct", "dry_run", "inverse", "style",
                       "monthly", "deadline")})
            self.plans[p.id] = p
        self.save()
        return p

    @plan_locked
    def stop_plan(self, plan_id, flatten=True):
        p = self.plans[plan_id]
        if flatten and p.book.positions:
            if is_open(p.market) and self.close_all(p, "stopped by you"):
                pass
            else:
                p.log("not everything could be sold now — will keep selling (at next open if closed)", "warn")
                p.status, p.reason = "closing", "stopped — selling"
                self.save(); return p
        p.finish("stopped", "stopped by you")
        self.settle(p)
        self.save()
        return p

    ACTIVE = ("running", "waiting", "closing", "paused")

    def check_real_cap(self, ccy, budget, exclude=None):
        """Each investor ≤ the per-investor cap; all active real-money plans together ≤ the total cap."""
        per = RealState.caps()["plan"][ccy]
        if budget > per + 1e-9:
            raise ValueError(f"max {per:,.0f} {ccy} per investor (MAX_PLAN_{ccy} in .env)")
        cap = RealState.caps()["budget"][ccy]
        used = sum(p.budget - p.adopted for p in list(self.plans.values()) if p.mode == "real" and not p.dry_run and p is not exclude
                   and p.status in self.ACTIVE and CCY[p.market] == ccy)
        if used + budget > cap:
            raise ValueError(f"real-money plans would total {used + budget:,.2f} {ccy}, over the cap "
                             f"{cap:,.0f} {ccy} (MAX_BUDGET_{ccy} in .env)")

    @plan_locked
    def update_plan(self, plan_id, risk=None, horizon=None, uni=None, budget=None,
                    min_pct="keep", max_pct="keep", market=None, dry_run=None, inverse=None, style=None,
                    monthly=None):  # noqa: C901
        """Change a trader's settings while it runs. Holdings are kept; new settings apply from its next decision."""
        with self.lock:
            p = self.plans[plan_id]
            if p.league:
                raise ValueError("set league investors from the AI league tab")
            ccy, changes = CCY[p.market], []
            new_min = p.min_pct if min_pct == "keep" else min_pct
            new_max = p.max_pct if max_pct == "keep" else max_pct
            if new_min is not None and new_max is not None and new_min >= new_max:
                raise ValueError("min % must be below max %")
            if dry_run is not None and p.mode == "real" and dry_run != p.dry_run:
                # dry-run positions are pretend; they can't become real holdings (and vice versa)
                if p.book.positions and not p.dry_run:
                    raise ValueError("this plan holds real shares — Stop & sell it first, then start a dry-run")
                if not dry_run:   # all checks first, so a refused switch changes nothing
                    if not (RealState.env_unlocked() and RealState.armed):
                        raise ValueError("arm real trading first (type REAL MONEY → Arm)")
                    nb = budget if budget is not None else p.budget
                    self.require_ready(p.pid, market or p.market)
                    self.check_real_cap(CCY[market or p.market], nb, exclude=p)
                    bp = TossExecutor(self.toss).buying_power(CCY[market or p.market])
                    if nb > bp:
                        raise ValueError(f"not enough Toss cash ({bp:,.2f})")
                if p.book.positions:   # dry-run → real: pretend positions are just discarded
                    p.log("cleared pretend positions: " + ", ".join(p.book.positions), "status")
                    p.book.positions = {}
                if not dry_run:
                    p.budget = p.book.cash = nb
                    budget = None
                    self._combined_limit_add(p, nb, bp)
                else:
                    p.book.cash = p.budget
                p.dry_run = dry_run
                self.attach_executor(p)
                p.book.equity_log = []
                changes.append("⚠️ REAL MONEY ON" if not dry_run else "switched to DRY-RUN (no orders)")
            if market and market != p.market:
                if p.book.positions:
                    raise ValueError("can't switch market while holding stocks — pause & let them sell, or Stop & sell first")
                new_ccy, new_budget = CCY[market], budget if budget is not None else None
                if new_budget is None:
                    raise ValueError(f"set a budget in {new_ccy} to switch to {market}")
                if p.mode == "mock" and not p.settled:
                    if new_budget > self.mock_wallet[new_ccy] + 1e-9:
                        raise ValueError(f"not enough in the mock wallet ({self.mock_wallet[new_ccy]:,.2f} {new_ccy})")
                    self.mock_wallet[ccy] += p.book.cash          # old currency back to the wallet
                    self.mock_wallet[new_ccy] -= new_budget
                if p.mode == "real" and not p.dry_run:
                    self.require_ready(p.pid, market)
                    self.check_real_cap(new_ccy, new_budget, exclude=p)
                p.market = p.book.market = market
                p.book.cash, p.budget, ccy = new_budget, new_budget, new_ccy
                p.cooldown, p.stops, p.floor_armed, p.adopted = {}, [], False, 0.0
                p.started, p.deadline = time.time(), time.time() + p.profile.secs
                changes.append(f"market {market} · budget {new_budget:,.2f} {new_ccy}")
                budget = None
            if budget is not None and abs(budget - p.budget) > 1e-9:
                delta = budget - p.budget
                if delta < 0 and -delta > p.book.cash + 1e-9:
                    raise ValueError(f"can only reduce by free cash ({p.book.cash:,.2f} {ccy}); the rest is in stocks")
                if p.mode == "mock" and not p.settled:
                    if delta > self.mock_wallet[ccy] + 1e-9:
                        raise ValueError(f"not enough in the mock wallet ({self.mock_wallet[ccy]:,.2f} {ccy})")
                    self.mock_wallet[ccy] -= delta
                if p.mode == "real" and not p.dry_run and delta > 0:
                    self.require_ready(p.pid, p.market)   # adding real money counts as new money
                    self.check_real_cap(ccy, budget - p.adopted, exclude=p)
                    bp = TossExecutor(self.toss).buying_power(ccy)
                    if delta > bp:
                        raise ValueError(f"not enough Toss buying power ({bp:,.2f} {ccy})")
                p.book.cash += delta
                p.budget = budget
                changes.append(f"budget {budget:,.2f} {ccy} ({'+' if delta > 0 else ''}{delta:,.2f})")
            if (risk and risk != p.risk) or (horizon and horizon != p.horizon):
                p.risk, p.horizon = risk or p.risk, horizon or p.horizon
                p.reprofile()
                p.started = time.time()
                p.deadline = month_end(p.market) if p.monthly else p.started + p.profile.secs
                changes.append(f"{p.profile.risk_name} risk · {p.profile.horizon_label}"
                               + ("" if p.monthly else " (new window from now)"))
            if monthly is not None and monthly != p.monthly:
                p.monthly = monthly
                if monthly:   # same holdings; the window now ends at this month's end, then it renews itself
                    p.deadline = month_end(p.market)
                    changes.append(f"MONTHLY cycle: trades to {datetime.fromtimestamp(p.deadline):%m-%d %H:%M} KST at the latest "
                                   "(sells all before the month's last close), then starts again with the same settings")
                else:
                    p.started, p.deadline = time.time(), time.time() + p.profile.secs
                    changes.append(f"monthly cycle off (new {p.profile.horizon_label} window from now)")
            if style and style != p.style:
                if style not in profiles.STYLES:
                    raise ValueError("unknown trading style")
                p.style = style
                p.reprofile()   # same window, new trading rhythm
                changes.append(f"style {profiles.STYLES[style]}")
            if uni and uni != p.universe:
                p.universe = uni
                changes.append(universe.MODES[uni])
            if inverse is not None and inverse != p.inverse:
                p.inverse = inverse
                changes.append("inverse & 2×/3× ETFs allowed" if inverse else "inverse & 2×/3× ETFs off")
            if new_min != p.min_pct or new_max != p.max_pct:
                p.min_pct, p.max_pct = new_min, new_max
                p.floor_armed = False
                changes.append(f"profit band {new_min if new_min is not None else '–'}% … {new_max if new_max is not None else '–'}%")
            if not changes:
                return p
            p.last_tick = 0
            p.log("SETTINGS changed: " + " · ".join(changes), "status")
            if p.mode == "real":
                audit("plan_update", plan=p.id, changes=changes)
        self.save()
        return p

    @plan_locked
    def extend_plan(self, plan_id):
        """Add one more window of the plan's own horizon (from its end, or from now if it already ended).
        A plan that is only "closing" because its horizon ran out goes back to trading; holdings are kept."""
        with self.lock:
            p = self.plans[plan_id]
            if p.league:
                raise ValueError("league players restart automatically")
            if p.monthly:
                raise ValueError("a monthly-cycle plan runs to the month's end and restarts itself — nothing to extend")
            if p.status not in ("running", "waiting", "closing", "paused"):
                raise ValueError("plan has finished — use Resume")
            revive = p.status == "closing" and (p.reason or "").startswith("horizon ended")
            if p.status == "closing" and not revive:
                raise ValueError(f"plan is selling because: {p.reason} — it can't be extended")
            if revive and p.mode == "real" and not p.dry_run and not (RealState.env_unlocked() and RealState.armed):
                raise ValueError("arm real trading first")
            now = time.time()
            p.deadline = max(p.deadline, now) + p.profile.secs
            if revive:
                p.status, p.reason = "running", ""
            p.last_tick = 0
            end = datetime.fromtimestamp(p.deadline).strftime("%m-%d %H:%M")
            p.log(f"EXTENDED by {p.profile.horizon_label} — now ends {end}" + (" (back to trading)" if revive else ""), "status")
            if p.mode == "real":
                audit("plan_extend", plan=p.id, deadline=p.deadline, revived=revive)
        self.save()
        return p

    @plan_locked
    def pause_plan(self, plan_id):
        """Stop trading but keep holdings exactly as they are (no buys, no sells, no stops)."""
        with self.lock:
            p = self.plans[plan_id]
            if p.status not in ("running", "waiting", "closing"):
                raise ValueError("only an active plan can be paused")
            p.status, p.reason = "paused", "paused by you — holdings kept, no trading"
            p.log("PAUSED by you — holdings kept as-is; no buying, selling, stop-loss or profit band until resumed", "status")
            if p.mode == "real":
                audit("plan_pause", plan=p.id)
        self.save()
        return p

    @plan_locked
    def resume_plan(self, plan_id):
        """Continue a stopped/halted/finished plan with its own money, positions and settings."""
        with self.lock:
            p = self.plans[plan_id]
            if p.league and p.status != "paused":
                raise ValueError("league players restart automatically")
            if p.status == "merged":
                raise ValueError("this plan was merged into the investor's running plan — nothing to resume")
            if p.status in self.ACTIVE and p.status != "paused":
                raise ValueError("plan is already active")
            other = None if p.league else self.active_plan(p.pid, p.mode, exclude=p)
            if other:
                raise ValueError(f"{PERSONAS[p.pid]['name']} already has an active {p.mode} plan — "
                                 "stop it first or update its settings")
            if p.reason == "KILL SWITCH" and not RealState.armed:
                raise ValueError("kill switch was used — re-arm real trading first")
            ccy = CCY[p.market]
            if p.mode == "real" and not p.dry_run:
                if not (RealState.env_unlocked() and RealState.armed):
                    raise ValueError("arm real trading first")
                if p.status != "paused":   # a paused plan (app restart, pause) just carries on; a finished one is new money
                    self.require_ready(p.pid, p.market)
                self.check_real_cap(ccy, p.budget - p.adopted, exclude=p)
            if p.mode == "mock" and p.settled:  # its cash went back to the wallet when it stopped
                if p.book.cash > self.mock_wallet[ccy] + 1e-6:
                    raise ValueError(f"not enough in the mock wallet ({self.mock_wallet[ccy]:,.2f} {ccy})")
                self.mock_wallet[ccy] -= p.book.cash
                p.settled = False
            note = ""
            if p.reason.startswith("max loss"):  # fresh loss limit measured from today's value
                p.budget = p.book.net(data.quotes_prices(list(p.book.positions))) if p.book.positions else p.book.cash
                note = f" · loss limit reset from {p.budget:,.2f}"
            if time.time() >= p.deadline:
                p.started = time.time()
                p.deadline = month_end(p.market) if p.monthly else p.started + p.profile.secs
                note += " · runs to the month's end" if p.monthly else f" · new {p.profile.horizon_label} window"
            if p.mode == "real" and not p.dry_run:
                from trader.toss import auth_block
                blk = auth_block()
                if blk:
                    raise ValueError(f"Toss refuses this machine's login: {blk['msg']} (public IP {blk['ip'] or '?'}). "
                                     "Add this IP in Toss WTS → 설정 → Open API.")
                self.reconcile(p)
            p.status, p.reason, p.stops, p.last_tick = "running", "", [], 0
            p.log(f"RESUMED by you{note}", "status")
            if p.mode == "real":
                audit("plan_resume", plan=p.id, dry_run=p.dry_run)
        self.save()
        return p

    @plan_locked
    def remove_plan(self, plan_id):
        p = self.plans[plan_id]
        if p.league or p.status in self.ACTIVE:
            raise ValueError("stop the plan before removing it")
        if p.book.positions:
            raise ValueError("plan still holds positions")
        self.settle(p)
        del self.plans[plan_id]
        self.save()

    def settle(self, p):
        """Return a finished mock plan's money to the wallet."""
        with self.lock:
            if p.mode == "mock" and not p.league and not p.book.positions and not p.settled:
                self.mock_wallet[CCY[p.market]] += p.book.cash
                p.settled = True

    # ── the decision tick ───────────────────────────────────
    def tick(self, p: Plan):
        try:
            with p.lock:
                self._tick(p)
            p._toss_down = None
        except OrderUnknown as e:
            p.pending.append(e.order)
            p.finish("halted", f"{e} — will be confirmed with Toss before anything else")
            notify(f"{PERSONAS[p.pid]['name']}: a real order needs confirming — plan halted for safety.")
        except Halt as e:
            p.finish("halted", str(e))
            self.settle(p)
        except TossUnavailable as e:
            # Toss unreachable (network blip, DNS hiccup, Toss outage): nothing was sent, so skip this tick instead
            # of halting. Only when it stays unreachable for 30 min does a real plan halt fail-closed — its
            # positions can't be managed blind for longer than that.
            down = p.__dict__.get("_toss_down") or time.time()
            p._toss_down = down
            if p.mode == "real" and not p.dry_run and time.time() - down > 1800:
                p.finish("halted", f"Toss unreachable for 30 min (fail-closed): {e}")
                notify(f"{PERSONAS[p.pid]['name']} (real money) halted: Toss unreachable for 30 min")
            else:
                p.log(f"Toss unreachable — skipping this tick, will retry ({str(e)[:90]})", "warn")
        except Exception as e:
            if p.mode == "real" and not p.dry_run:   # halt first, then log
                p.finish("halted", f"unexpected error (fail-closed): {e}")
                notify(f"{PERSONAS[p.pid]['name']} (real money) halted: {str(e)[:80]}")
            else:
                p.log(f"error: {e}", "warn")
            log_error(f"tick {p.id}", e)
        finally:
            p.busy = False
            p.last_tick = time.time()

    @staticmethod
    def stop_window(p):
        """StoplossGuard look-back: 3 stop-losses inside it pause new buys. A quarter of the horizon, but 2 hours for
        swing — its stops come minutes apart, and a 1-week swing plan would otherwise sit out ~2 days (2026-10-28)."""
        return (p.tuning or {}).get("stop_window") or (7200 if p.style == "swing" else p.profile.secs / 4)

    def _tick(self, p: Plan):
        if p.status not in ("running", "waiting", "closing"):
            return
        self.adopt_orphans(p)
        prof, brain = p.profile, self.brains[p.pid]
        t_ = p.tuning or {}
        if p.mode == "real" and not p.dry_run:
            self.combined_limit_check()
            tripped = (self.prefs.get("real_limits") or {}).get("tripped")
            if tripped and p.status in ("running", "waiting"):
                p.status, p.reason = "closing", tripped
                p.log(tripped + " — selling everything, stays stopped until you restart it", "warn")
        if p.mode == "real" and not p.dry_run:   # league/mock learning must not change a live real-money strategy
            if not p.brain_snap:
                p.brain_snap = {"w": dict(brain.w), "entry": brain.entry, "tp_mult": brain.tp_mult, "sl_mult": brain.sl_mult}
            live = self.brains[p.pid]
            if live.entry > p.brain_snap["entry"]:       # learn caution immediately, boldness only at a new plan
                p.brain_snap["entry"] = live.entry
                p.log(f"learned to be pickier: buy threshold raised to {live.entry:.2f}", "status")
            if live.sl_mult < p.brain_snap["sl_mult"]:   # tighter risk control also applies at once
                p.brain_snap["sl_mult"] = live.sl_mult
            cap_e = PERSONAS[p.pid]["entry"] * 1.25        # the pickiness cap applies to the frozen copy too
            if p.brain_snap["entry"] > cap_e + 1e-9:
                p.log(f"buy threshold capped at {cap_e:.2f} (was {p.brain_snap['entry']:.2f}) — too picky to use its budget", "status")
                p.brain_snap["entry"] = cap_e
            from .brain import Brain
            brain = Brain(p.pid, dict(p.brain_snap, lessons=[], history=[]))
            brain.sym, brain.streak = live.sym, live.streak   # current stock memory + losing-streak caution
            brain.memory, brain.hours, brain.rules = live.memory, live.hours, live.rules   # recall + learned rules
        uni = self.symbols(p)
        held = list(p.book.positions)
        bench_sym = self.BENCH[p.market]
        q = data.quotes(set(uni) | set(held) | {bench_sym})
        reg = self.regime(p.market); R = reg["label"]
        if prof.secs <= 86400 or p.style == "swing":   # short-term traders read today's tape, not just the daily trend
            intra = self.intraday(p.market)
            if intra is not None:
                if intra < -0.004:
                    R = "bear"
                elif R == "bull" and abs(intra) < 0.002:
                    R = "choppy"   # daily trend is up, but today is going sideways
        p._regime, p._bench = R, (q.get(bench_sym) or {}).get("price")
        prices = {s: v["price"] for s, v in q.items()}

        if p.mode == "real" and not p.dry_run:
            self.reconcile(p)

        net = p.book.net(prices)
        ret = net / p.budget - 1
        p.book.equity_log.append({"t": time.time(), "net": round(net, 4), "b": p._bench})

        # plan-level exits: target band, max loss, deadline
        # session_open: Toss accepts orders now (decides waiting/running). market_open: also fresh quotes to act on —
        # a quiet stretch with no fresh quote just skips this tick instead of flipping the plan to "closed".
        session_open = is_open(p.market)
        market_open = session_open and any(v["age"] < 90 for s, v in q.items() if s in uni)
        if market_open and p._bench:
            if p.league:
                self.track_perf(p.pid, p.market, net, p._bench)
            p.bench_start = p.bench_start or p._bench
            p.bench_last = p._bench
        if p.status == "closing":
            if market_open and self.close_all(p, p.reason or "closing"):
                p.finish("halted" if p.reason.startswith(("max loss", "combined loss")) else "done", p.reason or "closed")
                self.settle(p)
            return   # still holding something: stay in "closing" and retry next tick
        if p.monthly:   # last trading day of the month: sell everything before the regular close (no weekend gap)
            rc = regular_close(p.market)
            if rc and last_trading_day_of_month(p.market):
                cut = rc - max(600, prof.tick + 300)   # its last tick before the cut still lands before the close
                if p.deadline > cut:
                    p.deadline = cut
                    p.log(f"last trading day of the month — sells everything by {datetime.fromtimestamp(cut):%H:%M} KST, "
                          "then the next monthly cycle starts", "status")
        # a monthly plan keeps trading after its goal / floor (goal pacing switches it to protect mode instead)
        if p.max_pct is not None and ret >= p.max_pct / 100 and not p.monthly:
            return self._end(p, market_open, f"target reached {ret:+.2%} ≥ max {p.max_pct}%", "done")
        if p.min_pct is not None and not p.monthly:
            buf = max(0.001, ((p.max_pct or p.min_pct * 2) - p.min_pct) / 100 * 0.2)
            if not p.floor_armed and ret >= p.min_pct / 100 + buf:
                p.floor_armed = True
                p.log(f"profit floor armed: will lock in ≥ {p.min_pct}% net", "status")
            if p.floor_armed and ret <= p.min_pct / 100:
                return self._end(p, market_open, f"profit floor hit — locked {ret:+.2%} net", "done")
        if ret <= -prof.max_loss:
            return self._end(p, market_open, f"max loss {ret:+.2%} (limit -{prof.max_loss:.0%})", "halted")
        if time.time() >= p.deadline:
            if p.monthly:
                return self._end(p, market_open, f"month-end: cycle over at {ret:+.2%} net", "done")
            return self._end(p, market_open, f"horizon ended at {ret:+.2%} net", "done")

        if not session_open:
            if p.status != "waiting":
                p.status = "waiting"; p.log(f"{p.market} market closed — waiting", "info")
            return
        if p.status == "waiting":
            p.status = "running"; p.log(f"{p.market} market open — trading", "info")
            if prof.secs <= 86400 and not p.monthly and not p.book.positions and not any(
                    t.get("side") == "BUY" for t in p.book.trades[-50:] if t.get("time", "") >= now_s()[:5]):
                p.started, p.deadline = time.time(), time.time() + prof.secs   # window starts at the open
        close = session_close(p.market)
        if prof.secs <= 86400 and not p.monthly and close and p.deadline > close - 300:
            p.deadline = close - 300   # a day-or-shorter plan finishes (sells) before today's close
            p.log(f"{prof.horizon_label} window shortened to today's close — will sell by "
                  f"{datetime.fromtimestamp(p.deadline):%H:%M} KST", "status")
        if not market_open:
            return   # session open but no fresh quotes yet — nothing safe to act on this tick

        bars = data.bars(set(uni) | set(held), prof.bar, ttl=max(30, prof.tick))
        sigs = {}
        for s in set(uni) | set(held):
            if s in bars:
                sg = signals(bars[s], prof, news.sentiment(s))
                if sg:
                    sigs[s] = sg

        if p.mode == "real" and not p.dry_run:   # a trader that can't see prices must never stay silent about it
            if sigs:
                p.__dict__.pop("_blind_t", None); p.__dict__.pop("_blind_told", None)
            elif time.time() - p.__dict__.setdefault("_blind_t", time.time()) > 600 and not p.__dict__.get("_blind_told"):
                p._blind_told = True
                p.log("no price data for 10 minutes: can't score any stock", "warn")
                trade_alert(f"{PERSONAS[p.pid]['name']}: no price data for 10 minutes, so it can't score stocks or buy. "
                            f"Feed: {data.FEED.get('last_error') or 'empty reply'}")
        mo = regular_open_mins(p.market)
        if mo is not None:
            for sg in sigs.values():
                sg["_mins_open"] = mo   # for the observe-only "first 15 min after the open" shadow rule
        p._sigs = sigs   # the sell alert reads the stock's score at the moment it sells
        # goal pacing: where it is vs where it should be by now on the way to its aim (see pacing.py)
        pc = None
        if (p.mode == "mock" or p.dry_run or self.PACING_REAL or p.monthly) and not t_.get("no_pacing"):   # monthly plans pace toward their goal
            win = max(p.deadline - p.started, 60)
            pc = pacing.pace(prof, p.min_pct, p.max_pct, ret, (time.time() - p.started) / win, R,
                             len([t for t in p.stops if time.time() - t < self.stop_window(p)]))
            if pc.mode != getattr(p, "_pace_mode", None):
                p.log("PACE: " + pc.note, "status")
                p._pace_mode = pc.mode
        p._pace = pc
        protect = bool(pc and pc.mode == "protect")
        trail_arm, trail_give = (0.3, 0.3) if protect else (0.6, 0.5)   # goal reached → lock gains in sooner

        close_t = regular_close(p.market)
        to_close = (close_t - time.time()) if close_t else 1e9
        flat_now = p.style == "swing" and to_close < 600     # swing: no overnight holdings
        tp, sl = prof.take_profit * brain.tp_mult, prof.stop_loss * brain.sl_mult
        holding = []   # what it thinks about each stock it holds (shown live in the app)
        now_t = time.time()
        bar_now = brain.entry + t_.get("entry_delta", 0) + {"bull": 0.0, "choppy": 0.04, "bear": 0.08}[R]
        # ── position exits
        for s, pos in list(p.book.positions.items()):
            if s not in prices:
                continue
            leg = pos.get("legacy_exit")   # bought before v1.08: exits exactly as it would have under its original settings
            ep = profiles.make(leg["risk"], leg["horizon"], p.style) if leg else prof
            tp, sl = (ep.take_profit * brain.tp_mult * 1.3, ep.stop_loss * brain.sl_mult * 1.3) \
                if leg or universe.is_small(s) else (ep.take_profit * brain.tp_mult, ep.stop_loss * brain.sl_mult)   # legacy (CPRI): its small-cap width is fixed, the daily list can drop it
            window = max(p.deadline - p.started, 60)
            if leg:
                window = max(p.deadline - leg.get("started", p.started), 60)   # the time box it was bought under
            if window < ep.secs * 0.9:                  # shortened window → aim for what's realistic in it
                k = max(0.35, (window / ep.secs) ** 0.5)
                tp, sl = tp * k, sl * k
            rt = FEES[p.market]["buy"] + FEES[p.market]["sell"] + FEES[p.market]["sell_tax"] + 2 * SLIPPAGE
            tp, sl = max(tp, 2.5 * rt), max(sl, 2.5 * rt)   # never so tight that fees alone trigger it
            if reg.get("volatile"):
                sl *= 1.2   # wider stop in a jumpy market so normal swings don't shake it out
            age = (time.time() - pos.get("t_open", p.started)) / window
            if age > 0.5:   # time-decaying target (Freqtrade's "minimal ROI"): late in the window, take less
                tp = max(tp * max(0.6, 1.4 - 0.8 * age), 2.5 * rt)
            if t_.get("tp") and not leg:
                tp, sl = t_["tp"], t_["sl"]   # fixed targets for this plan
            r = p.book.pos_net_ret(s, prices[s])
            pos["peak_ret"] = max(pos.get("peak_ret", 0), r)
            holding.append({"symbol": s, "ret": round(r, 4), "tp": round(tp, 4), "sl": round(sl, 4),
                            "trailing": pos["peak_ret"] >= tp * trail_arm, "weak": pos.get("bad", 0) > 0})
            why = None
            if flat_now:
                why = f"swing: flat before the close ({r:+.2%})"
            elif s in INVERSE_ALL and R == "bull" and not t_.get("market_call"):
                why = f"market turned up — closing inverse ETF at {r:+.2%}"
            elif s in LEVERAGE and s in INVERSE_ALL and time.time() - pos.get("t_open", p.started) > 86400:
                why = f"{LEVERAGE[s]}× inverse held a day — closing before daily-reset decay ({r:+.2%})"
            elif r >= tp:
                why = f"take profit {r:+.2%}"
            elif r <= -sl:
                why = f"stop loss {r:+.2%}"
            elif pos["peak_ret"] >= tp * trail_arm and r <= pos["peak_ret"] - sl * trail_give:
                why = f"trailing stop {r:+.2%} (peak {pos['peak_ret']:+.2%})"
            elif t_.get("eod_min") and not leg and to_close < 900 and r >= t_["eod_min"] \
                    and not (s in sigs and brain.score(sigs[s], R, s) >= bar_now + t_["eod_keep"]):
                why = f"end-of-day profit {r:+.2%}"
            elif leg:
                if now_t - pos.get("_leg_t", 0) >= 1800 and now_t - pos.get("t_open", 0) > max(3 * ep.tick, min(ep.secs * 0.05, 1800), 180):
                    pos["_leg_t"] = now_t   # its original 30-minute reading on daily bars
                    df = data.bars([s], ep.bar, ttl=1800).get(s)
                    sg = signals(df, ep, news.sentiment(s)) if df is not None else None
                    if sg:
                        scx = brain.score(sg, R, s)
                        bad = scx < -brain.entry or (r < -sl * 0.5 and scx < -brain.entry * 0.5)
                        pos["bad"] = pos.get("bad", 0) + 1 if bad else 0
                        if pos["bad"] >= 2:
                            why = f"signal turned ({scx:+.2f}) at {r:+.2%}"
            elif s in sigs and time.time() - pos.get("t_open", 0) > max(3 * prof.tick, min(prof.secs * 0.05, 1800), 180) \
                    and now_t - pos.get("_sig_t", 0) >= t_.get("sig_gap", 0):
                pos["_sig_t"] = now_t
                scx = brain.score(sigs[s], R, s)
                bad = scx < -brain.entry or (r < -sl * 0.5 and scx < -brain.entry * 0.5)
                pos["bad"] = pos.get("bad", 0) + 1 if bad else 0
                if pos["bad"] >= t_.get("sig_readings", 2) and (not t_.get("sig_loss_only") or r < 0):
                    why = f"signal turned ({scx:+.2f}) at {r:+.2%}"   # stays bad on repeated readings → not just noise
            if t_.get("eod_min") and not leg and not why and to_close < 900 and r >= t_["eod_min"] and pos.get("_kept") != now_s()[:5]:
                pos["_kept"] = now_s()[:5]
                p.log(f"keeping {s} overnight at {r:+.2%}: signal still strong (score {brain.score(sigs[s], R, s):+.2f}, bar {bar_now:.2f})", "status")
            if why and self.sell(p, s, pos["qty"], q.get(s), sigs.get(s, {}).get("_price"), why) and why.startswith("stop loss"):
                p.stops.append(time.time())   # counted only when the stop actually sold (a blocked attempt is not a loss)

        # ── entries
        learner = self.brains[p.pid]   # real plans decide on a frozen copy but still teach the shared brain
        rt_cost = FEES[p.market]["buy"] + FEES[p.market]["sell"] + FEES[p.market]["sell_tax"] + 2 * SLIPPAGE
        ranked = sorted(((brain.score(sg, R, s_), s_) for s_, sg in sigs.items() if s_ in uni), reverse=True)
        sample = [c for c in ranked[:3] + ranked[-2:] if c[1] in prices]
        learner.observe(time.time(), R, [(s_, prices[s_], sigs[s_], sc_) for sc_, s_ in dict.fromkeys(sample)],
                        rt_cost, max(prof.tick * 10, 900), p._bench, p.market)
        learner.evaluate(prices, time.time(), p._bench)
        window = max(p.deadline - p.started, 60)
        remaining = (p.deadline - time.time()) / window       # share of THIS plan's window that is left
        recent_stops = [t for t in p.stops if time.time() - t < self.stop_window(p)]
        side = None
        if t_.get("market_call") and is_open(p.market, regular=True):
            side = self.market_call(p, reg)
            wrong = [s for s, v in p.book.positions.items() if (s in INVERSE_ALL) != (side == "short") and not v.get("legacy_exit")]
            for s in wrong:
                self.sell(p, s, p.book.positions[s]["qty"], q.get(s), sigs.get(s, {}).get("_price"),
                          f"side switch: market call {'down' if side == 'short' else 'up'}")
            if any(s in p.book.positions for s in wrong):   # never hold both sides: buy nothing until the sells are done
                return self._publish_scan(p, R, reg, brain.entry, [], holding, len(sigs), 1.0, 0.0,
                                          "switching sides, waiting for the sell to go through")
        if t_.get("regular_only") and (to_close < t_["no_buy_last"] or not is_open(p.market, regular=True)):
            return self._publish_scan(p, R, reg, brain.entry, [], holding, len(sigs), 1.0, 0.0,
                                      "buys only in the regular session, not in its last 15 min")
        if p.style == "swing" and (to_close < 1800 or not is_open(p.market, regular=True)):
            return self._publish_scan(p, R, reg, brain.entry, [], holding, len(sigs), 1.0, 0.0,
                                      "swing: buys only in the regular session, not in its last 30 min — flat by the close")
        # no new buys in the last stretch / last 15 min. A monthly plan trades to the month's end: only a non-swing
        # trader stops buying on the month's last trading day (it sells everything at that close); swing is flat nightly.
        last_stretch = (p.style != "swing" and last_trading_day_of_month(p.market)) if p.monthly else remaining < 0.15
        if last_stretch or p.deadline - time.time() < 900:
            return self._publish_scan(p, R, reg, brain.entry, [], holding, len(sigs), 1.0, 0.0,
                                      "last stretch of the plan, no new buys, just managing what it holds")
        if len(recent_stops) >= 3:
            if not getattr(p, "_guard_logged", False):
                p.log("StoplossGuard: 3 stop-losses recently — pausing new entries", "warn")
                p._guard_logged = True
            return self._publish_scan(p, R, reg, brain.entry, [], holding, len(sigs), 1.0, 0.0,
                                      "3 stop-losses recently, pausing new buys")
        p._guard_logged = False
        invested = sum(v["cost"] for v in p.book.positions.values())
        room = p.budget * prof.invest_frac - invested
        entry = bar_now
        hour_adj = 0.0 if t_.get("no_hour_adj") else learner.hour_adj(p.market, learner.local_hour(p.market, time.time()))
        entry += hour_adj   # pickier in the hours of the day that have lost it money
        if pc:
            entry += pc.entry_adj   # pickier when ahead of its goal, a bit less picky when behind with time left
        p._entry, p._brain = entry, brain   # the brain that actually decides (a frozen copy for real plans)
        size = {"bull": 1.0, "choppy": 0.9, "bear": 0.75}[R] * brain.size_mult() * (0.9 if reg.get("volatile") else 1.0)
        if pc:
            size *= pc.size_mult    # smaller buys once ahead / goal reached (never bigger when behind)
        # US fractional shares (Toss supports them). Real-money fractional orders stay off until verified
        # on your account, so real plans use whole shares.
        frac_ok = p.market == "US" and (p.mode == "mock" or p.dry_run)
        cap = RealState.caps()["order"][CCY[p.market]] if p.mode == "real" else 1e18
        # Conviction sizing (2026-10-15): the budget is there to be used. When stocks clear the bar, the plan puts its
        # whole investable budget to work across the ones that qualify RIGHT NOW — one qualifier gets the biggest
        # position its risk level allows (Very High: everything), two split it, and so on up to max_positions — and
        # it buys them in the same tick instead of one every few minutes. The per-stock ceiling (by risk level) is
        # trimmed a little in choppy / bear / jumpy markets. A whole-share plan only considers a stock whose single
        # share fits under that ceiling.
        conc = p.budget * {1: 0.35, 2: 0.45, 3: 0.5, 4: 0.6, 5: 1.0}[p.risk] * size
        one_share_max = min(room, p.book.cash * 0.995, cap, conc)
        # Size as a share of the plan (2026-10-17): no single stock takes more than ORDER_SHARE of the plan's budget
        # (e.g. VYX was ~40% of Vector's). There is NO dollar cap: when one share costs more than that share, ONE
        # share is still allowed as long as it fits the per-stock ceiling above — so $100+ stocks stay tradable.
        share_cap = p.budget * t_.get("order_share", self.ORDER_SHARE[p.risk])
        scored = {s_: brain.score(sg, R, s_) for s_, sg in sigs.items() if s_ in uni and s_ not in p.book.positions}
        fits = lambda s_: bool(prices.get(s_)) and (frac_ok or prices[s_] * 1.004 <= one_share_max)
        cands = sorted(((sc_, s_) for s_, sc_ in scored.items() if fits(s_)), reverse=True)
        if room < (1 if p.market == "US" else 1000):   # the budget is at work: it just manages what it holds
            return self._publish_scan(p, R, reg, entry, [], holding, len(sigs), size, hour_adj,
                                      "fully invested, just managing what it holds")
        out_of_reach = sorted(((sc_, s_) for s_, sc_ in scored.items() if not fits(s_) and sc_ >= entry), reverse=True)
        scan, bought, n_bought, topped, spent, max_pos = [], False, 0, 0, 0.0, prof.max_positions
        investable = max(p.budget * prof.invest_frac, 1)
        if len(p.book.positions) >= prof.max_positions and t_:
            return self._publish_scan(p, R, reg, entry, [], holding, len(sigs), size, hour_adj, "all position slots taken")
        if len(p.book.positions) >= prof.max_positions:
            # every slot is taken but budget is still idle (raised mid-plan, or the positions were opened small):
            # first add to the holdings that still clear the bar, then let what is STILL idle open extra slots
            room, topped, spent, scan = self._top_up(p, R, entry, sigs, prices, q, prof, brain, learner, size, room,
                                                     min(conc, share_cap), cap, frac_ok)
            if room / investable < 0.10:
                return self._publish_scan(p, R, reg, entry, scan, holding, len(sigs), size, hour_adj,
                                          f"added to {topped} holding{'s' if topped > 1 else ''} that still clear my bar — "
                                          f"{spent:,.0f} {CCY[p.market]} of idle budget put to work" if topped
                                          else "fully invested, just managing what it holds")
            max_pos = len(p.book.positions) + max(1, math.floor(room / max(p.budget * prof.pos_pct * size, 1)))
            out_of_reach = [] if topped else out_of_reach
        # how many ways to split the budget: the affordable stocks that clear the bar this tick (capped by free slots)
        holds_long = any(s_ not in INVERSE_ALL for s_ in p.book.positions)
        is_short = side == "short"
        inv_ok = (lambda: is_short) if side else (lambda: R == "bear")
        inv_bar = (-9.0 if is_short else entry) if side else entry - 0.08   # on a down call the inverse fund IS the trade
        qualifying = [s_ for sc_, s_ in cands if sc_ >= (inv_bar if s_ in INVERSE_ALL else entry)
                      and not (s_ in INVERSE_ALL and (not inv_ok() or holds_long)) and not (is_short and s_ not in INVERSE_ALL)
                      and p.cooldown.get(s_, 0) <= time.time()]
        ways = max(1, min(len(qualifying), max_pos - len(p.book.positions)))
        for sc, s in cands:
            px = prices.get(s)
            inv = s in INVERSE_ALL
            if inv and not inv_ok():
                scan.append((s, sc, "inverse: market not falling")); continue
            if is_short and not inv:
                scan.append((s, sc, "long: market call down")); continue
            if inv and holds_long:
                # no self-hedging (2026-10-17): buying PSQ/SH while holding stocks just pays fees on both sides
                scan.append((s, sc, "inverse: holds stocks")); continue
            if sc < (inv_bar if inv else entry):
                scan.append((s, sc, "below entry")); continue
            if p.cooldown.get(s, 0) > time.time():
                scan.append((s, sc, "cooling off")); continue   # just sold it (longer after a stop-loss)
            dc = sigs[s].get("_day_chg")
            if is_extended(sigs[s], self.EXTENDED_UP, self.EXTENDED_NEAR_HIGH) and learner.rule_on("ext"):
                # already ran up today and sits at the high: these mostly faded (post-mortem 10-10)
                self.note_skip(p, s, px or sigs[s]["_price"], f"already {dc:+.1%} today, at the high", sc)
                scan.append((s, sc, "too extended")); continue
            if is_thin(sigs[s]) and learner.rule_on("thin"):
                self.note_skip(p, s, px or sigs[s]["_price"], f"rally on thin volume ({sigs[s]['_rvol']:.1f}× normal)", sc)
                scan.append((s, sc, "thin volume")); continue
            coach = [k for k, on in learner.shadow_block(sigs[s]) if on]   # coach lessons that have proven themselves
            if coach:
                self.note_skip(p, s, px or sigs[s]["_price"], "coach rule: " + RULE_WORDS[coach[0]], sc)
                scan.append((s, sc, "coach rule")); continue
            if p.mode == "real" and not p.dry_run:
                owner = next((o for o in list(self.plans.values()) if o is not p and o.mode == "real"
                              and not o.dry_run and s in o.book.positions), None)
                if owner:   # never stack two investors' real money on one stock
                    scan.append((s, sc, f"held by {PERSONAS[owner.pid]['name']}")); continue
            if len(p.book.positions) >= max_pos or room <= 0:
                scan.append((s, sc, "no room")); continue
            slots_left = max(1, max_pos - len(p.book.positions))
            slot = room if slots_left == 1 else max(p.budget * prof.pos_pct * size, room / min(slots_left, ways))
            alloc = min(slot * (1.2 if inv else 1), conc, share_cap, room, p.book.cash * 0.995, cap * 0.99 / 1.004)
            if inv and t_.get("inv_pct"):
                alloc = min(p.budget * t_["inv_pct"], room, p.book.cash * 0.995, cap * 0.99 / 1.004)
            qty = math.floor(alloc / (px * 1.004))
            if frac_ok and alloc >= 1:
                qty = math.floor(alloc / (px * 1.004) * 10000) / 10000   # e.g. 1.9512 shares
            elif qty < 1 and px * 1.004 <= one_share_max:
                qty = 1  # whole shares: one share when the slot is smaller than a share (within the per-stock ceiling)
            elif qty >= 1 and (qty + 1) * px * 1.004 <= min(alloc * 1.2, conc, share_cap, room, p.book.cash * 0.995, cap * 0.99):
                qty += 1  # whole shares: round up when one more share fits within 20% of the slot (and the plan share)
            if qty <= 0:
                scan.append((s, sc, "can't afford")); continue
            if self.buy(p, s, qty, q.get(s), sigs[s]["_price"], sigs[s], sc):
                room -= qty * px; bought = True; n_bought += 1; p._last_buy = time.time()
                scan.append((s, sc, "bought"))
                if room <= 0 or len(p.book.positions) >= max_pos:
                    break
        scan += [(s_, sc_, "can't afford") for sc_, s_ in out_of_reach[:2]]   # shown, so you can see what it passed on
        note = None
        if n_bought:
            note = f"going in: {n_bought} stock{'s' if n_bought > 1 else ''} cleared my bar, budget split {ways} way{'s' if ways > 1 else ''}"
        if topped:
            note = f"added to {topped} holding{'s' if topped > 1 else ''} that still clear my bar ({spent:,.0f} {CCY[p.market]})" + (f"; {note}" if note else "")
        self._publish_scan(p, R, reg, entry, scan, holding, len(sigs), size, hour_adj, note)
        if out_of_reach and not bought and time.time() - getattr(p, "_oor_t", 0) > 3600:
            p._oor_t = time.time()
            p.log(f"Out of my price range: {', '.join(s_ for _, s_ in out_of_reach[:4])} — a single share costs more than "
                  f"this plan can put into one stock ({one_share_max:,.0f} {CCY[p.market]}); looking for cheaper setups", "info")

    def _top_up(self, p, R, entry, sigs, prices, q, prof, brain, learner, size, room, conc, cap, frac_ok):
        """Every slot is taken but budget is idle: add to the holdings that STILL clear the buy bar right now, each up
        to one full slot (the budget is there to be used — 2026-10-16). A holding that no longer qualifies is left
        alone. Returns (room left, holdings added to, amount spent, scan items)."""
        investable = max(p.budget * prof.invest_frac, 1)
        target = max(p.budget * prof.pos_pct * size, investable / max(len(p.book.positions), 1))   # one slot's size
        scored = {s_: brain.score(sigs[s_], R, s_) for s_ in p.book.positions if s_ in sigs and prices.get(s_)}
        scan, n, spent = [], 0, 0.0
        for s, sc in sorted(scored.items(), key=lambda kv: -kv[1]):
            pos, px, inv = p.book.positions[s], prices[s], s in INVERSE_ALL
            if inv and (R != "bear" or any(x not in INVERSE_ALL for x in p.book.positions)):
                continue
            if sc < (entry - 0.08 if inv else entry) or pos.get("bad", 0):
                scan.append((s, sc, "add: below entry")); continue
            if is_extended(sigs[s], self.EXTENDED_UP, self.EXTENDED_NEAR_HIGH) and learner.rule_on("ext"):
                scan.append((s, sc, "too extended")); continue
            if is_thin(sigs[s]) and learner.rule_on("thin"):
                scan.append((s, sc, "thin volume")); continue
            if any(on for _, on in learner.shadow_block(sigs[s])):
                scan.append((s, sc, "coach rule")); continue
            alloc = min(target - pos["cost"], conc - pos["cost"], room, p.book.cash * 0.995, cap * 0.99 / 1.004)
            qty = math.floor(alloc / (px * 1.004) * 10000) / 10000 if frac_ok else math.floor(alloc / (px * 1.004))
            if qty <= 0 or qty * px < (1 if p.market == "US" else 1000):
                continue   # this holding is already a full slot
            if self.buy(p, s, qty, q.get(s), sigs[s]["_price"], sigs[s], sc):
                room -= qty * px; spent += qty * px; n += 1; p._last_buy = time.time()
                scan.append((s, sc, "add: bought"))
                if room <= 0:
                    break
        return room, n, spent, scan

    STATUS_WORDS = {"below entry": "below my bar", "pacing": "waiting a bit between buys", "cooling off": "just sold it, cooling off",
                    "too extended": "already ran up today", "thin volume": "rally on thin volume", "no room": "no room left in the plan",
                    "can't afford": "a share costs more than I can put in", "inverse: market not falling": "inverse, but the market isn't falling",
                    "inverse: holds stocks": "inverse, but it already holds stocks (no self-hedging)",
                    "long: market call down": "my market call is down, so no new longs",
                    "coach rule": "skipped by a coach rule that proved itself",
                    "bought": "bought it", "add: bought": "added to it", "add: below entry": "holding it, not adding until it clears my bar"}

    def _publish_scan(self, p, R, reg, entry, scan, holding, watching, size, hour_adj, note=None):
        """Store what the trader is looking at (p.scan, shown live on its card) and write its reasoning to the
        activity log whenever that reasoning changes — not every tick, so the log stays readable."""
        top = [{"symbol": s, "score": round(sc, 2), "status": st} for s, sc, st in scan[:4]]
        p.scan = {"t": time.time(), "entry": round(entry, 2), "watching": watching, "regime": R,
                  "volatile": bool(reg.get("volatile")), "size": round(size, 2), "hour_adj": round(hour_adj, 2),
                  "top": top, "holding": holding, "note": note,
                  "pace": p._pace.to_dict() if getattr(p, "_pace", None) else None}
        parts = []
        for h in holding:
            extra = " — trailing stop active" if h["trailing"] else " — signal weakening, watching it" if h["weak"] else ""
            parts.append(f"holding {h['symbol']} {h['ret']:+.1%} (sell at {h['tp']:+.0%} or {-h['sl']:+.0%}){extra}")
        if note:
            parts.append(note)
        else:
            parts.append(f"market {R}{' and jumpy' if reg.get('volatile') else ''}, my buy bar is {entry:.2f}")
            words = []
            for x in top[:3]:
                w = self.STATUS_WORDS.get(x["status"], x["status"])
                if x["status"] == "below entry" and x["score"] >= entry - 0.1:
                    w = "close to my bar, watching it"
                words.append(f"{x['symbol']} {x['score']:+.2f} ({w})")
            if words:
                parts.append("best now: " + ", ".join(words))
            if top and all(x["status"] == "below entry" for x in top):
                parts.append("nothing good enough yet")
            elif not top:
                parts.append("no stock has enough price data yet" if watching == 0 else "nothing to consider right now")
        msg = "Thinking: " + "; ".join(parts)
        # a new log line only when the thinking itself changes (market read, which stocks, why not, a holding moving
        # 1%+) — never for score wobble alone — at most every 5 min, plus a heartbeat every 30 min
        sig = (R, note, tuple(sorted((x["symbol"], x["status"], x["score"] >= entry - 0.1) for x in top[:3])),
               tuple((h["symbol"], round(h["ret"] * 100)) for h in holding))
        now = time.time()
        if sig != getattr(p, "_think_sig", None) and now - getattr(p, "_think_t", 0) >= max(p.profile.tick, 300) \
                or now - getattr(p, "_think_t", 0) >= 1800:
            p._think_sig, p._think_t = sig, now
            p.log(msg, "think")

    # ── competing with the index ─────────────────────────────
    BENCH_NAME = {"US": "S&P 500", "KR": "KOSPI 200"}
    PROVE_DAYS, PROVE_TRADES = 20, 40
    RISK_MARGIN = {1: 0.0, 2: 0.0, 3: 0.005, 4: 0.01, 5: 0.02}   # riskier settings must beat the index by more

    def track_perf(self, pid, market, net, bench, t=None):
        t = t or time.time()
        d = datetime.fromtimestamp(t, MARKET_TZ[market]).strftime("%Y-%m-%d")   # the market's own trading day
        rows = self.perf.setdefault(pid, {}).setdefault(market, [])
        if rows and rows[-1]["d"] == d:
            rows[-1].update(net=round(net, 4), b=bench)
        elif not rows or d > rows[-1]["d"]:
            rows.append({"d": d, "net0": round(net, 4), "b0": bench, "net": round(net, 4), "b": bench})
            del rows[:-260]

    def vs_index(self, pid, days=None):
        """League account return vs simply holding the index over the same days (last `days`, default 20)."""
        days = days or self.PROVE_DAYS
        out = {}
        for m, rows in self.perf.get(pid, {}).items():
            w = rows[-days:]
            if not w or not w[0]["net0"] or not w[0]["b0"]:
                continue
            r, b = w[-1]["net"] / w[0]["net0"] - 1, w[-1]["b"] / w[0]["b0"] - 1
            out[m] = {"ret": r, "index": b, "excess": r - b, "days": len(w), "name": self.BENCH_NAME[m]}
        return out

    def readiness(self, pid):
        """Ready for real money in a market = enough days and trades, and beat the index by the risk margin."""
        vs, pc = self.vs_index(pid), self.player_cfg(pid)
        margin = self.RISK_MARGIN.get(pc["risk"], 0.0)
        trades = sum(1 for p in self.plans.values() if p.league and p.pid == pid
                     for t in p.book.trades if t["side"] == "SELL")
        out = {}
        for m in ("US", "KR"):
            v = vs.get(m)
            if not v:
                out[m] = {"ready": False, "why": "no track record yet"}
                continue
            need = []
            if v["days"] < self.PROVE_DAYS:
                need.append(f"{v['days']}/{self.PROVE_DAYS} days")
            if trades < self.PROVE_TRADES:
                need.append(f"{trades}/{self.PROVE_TRADES} trades")
            if v["excess"] <= margin:
                need.append(f"{'trails' if v['excess'] < 0 else 'leads'} {v['name']} by {abs(v['excess']):.2%}"
                            + (f", needs +{margin:.1%}" if margin else ", needs to beat it"))
            out[m] = {"ready": not need, "why": "; ".join(need) or f"beat {v['name']} by {v['excess']:+.2%}",
                      "margin": margin, "excess": v["excess"], "days": v["days"], "trades": trades, "index": v["name"]}
        return out

    def backfill_perf(self):
        """One-off: rebuild the daily vs-index record from league equity logs written before it existed."""
        if self.perf:
            return
        for m, sym in self.BENCH.items():
            try:
                bars = data.bars([sym], "5m", ttl=3600).get(sym)
                if bars is None or not len(bars):
                    continue
                ts = np.array([x.timestamp() for x in bars.index]); px = bars["Close"].to_numpy(dtype=float)
                for p in [p for p in list(self.plans.values()) if p.league and p.market == m]:
                    for e in p.book.equity_log:
                        i = np.searchsorted(ts, e["t"]) - 1
                        if 0 <= i and e["t"] - ts[i] < 900 and is_open(m, datetime.fromtimestamp(e["t"]).astimezone(), regular=True):
                            self.track_perf(p.pid, m, e["net"], float(px[i]), e["t"])
            except Exception as e:
                log_error("backfill_perf", e)

    EXTENDED_UP, EXTENDED_NEAR_HIGH = 0.03, 0.01
    PACING_REAL = False   # goal pacing runs on mock / dry-run plans; real-money plans opt in after review
    ORDER_SHARE = {1: 0.20, 2: 0.20, 3: 0.25, 4: 0.30, 5: 0.35}   # most of a plan's budget one stock may take, by risk   # don't buy ≥3% up today AND within 1% of today's high

    def note_skip(self, p, s, px, why, score):
        """Remember buys the rules blocked, so the daily report can show whether skipping was right."""
        last = next((k for k in reversed(p.skips) if k["sym"] == s), None)
        if last and time.time() - last["ts"] < 3600:
            return
        p.skips.append({"ts": time.time(), "sym": s, "px": round(px, 4), "why": why, "score": round(score, 2)})
        del p.skips[:-60]

    def renew_monthly(self, p):
        """A monthly plan that sold out at month-end starts its next cycle with the same settings — once the market
        is closed (no buying back in the minutes after the sell-off). Retried every 10 min until it starts; you are
        told once if it can't (disarmed, kill switch, cap, not enough Toss cash)."""
        if not p.monthly or p.renewed_to or p.status != "done" or not (p.reason or "").startswith("month-end") \
                or p.league or p.book.positions or time.time() - p.__dict__.get("_renew_try", 0) < 600 or is_open(p.market):
            return
        p._renew_try = time.time()
        try:
            own = p.budget - p.adopted   # its own money — shares it took over from earlier plans don't grow the next cycle
            if p.tuning:   # next cycle starts from what it really ended with, never above its set budget
                own = min(p.tuning["target_budget"], p.book.cash)
            n = self.start_plan(p.mode, p.pid, p.market, p.risk, p.horizon, own, p.min_pct, p.max_pct, p.dry_run,
                                p.universe, p.inverse, p.style, monthly=True, renewal=True)
        except Exception as e:
            p.log(f"next monthly cycle couldn't start yet: {e} — retrying every 10 min", "warn")
            if not p.__dict__.get("_renew_told"):
                p._renew_told = True
                notify(f"{PERSONAS[p.pid]['name']}: next monthly cycle couldn't start — {str(e)[:120]}")
            return
        if p.tuning:
            n.tuning = p.tuning
            n.reprofile()
        p.renewed_to = n.id
        p.log(f"next monthly cycle started: plan {n.id} until {datetime.fromtimestamp(n.deadline):%m-%d %H:%M} KST", "status")
        if p.mode == "real":
            audit("plan_renew", plan=p.id, new=n.id, deadline=n.deadline)
            notify(f"{PERSONAS[p.pid]['name']}: month-end cycle closed at {p.reason.split('at ')[-1]} — next cycle started "
                   f"({n.budget:,.2f} {CCY[p.market]}, until {datetime.fromtimestamp(n.deadline):%m/%d})")
        self.save()

    def market_call(self, p, reg):
        """대길's read of the market: "short" when the daily regime is bear AND the index fell over the last 2 hours,
        confirmed on consecutive 15-minute readings; otherwise "long". At most max_switches changes a day."""
        t = p.tuning
        c = p.__dict__.get("_call")
        today = now_s()[:5]
        if c is None:
            c = p._call = {"side": "short" if any(s in INVERSE_ALL for s in p.book.positions) else "long",
                           "pend": 0, "t": 0, "day": "", "n": 0}
        if c["day"] != today:
            c.update(day=today, n=0)
            p.log(f"Market call today: {'down' if c['side'] == 'short' else 'up/neutral'} (market {reg.get('label')})", "status")
        if time.time() - c["t"] < t["call_gap"]:
            return c["side"]
        c["t"] = time.time()
        intra = self.intraday(p.market)
        want = "short" if reg.get("label") == "bear" and intra is not None and intra <= t["call_move"] else "long"
        c["pend"] = c["pend"] + 1 if want != c["side"] else 0
        if c["pend"] >= t["call_readings"] and c["n"] < t["max_switches"]:
            c.update(side=want, pend=0, n=c["n"] + 1)
            p.log(f"Market call changed to {'down' if want == 'short' else 'up/neutral'}: market {reg.get('label')}, "
                  f"index {intra if intra is not None else 0:+.2%} over 2 h", "status")
        return c["side"]

    def _combined_limit_add(self, p, budget, broker_cash):
        """A trader just went live with `budget`: start the combined loss limit (first trader) or raise its base.
        reserve = broker cash that is not the traders' money (your own cash stays out of the limit)."""
        lim = self.prefs.get("real_limits")
        if lim is None:
            others = sum(q.book.cash for q in list(self.plans.values())
                         if q is not p and q.mode == "real" and not q.dry_run and q.status in self.ACTIVE)
            lim = {"reserve": round(broker_cash - budget - others, 2), "base": round(budget + others, 2)}
            self.prefs["real_limits"] = lim
        else:
            lim["base"] = round(lim["base"] + budget, 2)
            lim["reserve"] = round(lim.get("reserve", 0) - budget, 2)
        lim["floor"] = round(lim["base"] * (1 - tuning.COMBINED_DROP), 2)
        audit("combined_limit_set", plan=p.id, **{k: lim[k] for k in ("base", "floor", "reserve")})

    def combined_limit_check(self):
        """Real value (the cash ledger + the plans' holdings after sell costs, minus money the plans don't use) against
        the combined limit. Trips once; every real plan then sells out and stays stopped. The ledger only moves on fills
        and confirmed transfers, so money you move in or out of Toss can never trip this, even before it is noticed."""
        lim = self.prefs.get("real_limits")
        if not lim or lim.get("tripped") or time.time() - getattr(self, "_lim_t", 0) < 60:
            return
        self._lim_t = time.time()
        real = [p for p in list(self.plans.values()) if p.mode == "real" and not p.dry_run and p.market == "US" and p.status in self.ACTIVE]
        ex = next((p.executor for p in real if p.executor), None)
        if not ex:
            return
        held = [(s, v) for p in real for s, v in p.book.positions.items()]   # both traders can hold the same stock
        px = data.quotes_prices({s for s, _ in held}) if held else {}
        led = (self.prefs.get("real_ledger") or {}).get("cash")
        cash = led if led is not None else ex.buying_power("USD")
        value = cash + sum(v["qty"] * px.get(s, v["cost"] / v["qty"]) * 0.9989 for s, v in held) - lim["reserve"]
        lim["value"], lim["value_t"] = round(value, 2), time.time()
        if value < lim["floor"]:
            lim["tripped"] = f"combined loss limit: ${value:,.0f} is below ${lim['floor']:,.0f}"
            audit("combined_limit", value=value, floor=lim["floor"])
            notify(f"Real money stopped: {lim['tripped']}. Selling everything; it stays stopped until you restart it.")
            self.save()

    # ── manual money moves (v1.11): deposits and withdrawals never stop or change trading ──
    def real_plans(self):
        """The current real USD plan of each tuned trader: running, or halted but still holding the budget."""
        out = {}
        for p in list(self.plans.values()):
            if p.mode == "real" and not p.dry_run and p.market == "US" and p.tuning and not p.renewed_to \
                    and (p.status in self.ACTIVE or p.status == "halted") \
                    and (p.pid not in out or p.started > out[p.pid].started):
                out[p.pid] = p
        return out

    def _ledger_move(self, delta):
        """Every real fill lands here: this ledger is what Toss cash should be if nothing but trading happened."""
        with self._ledger_lock:
            led = self.prefs.get("real_ledger")
            if led and led.get("cash") is not None:
                led["cash"] = round(led["cash"] + delta, 6)

    @staticmethod
    def _shift_base(base, value, moved, limit):
        """A loss limit's baseline after `moved` left: down by the same dollars. If that alone would reach the limit or
        leave nothing, it keeps today's percent instead: a transfer can never be what trips a limit."""
        new, after = base - moved, value - moved
        if value > 0 and after > 0 and (new <= 0 or after / new - 1 <= -limit):
            new = base * after / value
        return round(max(new, 1.0), 2)

    def transfer_status(self):
        ps = self.real_plans()
        led = self.prefs.get("real_ledger") or {}
        book = sum(p.book.cash for p in ps.values())
        return {"ledger_cash": led.get("cash"), "unallocated": round(max((led.get("cash") or 0) - book, 0.0), 2),
                "budgets": {pid: round(p.budget, 2) for pid, p in ps.items()},
                "cash": {pid: round(p.book.cash, 2) for pid, p in ps.items()},
                "limits": self.prefs.get("real_limits"), "transfers": (self.prefs.get("transfers") or [])[-20:]}

    def transfer_check(self):
        """Toss USD cash against the ledger, once a minute with every plan's lock held, so no order is in flight. A gap
        seen twice in a row, a minute apart, is money you moved. Skips the round while Toss is unsure: an open order
        (it holds cash back) or a real order still waiting for confirmation."""
        ps = self.real_plans()
        lim = self.prefs.get("real_limits")
        if not ps or not lim or LEASE.status() not in ("mine", "unconfigured"):
            return
        got = []
        try:
            for p in ps.values():
                if not p.lock.acquire(timeout=1):
                    return
                got.append(p)
            if any(p.pending for p in ps.values()):
                return
            ex = TossExecutor(self.toss)
            if [o for o in ex.open_orders() if o.get("currency", "USD") == "USD"]:
                return
            cash = ex.buying_power("USD")
            led = self.prefs.setdefault("real_ledger", {})
            if led.get("cash") is None:
                led["cash"] = cash
                lim["reserve"] = round(cash - sum(p.book.cash for p in ps.values()), 2)   # not the traders' money
                audit("ledger_start", cash=cash, reserve=lim["reserve"])
                return self.save()
            with self._ledger_lock:
                delta = round(cash - led["cash"], 2)
                if abs(delta) < 0.01:
                    led.pop("seen", None)
                    return
                seen = led.get("seen")
                if not seen or abs(seen["delta"] - delta) > 0.5:
                    led["seen"] = {"delta": delta, "t": time.time()}
                    return
                if time.time() - seen["t"] < 50:
                    return
                led.pop("seen", None)
                led["cash"] = cash
            if abs(delta) < tuning.DRIFT:   # rounding, a few cents of fees: nobody's profit, nobody's loss
                lim["reserve"] = round(lim["reserve"] + delta, 2)
                audit("cash_drift", delta=delta, cash=cash)
            else:
                self._apply_transfer(delta, ps, cash, lim)
            self.save()
        finally:
            for p in got:
                p.lock.release()

    def _apply_transfer(self, delta, ps, cash, lim):
        free = max(cash - delta - sum(p.book.cash for p in ps.values()), 0.0)
        rec = dict(t=now_s(), kind="deposit" if delta > 0 else "withdrawal", amount=round(abs(delta), 2), cash=round(cash, 2))
        names = lambda: ", ".join(f"{PERSONAS[pid]['name']} ${p.budget:,.2f}" for pid, p in ps.items())
        if delta > 0:   # waits as unallocated cash until you say how to use it
            lim["reserve"] = round(lim["reserve"] + delta, 2)
        else:
            out = -delta
            spare = min(out, free)                      # unallocated cash goes first
            rest = out - spare
            held_back = min(out, max(lim["reserve"], 0.0))
            lim["reserve"] = round(lim["reserve"] - held_back, 2)
            weight = sum(tuning.SPLIT[pid] for pid in ps)
            try:
                px = data.quotes_prices({s for p in ps.values() for s in p.book.positions})
            except Exception:
                px = {}
            value = cash + out - (lim["reserve"] + held_back) + sum(v["qty"] * px.get(s, v["cost"] / v["qty"]) * 0.9989
                                                                    for p in ps.values() for s, v in p.book.positions.items())
            for pid, p in ps.items():
                cut = rest * tuning.SPLIT[pid] / weight
                if cut < 0.005:
                    continue
                # a trader short of free cash goes negative on its own ledger: it can't buy, nothing is sold, and its
                # next sales fill the gap. Its budget is already at the new size, so its loss limit stays right.
                p.budget = self._shift_base(p.budget, p.book.net(px), cut, p.profile.max_loss)
                p.book.cash = round(p.book.cash - cut, 6)
                p.tuning["target_budget"] = p.budget
                for e in p.book.equity_log:   # the chart shows trading only
                    e["net"] = round(e["net"] - cut, 4)
                p.log(f"withdrawal: ${cut:,.2f} came out of this budget, now ${p.budget:,.2f}", "status")
                rec[pid] = round(cut, 2)
            drop = out - held_back                      # what really left the trading money
            lim["base"] = self._shift_base(lim["base"], value, drop, tuning.COMBINED_DROP)
            lim["floor"] = round(lim["base"] * (1 - tuning.COMBINED_DROP), 2)
            rec["unallocated_used"] = round(spare, 2)
        unalloc = max(cash - sum(p.book.cash for p in ps.values()), 0.0)
        rec.update(budgets={pid: round(p.budget, 2) for pid, p in ps.items()}, unallocated=round(unalloc, 2))
        self.prefs["transfers"] = (self.prefs.get("transfers") or [])[-199:] + [rec]
        audit("transfer", **rec)
        trade_alert(f"Toss {rec['kind']} ${rec['amount']:,.2f}. Nothing paused or sold.\nBudgets: {names()}. Unallocated ${unalloc:,.2f}.")

    def allocate(self, amounts):
        """Hand unallocated cash to the traders while they run: {"vector": 800, "blaze": 200}. New money, so the normal
        readiness and cap checks apply. No restart."""
        ps = self.real_plans()
        lim = self.prefs.get("real_limits")
        if not lim or any(pid not in ps or amt <= 0 for pid, amt in amounts.items()) or not amounts:
            raise ValueError("give a positive amount for a running real trader")
        got = []
        try:
            for p in ps.values():
                if not p.lock.acquire(timeout=5):
                    raise ValueError("a trader is busy with an order, try again in a minute")
                got.append(p)
            free = self.transfer_status()["unallocated"]
            if sum(amounts.values()) > free + 1e-9:
                raise ValueError(f"only ${free:,.2f} is unallocated")
            for pid, amt in amounts.items():
                p = ps[pid]
                self.require_ready(pid, p.market)
                self.check_real_cap("USD", p.budget - p.adopted + amt, exclude=p)
            total = sum(p.budget - p.adopted + amounts.get(pid, 0.0) for pid, p in ps.items() if p.status in self.ACTIVE)
            if total > RealState.caps()["budget"]["USD"]:
                raise ValueError(f"real-money plans would total {total:,.2f} USD, over the cap")
            for pid, amt in amounts.items():
                p = ps[pid]
                p.budget = round(p.budget + amt, 2)
                p.book.cash = round(p.book.cash + amt, 6)
                p.tuning["target_budget"] = p.budget
                p.log(f"${amt:,.2f} of unallocated cash added, budget now ${p.budget:,.2f}", "status")
            add = sum(amounts.values())
            lim["reserve"] = round(lim["reserve"] - add, 2)
            lim["base"] = round(lim["base"] + add, 2)
            lim["floor"] = round(lim["base"] * (1 - tuning.COMBINED_DROP), 2)
            audit("allocate", amounts=amounts, **{k: lim[k] for k in ("reserve", "floor", "base")})
            self.save()
            return self.transfer_status()
        finally:
            for p in got:
                p.lock.release()

    # ── v1.12: daily full-account budget ──
    def _unallocated(self, ps):
        led = (self.prefs.get("real_ledger") or {}).get("cash")
        return None if led is None else round(led - sum(p.book.cash for p in ps.values()), 2)

    def reconcile_reserve(self):
        """Cash the traders don't own (unallocated) is kept out of the combined loss limit's value. v1.11's first ledger
        reading took the whole Toss balance without this, which counted your new money as the traders' cushion."""
        ps = self.real_plans()          # halted traders still own their cash: count them too
        lim, led = self.prefs.get("real_limits"), self.prefs.get("real_ledger") or {}
        free = self._unallocated(ps)
        if not ps or not lim or free is None or led.get("seen") or abs(lim.get("reserve", 0) - free) < 0.5:
            return
        audit("reserve_fix", before=lim.get("reserve"), after=free)
        lim["reserve"] = free
        self.save()

    def daily_budget(self, force=False):
        """Once per US trading day, before the traders' first trade: the whole Toss account (cash + the traders' own
        positions; your own holdings never count) is the traders' money. Unallocated cash goes to each trader up to
        its share (tuning.SPLIT) within the per-trader and total caps. A trader that isn't ready for more real money
        keeps its budget and its share goes to the other. Never takes money from a trader, never buys anything."""
        cfg = self.prefs.setdefault("daily_budget", {"enabled": False})
        self.reconcile_reserve()
        if not cfg.get("enabled") and not force:
            return None
        if markets.session("US") is None and not force:
            return None
        now = datetime.now(ZoneInfo("America/New_York")) + timedelta(hours=6)   # 20:00 ET = next trading day's run
        day = now.strftime("%Y-%m-%d")
        if cfg.get("day") == day and not force:
            return None
        ps = {pid: p for pid, p in self.real_plans().items() if p.status in self.ACTIVE}
        lim = self.prefs.get("real_limits")
        free = self._unallocated(self.real_plans())   # unallocated = ledger minus EVERY real trader's cash
        if not ps or not lim or free is None:
            return None
        if any(p.pending for p in ps.values()):
            return None
        held = {s for p in ps.values() for s in p.book.positions}
        px = data.quotes_prices(held) if held else {}
        pos = sum(v["qty"] * px.get(s, v["cost"] / v["qty"]) for p in ps.values() for s, v in p.book.positions.items())
        led = self.prefs["real_ledger"]["cash"]
        total = led + pos
        buffer = round(max(10.0, total * tuning.DAILY_BUFFER), 2)
        lim["reserve"] = free                       # what the traders don't own is never counted as their value
        caps = RealState.caps()
        ready = {}
        for pid, p in ps.items():
            try:
                self.require_ready(pid, p.market); ready[pid] = True
            except ValueError as e:
                ready[pid] = str(e)
        usable = total - buffer
        fixed = sum(p.budget - p.adopted for pid, p in ps.items() if ready[pid] is not True)
        weight = sum(tuning.SPLIT.get(pid, 0) for pid in ps if ready[pid] is True)
        target = {}
        for pid, p in ps.items():
            if ready[pid] is not True:
                target[pid] = p.budget
            else:
                target[pid] = min(caps["plan"]["USD"], (usable - fixed) * tuning.SPLIT.get(pid, 0) / weight) if weight else p.budget
        room = max(0.0, caps["budget"]["USD"] - sum(p.budget - p.adopted for p in ps.values()))
        left = max(0.0, min(free - buffer, room))
        add = {}
        for pid in sorted(ps, key=lambda k: -tuning.SPLIT.get(k, 0)):
            a = round(min(max(0.0, target[pid] - ps[pid].budget), left), 2)
            if a >= 1.0:
                add[pid] = a; left -= a
        rec = {"day": day, "t": time.time(), "total": round(total, 2), "positions": round(pos, 2), "buffer": buffer,
               "unallocated_before": free, "ready": {k: v is True for k, v in ready.items()},
               "why_not": {k: v for k, v in ready.items() if v is not True}, "target": {k: round(v, 2) for k, v in target.items()},
               "added": add}
        if add:
            st = self.allocate(add)
            rec["budgets"] = st["budgets"]; rec["unallocated_after"] = st["unallocated"]
        else:
            rec["budgets"] = {pid: p.budget for pid, p in ps.items()}; rec["unallocated_after"] = free
            self.save()
        cfg.update(day=day, last=rec)
        audit("daily_budget", **rec)
        names = {pid: PERSONAS[pid]["name"] for pid in set(ps) | set(rec["budgets"]) | set(rec["why_not"])}
        line = (f"Daily budget: account ${total:,.2f}. "
                + ", ".join(f"{names[k]} ${v:,.2f}" for k, v in rec["budgets"].items())
                + f". Unallocated ${rec['unallocated_after']:,.2f}."
                + "".join(f" {names[k]} not ready, kept its budget." for k in rec["why_not"]))
        print(line, flush=True)
        if add:
            trade_alert(line)
        return rec

    def warm_bars(self):
        """Load and save the real traders' price history ahead of time, so they can score stocks at the open whatever the
        feed is doing then. Runs at start-up and every hour."""
        try:
            for p in self.real_plans().values():
                syms = set(self.symbols(p)) | set(p.book.positions)
                got = data.bars(syms, p.profile.bar, ttl=300)
                print(f"price history {PERSONAS[p.pid]['name']}: {len(got)} of {len(syms)} stocks ({p.profile.bar})", flush=True)
                if len(got) < len(syms) / 2 and time.time() - getattr(self, "_warm_told", 0) > 21600:
                    self._warm_told = time.time()
                    trade_alert(f"Price history is short: {len(got)} of {len(syms)} stocks for {PERSONAS[p.pid]['name']}. "
                                f"Feed: {data.FEED.get('last_error') or 'empty reply'}")
                for iv in ("1d", "5m"):
                    data.bars([self.BENCH[p.market]], iv, ttl=300)
        except Exception as e:
            log_error("warm bars", e)

    def _transfer_round(self):
        try:
            self.transfer_check()
        except Exception as e:   # Toss hiccup: nothing changes, next minute tries again
            log_error("transfer check", e)
        try:
            self.daily_budget()
        except Exception as e:   # anything odd: yesterday's budgets stay, next minute tries again
            log_error("daily budget", e)
        finally:
            self._xfer_busy = False

    def apply_pending_tuning(self):
        """One-shot switch to the settings in tuning.py (state/app/tuning_pending.json); renamed once it has run."""
        f = STATE / tuning.PENDING
        if not f.exists():
            return
        try:
            done = []
            ps = [p for p in list(self.plans.values()) if p.mode == "real" and not p.dry_run and p.pid in tuning.PLANS
                  and p.status in self.ACTIVE]
            if {p.pid for p in ps} != set(tuning.PLANS):
                raise ValueError("expected one running real plan per trader")
            for p in ps:
                t = tuning.PLANS[p.pid]
                with p.lock:
                    for s, v in p.book.positions.items():
                        if not p.tuning:   # never re-tag: a second run must not overwrite the original rules
                            v.setdefault("legacy_exit", {"risk": p.risk, "horizon": p.horizon, "started": p.started})
                self.update_plan(p.id, budget=t["target_budget"], horizon=t.get("horizon"))
                with p.lock:
                    p.tuning = dict(t); p.reprofile(); p.last_tick = 0
                    p.log("v1.08 real-money settings on", "status")
                done.append(p)
            ex = next(p.executor for p in ps)
            held = {s: v for p in ps for s, v in p.book.positions.items()}
            px = data.quotes_prices(set(held)) if held else {}
            cash = ex.buying_power("USD")
            value = cash + sum(v["qty"] * px.get(s, v["cost"] / v["qty"]) * 0.9989 for s, v in held.items())
            base = sum(p.budget for p in ps)
            book = sum(p.book.cash + sum(px.get(s, v["cost"] / v["qty"]) * v["qty"] * 0.9989 for s, v in p.book.positions.items()) for p in ps)
            self.prefs["real_limits"] = {"reserve": round(value - book, 2), "floor": round(base * (1 - tuning.COMBINED_DROP), 2), "base": base}
            audit("tuning_on", plans=[p.id for p in ps], **self.prefs["real_limits"])
            self.save()
            f.replace(f.with_suffix(".done"))
        except Exception as e:
            log_error("apply tuning", e)
            raise

    def apply_rebase(self):
        """One-time reset after money left Toss (state/app/rebase_pending.json, renamed once it has run): both real plans
        must be flat. Sets their budgets and ledgers, then re-measures the combined limit from the real USD balance.
        It does not restart them: that stays with resume_plan and its normal checks."""
        f = STATE / tuning.REBASE
        if not f.exists():
            return
        try:
            t = json.loads(f.read_text())
            ps = [p for p in list(self.plans.values()) if p.mode == "real" and not p.dry_run and p.pid in t
                  and (p.status in self.ACTIVE or p.status == "halted") and p.tuning]
            if sorted(p.pid for p in ps) != sorted(t):
                raise ValueError("expected one real plan per trader")
            if any(p.book.positions for p in ps):
                raise ValueError("both plans must be flat")
            cash = TossExecutor(self.toss).buying_power("USD")
            base = round(sum(t.values()), 2)
            if base > cash:
                raise ValueError(f"budgets {base} are above Toss cash {cash}")
            for p in ps:
                with p.lock:
                    p.budget = p.book.cash = float(t[p.pid])
                    p.tuning["target_budget"] = float(t[p.pid])
                    p.log(f"budget reset to {t[p.pid]:,.2f} USD to match the Toss balance", "status")
            self.prefs["real_limits"] = {"reserve": round(cash - base, 2), "floor": round(base * (1 - tuning.COMBINED_DROP), 2), "base": base}
            self.prefs["real_ledger"] = {"cash": cash}
            audit("rebase", plans=[p.id for p in ps], cash=cash, **self.prefs["real_limits"])
            self.save()
            f.replace(f.with_suffix(".done"))
        except Exception as e:   # the plans are stopped, so stay stopped and leave the file for a look
            log_error("apply rebase", e)

    def _end(self, p, market_open, reason, status):
        if p.book.positions and (not market_open or not self.close_all(p, reason)):
            p.status, p.reason = "closing", reason
            p.log(f"{reason} — " + ("market closed, selling at next open" if not market_open
                                     else "some sells didn't go through, retrying"), "warn")
            return
        p.finish(status, reason)
        self.settle(p)

    def buy(self, p, s, qty, quote, bar_close, sig, score):
        why = risk_gate(p, "BUY", s, qty, quote, bar_close)
        if why:
            if why not in ("cooldown",):
                p.log(f"blocked BUY {s}: {why}", "warn")
            return False
        if p.mode == "real" and not p.dry_run:   # the plan's ledger is not the account: check real Toss cash first
            have = p.executor.buying_power(CCY[p.market])
            if qty * quote["price"] * 1.004 > have:
                p.log(f"blocked BUY {s}: Toss cash {have:,.2f} is below this order", "warn"); return False
        f = p.executor.execute(p.market, "BUY", s, qty, quote["price"], p.id)
        if not f:
            p.log(f"BUY {s} not filled", "warn"); return False
        p.book.apply_buy(s, f["qty"], f["avg"], f["fees"], {k: sig[k] for k in sig if not k.startswith("_")}, now_s())
        pos = p.book.positions[s]
        pos.setdefault("bench0", getattr(p, "_bench", None)); pos.setdefault("regime", getattr(p, "_regime", None))
        why = f"{getattr(p, '_brain', self.brains[p.pid]).explain(sig, getattr(p, '_regime', None), s)}; score {score:+.2f} vs bar " \
              f"{getattr(p, '_entry', self.brains[p.pid].entry):.2f}, market {getattr(p, '_regime', '?')}"
        p.book.trades[-1].update(ts=time.time(), why=why)
        p.log(f"BUY {s} ×{f['qty']:g} @ {f['avg']:,.4g} (score {score:+.2f}, fee {f['fees']:,.2f}) — {why}", "buy")
        if p.mode == "real" and not p.dry_run:
            try:   # plain text, no numbers: who, what, why
                R = getattr(p, "_regime", None)
                b = getattr(p, "_brain", self.brains[p.pid])
                trade_alert(alerts.buy_text(PERSONAS[p.pid]['name'], alerts.stock_label(s, NAMES, universe.info(s)),
                                            alerts.buy_reason(b.weights(R), sig, s in INVERSE_ALL, b.sym_bias(s), b.recall(sig))))
            except Exception as e:
                log_error("trade alert", e)
        return True

    def sell(self, p, s, qty, quote, bar_close, reason):
        why = risk_gate(p, "SELL", s, qty, quote, bar_close)
        if why:
            p.log(f"blocked SELL {s}: {why}", "warn"); return False
        f = p.executor.execute(p.market, "SELL", s, qty, quote["price"], p.id)
        if not f:
            p.log(f"SELL {s} not filled", "warn"); return False
        pos0 = dict(p.book.positions.get(s, {}))
        tr, sig, net_ret = p.book.apply_sell(s, f["qty"], f["avg"], f["fees"], reason, now_s())
        b0, b1 = pos0.get("bench0"), getattr(p, "_bench", None)
        excess = net_ret - (b1 / b0 - 1) if b0 and b1 else None
        tr["ts"] = time.time()
        # Freqtrade-style cooldown: no re-buy right after a sale, and much longer after a stop-loss
        # (the 10-10 post-mortem lost money re-buying the same names it had just been stopped out of)
        p.cooldown[s] = time.time() + (max(p.profile.tick * 5, min(p.profile.secs / 4, 7200), 1800)
                                       if "stop loss" in reason else p.profile.tick * 5)
        p.log(f"SELL {s} ×{f['qty']:g} @ {f['avg']:,.4g} — {reason} — P&L {tr['pnl']:+,.2f} ({net_ret:+.2%} net)", "sell")
        if p.mode == "real" and not p.dry_run:
            try:   # plain text, no numbers: who, what, why
                trade_alert(alerts.sell_text(PERSONAS[p.pid]['name'], alerts.stock_label(s, NAMES, universe.info(s)),
                                             alerts.sell_reason(reason)))
            except Exception as e:
                log_error("trade alert", e)
        self.brains[p.pid].learn_trade(sig, net_ret, s, excess, pos0.get("regime"))   # judged beyond the market
        return True

    def close_all(self, p, reason) -> bool:
        """Sell everything. Returns True only when the plan really holds nothing any more."""
        q = data.quotes(list(p.book.positions))
        for s, pos in list(p.book.positions.items()):
            try:
                self.sell(p, s, pos["qty"], q.get(s), None, reason)
            except Halt:
                raise
            except Exception as e:
                p.log(f"couldn't sell {s} yet ({e}) — will retry", "warn")
        return not p.book.positions

    def adopt_orphans(self, p):
        """One investor manages all of its own purchases: positions left in its earlier plans (halted / stopped /
        finished — same mode, same real-or-dry-run, same market) move into its running plan."""
        if p.league or p.status not in ("running", "waiting", "closing"):
            return
        for o in list(self.plans.values()):
            if (o is p or o.league or o.pid != p.pid or o.mode != p.mode or o.dry_run != p.dry_run
                    or o.market != p.market or o.status not in ("halted", "stopped", "done")
                    or not (o.book.positions or o.pending)):
                continue
            if not o.lock.acquire(timeout=2):
                continue
            try:
                if o.mode == "real" and not o.dry_run and o.pending:
                    self.resolve_pending(o)
                    if o.pending:
                        continue   # an earlier order still unconfirmed — merge once Toss confirms it
                moved = []
                cost = 0.0
                for sym, pos in o.book.positions.items():
                    cur = p.book.positions.get(sym)
                    if cur:
                        cur["qty"] += pos["qty"]; cur["cost"] += pos["cost"]
                        cur["peak_ret"] = max(0.0, p.book.pos_net_ret(sym, cur["cost"] / cur["qty"]))
                    else:
                        p.book.positions[sym] = dict(pos, peak_ret=0.0)
                    cost += pos["cost"]
                    moved.append(f"{sym} ×{pos['qty']:g}")
                p.budget += cost
                p.adopted += cost
                if p.mode == "mock" and not o.settled:   # its unused fake cash comes along too
                    p.book.cash += o.book.cash; p.budget += o.book.cash
                    o.book.cash = 0.0; o.settled = True
                o.book.positions = {}
                o.status, o.reason = "merged", f"positions moved to running plan {p.id}"
                o.log(f"MERGED into {p.id}: {', '.join(moved)}", "status")
                p.log(f"MERGED from earlier plan {o.id}: {', '.join(moved)} (cost {cost:,.2f}) — now managed here", "status")
                if p.mode == "real":
                    audit("plan_merge", plan=p.id, source=o.id, positions=moved, cost=round(cost, 4))
            finally:
                o.lock.release()

    def resolve_pending(self, p):
        """Apply the real outcome of orders whose state was unknown (e.g. connection dropped)."""
        ex = p.executor if isinstance(p.executor, TossExecutor) else TossExecutor(self.toss, dry_run=False)
        for o in list(p.pending):
            st = ex.get_order(o["orderId"])
            if st["status"] not in ex.TERMINAL:
                try:
                    self.toss.cancel_order(o["orderId"])
                except Exception:
                    pass
                continue  # check again next time
            f = ex.fill_of(st, p.id)
            if f:
                if o["side"] == "BUY":
                    p.book.apply_buy(o["symbol"], f["qty"], f["avg"], f["fees"], {}, now_s())
                else:
                    pos = p.book.positions.get(o["symbol"])
                    if pos:
                        p.book.apply_sell(o["symbol"], min(f["qty"], pos["qty"]), f["avg"], f["fees"],
                                          "confirmed after reconnect", now_s())
                p.log(f"confirmed order {o['orderId'][:10]}…: {o['side']} {o['symbol']} ×{f['qty']:g} @ {f['avg']:,.4g}", "status")
            else:
                p.log(f"confirmed order {o['orderId'][:10]}…: not filled", "status")
            audit("pending_resolved", plan=p.id, orderId=o["orderId"], filled=bool(f))
            p.pending.remove(o)

    def reconcile(self, p):
        """Bot never owns more than Toss says you hold. Manual sells shrink the bot's ledger."""
        if p.pending:
            self.resolve_pending(p)
            if p.pending:
                raise Halt("waiting for Toss to confirm an earlier order")
        actual = p.executor.holdings()
        others = [o for o in list(self.plans.values()) if o is not p and o.mode == "real" and not o.dry_run
                  and (o.status in self.ACTIVE or o.book.positions)]
        for s, pos in list(p.book.positions.items()):
            have = actual.get(s, 0) - sum(o.book.positions.get(s, {}).get("qty", 0) for o in others)
            if have + 1e-9 < pos["qty"]:
                have = max(0.0, have)
                p.log(f"reconcile: Toss holds {have} {s}, bot ledger had {pos['qty']} — adjusting", "warn")
                audit("reconcile_adjust", plan=p.id, symbol=s, ledger=pos["qty"], actual=have)
                if have <= 0:
                    del p.book.positions[s]
                else:
                    pos["cost"] *= have / pos["qty"]; pos["qty"] = have

    # ── real-mode controls ──────────────────────────────────
    def arm(self, phrase):
        if phrase.strip() != "REAL MONEY":
            raise ValueError('type exactly: REAL MONEY')
        (self.prefs.get("real_limits") or {}).pop("tripped", None)
        if not RealState.env_unlocked():
            raise ValueError("locked in .env (REAL_TRADING_CONFIRMED=yes missing)")
        RealState.set_armed(True); RealState.killed = False
        audit("armed")
        for p in list(self.plans.values()):
            if p.mode == "real" and p.status == "paused" and p.reason.startswith("app restarted"):
                with p.lock:
                    try:
                        self.reconcile(p)
                    except Exception as e:
                        p.log(f"couldn't reconcile with Toss yet ({e}) — still paused", "warn"); continue
                    if time.time() >= p.deadline:
                        p.started, p.deadline = time.time(), time.time() + p.profile.secs
                    p.status, p.reason = "running", ""
                    p.log("re-armed, reconciled with Toss, resuming", "status")
        self.save()

    def kill(self, flatten=False):
        if LEASE.status() == "other":   # the other engine owns the account right now; its kill switch is the one
            raise PermissionError("The other engine holds the real-money lease — use the kill switch there.")
        RealState.killed = True; RealState.set_armed(False)
        audit("KILL_SWITCH", flatten=flatten)
        n = 0
        try:
            n = TossExecutor(self.toss).cancel_open()
        except Exception as e:
            audit("kill_cancel_failed", error=str(e)[:200])
        for p in list(self.plans.values()):
            if p.mode == "real" and p.status in ("running", "waiting", "closing", "paused"):
                with p.lock:   # waits for any order in flight, then stops the plan
                    if flatten and p.book.positions and is_open(p.market) and not p.dry_run:
                        RealState.flattening = True   # SELLs only; buys stay blocked by the kill switch
                        try:
                            self.close_all(p, "kill switch flatten")
                        except Exception as e:
                            p.log(f"flatten failed: {e}", "warn")
                        finally:
                            RealState.flattening = False
                    p.finish("halted", "KILL SWITCH")
        self.save()
        return n

    # ── league ──────────────────────────────────────────────
    def player_cfg(self, pid):
        """Each AI investor has its own investment mode (risk × horizon) and its own rounds."""
        players = self.league_cfg.setdefault("players", {})
        if pid not in players:  # first run after this feature: keep the shared setting they were already using
            players[pid] = {"risk": self.league_cfg.get("risk", 3), "horizon": self.league_cfg.get("horizon", "1d"),
                            "round": self.league_cfg.get("round", 1)}
        players[pid].setdefault("universe", "large")
        return players[pid]

    def set_player_mode(self, pid, risk, horizon, restart_now=False, uni=None):
        c = self.player_cfg(pid)
        c.update(risk=risk, horizon=horizon)
        if uni:
            c["universe"] = uni
        if restart_now:  # end this player's round now: sell (or sell at next open), then a new round starts in the new mode
            for p in [p for p in list(self.plans.values()) if p.league and p.pid == pid
                      and p.status in ("running", "waiting", "closing")]:
                self.stop_plan(p.id)
        self.save()

    def start_league_round(self, pids=None):
        cfg = self.league_cfg
        for pid in pids or PERSONAS:
            pc = self.player_cfg(pid)
            for m in ("US", "KR"):
                old = next((p for p in list(self.plans.values()) if p.league and p.pid == pid and p.market == m), None)
                cash = old.book.cash if old else cfg["budget"][m]
                p = Plan("mock", pid, m, pc["risk"], pc["horizon"], cash, league=True, universe=pc["universe"])
                if old:
                    p.book.equity_log = old.book.equity_log
                    p.book.trades = old.book.trades
                    p.skips = old.skips
                    del self.plans[old.id]
                p.log(f"Round {pc['round']} · {p.profile.risk_name} risk · {p.profile.horizon_label} · "
                      f"{universe.MODES[p.universe]}", "status")
                self.attach_executor(p)
                self.plans[p.id] = p
        self.save()

    def league_round_check(self):
        """A player's round ends when both its markets are done; it then learns and starts its next round."""
        for pid in PERSONAS:
            lp = [p for p in list(self.plans.values()) if p.league and p.pid == pid]
            if not lp or not all(p.status in ("done", "halted", "stopped") for p in lp):
                continue
            board = [r for r in self.leaderboard() if not r.get("baseline")]
            me = next(r for r in board if r["pid"] == pid)
            pc = self.player_cfg(pid)
            self.league_history.append({"pid": pid, "round": pc["round"], "ended": now_s(),
                                        "mode": f"{pc['risk']}_{pc['horizon']}", "ret": me["ret"], "trades": me["trades"]})
            leader = board[0]["pid"]
            lb = self.brains[leader]
            if leader != pid and len(lb.history) >= 5:
                self.brains[pid].imitate(lb)
            summary = {r["pid"]: f"{r['ret']:+.2%} ({r['risk_name']}·{r['horizon_label']})" for r in board}
            summary["index_buy_and_hold"] = f"{self.baseline_row()['ret']:+.2%}"
            summary["you_vs_index"] = {m: f"you {v['ret']:+.2%} vs {v['name']} {v['index']:+.2%} over {v['days']} days"
                                       for m, v in self.vs_index(pid).items()}
            digest = {s: v["score"] for s, v in news.snapshot()["symbols"].items()}
            self.bg.submit(self.brains[pid].reflect, summary, digest,
                           {m: self.regime(m)["label"] for m in ("US", "KR")})
            pc["round"] += 1
            self.start_league_round([pid])

    def leaderboard(self):
        fx = data.usdkrw()
        rows = []
        for pid, persona in PERSONAS.items():
            ps = [p for p in list(self.plans.values()) if p.league and p.pid == pid]
            start = sum(self.league_cfg["budget"][p.market] * (fx if p.market == "US" else 1) for p in ps)
            q = data.quotes({s for p in ps for s in p.book.positions})
            prices = {s: v["price"] for s, v in q.items()}
            now = sum(p.book.net(prices) * (fx if p.market == "US" else 1) for p in ps)
            sells = [t for p in ps for t in p.book.trades if t["side"] == "SELL"]
            curve = np.array([e["net"] for p in ps if p.market == "US" for e in p.book.equity_log] or [1.0])
            rets = np.diff(curve) / curve[:-1] if len(curve) > 2 else np.array([0.0])
            peak = np.maximum.accumulate(curve)
            b = self.brains[pid]
            pc = self.player_cfg(pid)
            prof = profiles.make(pc["risk"], pc["horizon"])
            live = [p for p in ps if p.status in ("running", "waiting", "closing", "paused")]
            rows.append({"risk": pc["risk"], "horizon": pc["horizon"], "risk_name": prof.risk_name,
                         "universe": pc["universe"], "universe_live": live[0].universe if live else pc["universe"],
                         "horizon_label": prof.horizon_label, "round": pc["round"],
                         "mode_live": f"{live[0].profile.risk_name} · {live[0].profile.horizon_label}" if live else "",
                         "round_ends": min((p.deadline for p in live), default=None),
                         "paused": bool(live) and all(p.status == "paused" for p in live),
                         "pid": pid, "name": persona["name"], "style": persona["style"], "color": persona["color"],
                         "bio": persona["bio"], "ret": now / start - 1 if start else 0,
                         "trades": len(sells), "win": (sum(t["pnl"] > 0 for t in sells) / len(sells)) if sells else None,
                         "mdd": float(((curve - peak) / peak).min()) if len(curve) > 1 else 0.0,
                         "sharpe": float(rets.mean() / rets.std() * math.sqrt(len(rets))) if rets.std() > 0 else 0.0,
                         "weights": b.w, "entry": round(b.entry, 3), "tp_mult": round(b.tp_mult, 2),
                         "sl_mult": round(b.sl_mult, 2), "lessons": b.lessons[-5:], "generation": b.generation,
                         "learned": len(b.history), "knowledge": b.knowledge(),
                         "vs": self.vs_index(pid), "readiness": (rd := self.readiness(pid)),
                         "ready": any(v["ready"] for v in rd.values()),
                         "positions": {p.market: {s: v["qty"] for s, v in p.book.positions.items()} for p in ps}})
        rows.append(self.baseline_row())
        rows.sort(key=lambda r: -r["ret"])   # the index competes as a player: beat it or rank below it
        return rows

    BASE_SYMS = {"US": "SPY", "KR": "069500"}

    def baseline_row(self):
        """Buy-and-hold index with the same starting money — the bar every player must beat."""
        base = self.league_cfg.setdefault("baseline", {})
        q = data.quotes(self.BASE_SYMS.values())
        rets = []
        for m, s in self.BASE_SYMS.items():
            if s in q and q[s]["age"] < 3 * 86400:
                base.setdefault(s, q[s]["price"])
                rets.append(q[s]["price"] / base[s] - 1)
        return {"pid": "index", "name": "S&P 500 + KOSPI 200", "style": "Buy & hold — the bar to beat", "color": "#8E8E93",
                "bio": "SPY + KODEX 200, bought at league start and never traded.", "baseline": True,
                "ret": sum(rets) / len(rets) if rets else 0.0, "trades": 0, "win": None, "mdd": 0.0,
                "sharpe": 0.0, "lessons": [], "positions": {}}

    # ── scheduler ───────────────────────────────────────────
    def universe(self):
        u = set()
        for p in list(self.plans.values()):
            if p.status in ("running", "waiting", "closing", "paused"):
                u |= set(self.symbols(p)) | set(p.book.positions)
        return sorted(u)

    _reporting = False

    def _send_report(self, force=False):
        try:
            return report.send(self, force=force)
        except Exception as e:
            log_error("daily report", e)
            raise
        finally:
            self._reporting = False

    def train_now(self, pids=None):
        """Night school for the investors (see train.py). Runs in the background; results in brain.train."""
        self._training = True
        try:
            out = train.run(self, pids)
            n = sum(v.get("n", 0) for v in out["players"].values())
            print(f"night school {out['date']}: {n} decisions replayed", flush=True)
            return out
        except Exception as e:
            log_error("night school", e)
            raise
        finally:
            self._training = False

    def run_forever(self):
        LEASE.start()
        news.start_background(self.universe)
        self.bg.submit(self.backfill_perf)
        self.bg.submit(data.probe_toss_candles)   # read-only: learns Toss's candle format for TOSS_BARS=on
        self.bg.submit(self.warm_bars)
        last_save = last_pending = last_report_check = last_train_check = last_xfer = 0
        last_warm = time.time()
        while True:   # nothing in here may kill the loop — every step is guarded
            if time.time() - last_pending > 60:
                last_pending = time.time()
                for p in list(self.plans.values()):
                    if p.pending and p.status not in self.ACTIVE and p.lock.acquire(timeout=1):
                        try:
                            self.resolve_pending(p)
                        except Exception as e:
                            log_error("pending", e)
                        finally:
                            p.lock.release()
            try:
                for p in list(self.plans.values()):
                    if p.status in ("running", "waiting", "closing") and not p.busy \
                            and time.time() - p.last_tick >= p.profile.tick:
                        p.busy = True
                        self.pool.submit(self.tick, p)
            except Exception as e:
                log_error("scheduler", e)
            try:
                for p in list(self.plans.values()):
                    if p.monthly and p.status == "done" and not p.renewed_to:
                        self.renew_monthly(p)
            except Exception as e:
                log_error("monthly renew", e)
            if time.time() - last_xfer > 60 and not self._xfer_busy:
                last_xfer = time.time()
                self._xfer_busy = True
                self.pool.submit(self._transfer_round)
            if time.time() - last_warm > 3600:
                last_warm = time.time()
                self.bg.submit(self.warm_bars)
            try:
                if self.league_cfg.get("running"):
                    self.league_round_check()
            except Exception as e:
                log_error("league", e)
            try:
                if not self._reporting and time.time() - last_report_check > 60:
                    last_report_check = time.time()
                    if report.due():
                        self._reporting = True
                        self.bg.submit(self._send_report)
            except Exception as e:
                log_error("report", e)
            try:
                if not self._training and time.time() - last_train_check > 60:
                    last_train_check = time.time()
                    if train.due(self.train_date):
                        self._training = True
                        self.bg.submit(self.train_now)
            except Exception as e:
                log_error("night school", e)
            try:
                if time.time() - last_save > 15:
                    self.save(); last_save = time.time()
            except Exception as e:
                log_error("save", e)
            self.heartbeat = time.time()
            time.sleep(2)
