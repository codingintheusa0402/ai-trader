"""AI investor brains. Every player scores stocks with the same 5 signals but its own weights:

  trend     fast MA vs slow MA (volatility-normalised)
  momentum  return over `mom` bars (volatility-normalised)
  reversion oversold bounce (RSI below 50 → positive)
  breakout  close vs prior N-bar high
  news      LLM sentiment of real headlines

Learning (bounded, persona-anchored so the 5 stay different):
  1. own experience : after each closed trade, weights move toward signals that predicted the
                      outcome (online gradient on signal×net return)
  2. each other     : at each round end, trailing players blend 15% toward the leader's weights
  3. reflection     : Claude reviews the player's trades + news and writes a lesson; may nudge
                      entry threshold / exit multipliers within ±15%
  4. night school   : every night it replays the recent past (train.py) — thousands of what-if decisions,
                      an exit-rule sweep validated out-of-sample, and time-of-day / rule statistics
  5. recall         : before each decision it looks up its most similar past situations (signal-space
                      nearest neighbours, as in TradingAgents' situation memory) and leans away from
                      setups that lost before

Borrowed from published systems: FinRL/Qlib (periodic retraining on rolling history, train/validation
split), TradingAgents (reflection + memory retrieval), Freqtrade (StoplossGuard, per-symbol cooldown,
time-decaying take-profit "minimal ROI").
"""
import json
import math
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from . import llm

MARKET_TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}

SIGNALS = ["trend", "momentum", "reversion", "breakout", "news"]

PERSONAS = {
    # Internal ids (atlas, blaze, …) key all saved state, history, league records and positions; the display names
    # are shown in Korean everywhere (name_en = romanization). Rename them freely: only `name` is shown.
    # 대길 (vector) and 아귀 (blaze) are the two traders that can trade real money; the other three only play in the
    # hidden paper league that feeds learning.
    "atlas":  dict(name="한결", name_en="Hangyeol", name_ko="한결", style="Defensive value", color="#4F8EF7",
                   bio="Buys quality on dips, hates chasing. Patient, low turnover.",
                   w=dict(trend=0.25, momentum=0.05, reversion=0.45, breakout=0.0, news=0.25),
                   entry=0.35, tp_mult=1.2, sl_mult=0.8),
    "blaze":  dict(name="아귀", name_en="Agwi", name_ko="아귀", style="Momentum rider", color="#F2994A",
                   bio="Rides the strongest movers; cuts losers fast.",
                   w=dict(trend=0.35, momentum=0.45, reversion=0.0, breakout=0.1, news=0.1),
                   entry=0.30, tp_mult=1.0, sl_mult=0.8),
    "echo":   dict(name="보람", name_en="Boram", name_ko="보람", style="Mean reversion", color="#27AE60",
                   bio="Buys oversold bounces, sells back at the average.",
                   w=dict(trend=0.05, momentum=0.0, reversion=0.75, breakout=0.0, news=0.2),
                   entry=0.30, tp_mult=0.7, sl_mult=1.0),
    "herald": dict(name="새벽", name_en="Saebyeok", name_ko="새벽", style="News trader", color="#BB6BD9",
                   bio="Trades the headlines, confirms with price action.",
                   w=dict(trend=0.15, momentum=0.15, reversion=0.0, breakout=0.05, news=0.65),
                   entry=0.25, tp_mult=1.0, sl_mult=1.0),
    "vector": dict(name="대길", name_en="Dae-gil", name_ko="대길", style="Breakout hunter", color="#EB5757",
                   bio="Waits for new highs on volume, rides the expansion.",
                   w=dict(trend=0.2, momentum=0.2, reversion=0.0, breakout=0.5, news=0.1),
                   entry=0.35, tp_mult=1.4, sl_mult=0.9),
}


def signals(df: pd.DataFrame, p, news_score: float) -> dict | None:
    c = df["Close"].astype(float)
    if len(c) < p.slow + 2:
        return None
    ret = c.pct_change().dropna()
    vol = float(ret.tail(p.slow).std()) or 1e-4
    fast, slow = c.tail(p.fast).mean(), c.tail(p.slow).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    rsi = 100 - 100 / (1 + up / dn) if dn > 0 else 100
    prior_high = c.iloc[-(p.slow + 1):-1].max()
    sq = lambda x: math.tanh(x)  # squash to (-1, 1)
    day_chg = off_high = None    # how far it has already run today, and how close it is to today's high
    try:
        today = df[df.index.date == df.index[-1].date()]
        o, h = float(today["Open"].iloc[0]), float(max(today["High"].max(), c.iloc[-1]))
        if o > 0 and h > 0:
            day_chg, off_high = float(c.iloc[-1]) / o - 1, 1 - float(c.iloc[-1]) / h
    except (KeyError, IndexError, AttributeError, ValueError):
        pass
    rvol = None   # volume behind the move: this bar's volume vs the recent average (1.0 = normal)
    try:
        v = df["Volume"].astype(float)
        avg = float(v.iloc[-(p.slow + 1):-1].mean())
        if avg > 0 and float(v.iloc[-1]) > 0:
            rvol = float(v.iloc[-1]) / avg
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return {
        "trend": sq((fast / slow - 1) / (vol * math.sqrt(p.fast))),
        "momentum": sq((c.iloc[-1] / c.iloc[-p.mom - 1] - 1) / (vol * math.sqrt(p.mom))),
        "reversion": sq((50 - rsi) / 20),
        "breakout": sq((c.iloc[-1] / prior_high - 1) / vol),
        "news": float(news_score),
        "_rsi": float(rsi), "_vol": vol, "_price": float(c.iloc[-1]),
        "_day_chg": day_chg, "_off_high": off_high, "_rvol": rvol,
    }


def is_extended(sig, up=0.03, near=0.01) -> bool:
    """Already ran up today and sits at the high — these mostly faded (real-money post-mortem 2026-10-10)."""
    dc, oh = sig.get("_day_chg"), sig.get("_off_high")
    return dc is not None and oh is not None and dc >= up and oh <= near


def is_thin(sig) -> bool:
    """A price move with no volume behind it (momentum/breakout setups on < 70% of normal volume)."""
    rv = sig.get("_rvol")
    return rv is not None and rv < 0.7 and (sig.get("momentum", 0) > 0.3 or sig.get("breakout", 0) > 0.3)


RULES = {"ext": is_extended, "thin": is_thin}
RULE_WORDS = {"ext": "skip stocks already up ≥3% today at the high", "thin": "skip rallies on thin volume"}

# Coach lessons turned into code (2026-10-17). The Claude coach kept repeating these, but advice in a lesson never
# changed a buy. Each one is a SHADOW rule for one investor: every stock it would have skipped is tracked like the
# rules above, but it is only ENFORCED once the evidence says it helps — at least 40 skipped cases that went on to
# trail the index on average. Until then it is logged, never blocks a buy. Only new entries are ever affected.
SHADOW_RULES = {
    "bo_neg": (lambda s: s.get("breakout", 0) < 0, {"echo"}),                        # Echo: breakout ≥ 0
    "rev_neg": (lambda s: s.get("reversion", 0) < 0, {"vector"}),                    # Vector: reversion ≥ 0
    "chase": (lambda s: s.get("momentum", 0) > 0.8 and s.get("reversion", 0) < -0.8, {"herald"}),  # Herald: no chasing
    # Blaze's stop-losses cluster right after the open — track "skip the first 15 min" in shadow only
    "open15": (lambda s: s.get("_mins_open") is not None and s["_mins_open"] < 15, {"blaze"}),
}
SHADOW_OBSERVE_ONLY = {"open15"}   # recorded and reported, never enforced automatically — enforcing needs your OK
RULES.update({k: f for k, (f, _) in SHADOW_RULES.items()})
RULE_WORDS.update({"bo_neg": "only buy when breakout ≥ 0 (coach lesson)",
                   "rev_neg": "only buy when reversion ≥ 0 (coach lesson)",
                   "chase": "skip momentum > 0.8 with reversion < −0.8 (coach lesson)",
                   "open15": "skip buys in the first 15 min after the open (shadow)"})
SHADOW_MIN_N, SHADOW_HELPS = 40, -0.001   # enforce once ≥40 skipped cases trailed the index by ≥0.1% on average


SIGNAL_WORDS = {"trend": ("uptrend", "downtrend"), "momentum": ("strong momentum", "falling momentum"),
                "reversion": ("oversold — due a bounce", "overbought"), "breakout": ("breaking to new highs", "below recent highs"),
                "news": ("positive news", "negative news")}


REGIMES = ("bull", "bear", "choppy")


class Brain:
    """One AI investor. Keeps learning from:
      1. its own closed trades — judged on return BEYOND the market (index) after fees
      2. every decision it makes — shadow observations of the stocks it did and didn't buy are re-checked
         later, so it learns from missed opportunities and dodged losses, not only from trades
      3. the market regime (bull / bear / choppy) — separate learned weights for each kind of market
      4. per-stock memory — stocks that keep losing are avoided, reliable ones get a small bonus
      5. self-calibration — the buy threshold tightens when buys lose and loosens when near-misses win
      6. the league leader (imitation) and a Claude coach with memory of its past lessons
    All changes are bounded and anchored to the investor's personality, and learning slows as it matures."""

    def __init__(self, pid: str, state: dict | None = None):
        base = PERSONAS[pid]
        self.pid, self.persona = pid, base
        s = state or {}
        self.w = s.get("w", dict(base["w"]))
        self.w_r = s.get("w_r") or {r: dict(self.w) for r in REGIMES}   # weights per market regime
        # pickiness is bounded (2026-10-16): never below 60% or above 125% of the persona's base, however it was learned
        self.entry = min(max(s.get("entry", base["entry"]), base["entry"] * 0.6), base["entry"] * 1.25)
        self.tp_mult = s.get("tp_mult", base["tp_mult"])
        self.sl_mult = s.get("sl_mult", base["sl_mult"])
        self.lessons = s.get("lessons", [])
        self.history = s.get("history", [])        # closed trades
        self.generation = s.get("generation", 0)
        self.obs = s.get("obs", [])                # shadow observations waiting to be checked
        self.n_obs = s.get("n_obs", 0)             # how many have been learned from
        self.sym = s.get("sym", {})                # per-stock memory: n, wins, net
        self.streak = s.get("streak", 0)           # consecutive losses (negative = wins)
        self.calib = s.get("calib", {"above": [], "near": []})
        self.hours = s.get("hours", {})            # market → local hour → [n, sum of forward excess] of above-bar setups
        self.rules = s.get("rules", {})            # rule → [n, sum]: what the stocks a rule skips did afterwards
        self.n_train = s.get("n_train", 0)         # night-school decisions replayed so far
        self.train = s.get("train", {})            # last night-school summary (date, per market·horizon)
        self.memory = s.get("memory", [])          # recall bank: [sig vector..., excess] from real+shadow+replay

    def to_dict(self):
        return dict(w=self.w, w_r=self.w_r, entry=self.entry, tp_mult=self.tp_mult, sl_mult=self.sl_mult,
                    lessons=self.lessons[-30:], history=self.history[-300:], generation=self.generation,
                    obs=self.obs[-400:], n_obs=self.n_obs, sym=self.sym, streak=self.streak,
                    calib={k: v[-200:] for k, v in self.calib.items()}, hours=self.hours, rules=self.rules,
                    n_train=self.n_train, train=self.train, memory=self.memory[-self.MEMORY:])

    MEMORY = 1500          # situations it can recall
    RECALL_K = 12          # nearest past situations consulted per decision

    # ── recall: what happened the last times it saw a setup like this ──
    def remember(self, sig, excess):
        self.memory.append([round(sig.get(k, 0), 3) for k in SIGNALS] + [round(float(excess), 5)])
        if len(self.memory) > self.MEMORY + 200:
            del self.memory[:-self.MEMORY]

    def recall(self, sig) -> tuple[float, int]:
        """Mean forward excess of the K most similar past situations, and how many were close enough."""
        if len(self.memory) < 60:
            return 0.0, 0
        M = np.asarray(self.memory, dtype=float)
        v = np.array([sig.get(k, 0) for k in SIGNALS], dtype=float)
        d = np.sqrt(((M[:, :5] - v) ** 2).sum(axis=1))
        idx = np.argsort(d)[:self.RECALL_K]
        close = idx[d[idx] < 0.45]
        if len(close) < 6:
            return 0.0, int(len(close))
        return float(M[close, 5].mean()), int(len(close))

    def recall_adj(self, sig) -> float:
        avg, n = self.recall(sig)
        if n < 6:
            return 0.0
        if avg < -0.004:
            return -0.10        # setups like this have lost before → much harder to buy
        if avg > 0.004:
            return 0.03
        return 0.0

    # ── time of day and rule statistics (learned live and in night school) ──
    @staticmethod
    def local_hour(market, t) -> int | None:
        tz = MARKET_TZ.get(market)
        return datetime.fromtimestamp(t, tz).hour if tz else None

    def _bump(self, table, key, fwd):
        cell = table.setdefault(str(key), [0, 0.0])
        cell[0] += 1; cell[1] = round(cell[1] + float(fwd), 5)

    def _hour_cells(self, market):
        """Hours with enough data, each scored relative to the investor's own average hour
        (the overall level is handled by the buy-bar calibration; this is about WHEN)."""
        cells = {int(h): v for h, v in self.hours.get(market, {}).items() if v[0] >= 30}
        if len(cells) < 2:
            return {}
        n = sum(v[0] for v in cells.values())
        mean = sum(v[1] for v in cells.values()) / n
        return {h: v[1] / v[0] - mean for h, v in cells.items()}

    def hour_adj(self, market, hour) -> float:
        """Harder to buy in the hours of the day that do worst for it; a little easier in its best."""
        rel = self._hour_cells(market).get(hour) if hour is not None else None
        if rel is None:
            return 0.0
        return 0.05 if rel < -0.001 else (-0.02 if rel > 0.002 else 0.0)

    def rule_on(self, rule) -> bool:
        """A filter stays on unless the stocks it skipped clearly went on to beat the index (≥40 cases).
        A shadow rule (coach lesson) is the other way round: off until it has proven it would have helped."""
        n, s = self.rules.get(rule, [0, 0.0])
        if rule in SHADOW_OBSERVE_ONLY:
            return False
        if rule in SHADOW_RULES:
            return self.pid in SHADOW_RULES[rule][1] and n >= SHADOW_MIN_N and s / n <= SHADOW_HELPS
        return not (n >= 40 and s / n > 0.002)

    def shadow_block(self, sig):
        """The coach-lesson rules for THIS investor that fire on a stock: [(rule, enforced?)]."""
        return [(k, self.rule_on(k)) for k, (f, who) in SHADOW_RULES.items() if self.pid in who and f(sig)]

    def hours_summary(self, market):
        cells = self._hour_cells(market)
        if not cells:
            return None
        rank = sorted(cells, key=cells.get)
        return {"worst": rank[0], "best": rank[-1], "avoid": [h for h in rank if cells[h] < -0.001][:3]}

    # ── deciding ───────────────────────────────────────────
    def weights(self, regime=None):
        return self.w_r.get(regime) or self.w

    def score(self, sig: dict, regime=None, sym=None) -> float:
        w = self.weights(regime)
        return sum(w[k] * sig.get(k, 0) for k in SIGNALS) + self.sym_bias(sym) + self.recall_adj(sig)

    def explain(self, sig: dict, regime=None, sym=None) -> str:
        """Plain-words reason for a buy: the signals that pushed the score up most."""
        return ", ".join(self.reasons(sig, regime, sym)) or "weak mixed signals"

    def reasons(self, sig: dict, regime=None, sym=None) -> list:
        """The same reasons as explain(), one item each (strongest first) — for the trade alert's numbered list."""
        w = self.weights(regime)
        parts = sorted(((w[k] * sig.get(k, 0), k) for k in SIGNALS), reverse=True)
        words = [f"{SIGNAL_WORDS[k][0 if sig.get(k, 0) > 0 else 1]} ({sig.get(k, 0):+.2f})" for c, k in parts[:2] if c > 0.02]
        if sig.get("_day_chg") is not None:
            words.append(f"{sig['_day_chg']:+.1%} today, {sig['_off_high']:.1%} below today's high")
        if sig.get("_rvol") is not None:
            words.append(f"volume {sig['_rvol']:.1f}× normal")
        b = self.sym_bias(sym)
        if b:
            words.append("a stock it has done well on" if b > 0 else "a stock it usually loses on")
        avg, n = self.recall(sig)
        if n >= 6:
            words.append(f"{n} similar past setups averaged {avg:+.2%} vs the index")
        return words

    def sym_bias(self, sym) -> float:
        m = self.sym.get(sym) if sym else None
        if not m or m["n"] < 4:
            return 0.0
        wr, avg = m["wins"] / m["n"], m["net"] / m["n"]
        if wr < 0.3 and avg < 0:
            return -0.15          # keeps losing on this stock → avoid
        if wr > 0.6 and avg > 0:
            return 0.05
        return 0.0

    def size_mult(self) -> float:
        return 0.5 if self.streak >= 3 else 1.0   # humility after 3 losses in a row

    def exit_score(self, sig: dict, regime=None, sym=None) -> float:
        return self.score(sig, regime, sym)

    # ── learning ───────────────────────────────────────────
    def _lr(self, base):
        return base / (1 + (len(self.history) + self.n_obs / 20) / 200)   # slows down as it matures

    def _normalise(self, w):
        anchor = self.persona["w"]
        for k in SIGNALS:  # stay within persona ± 0.25 so the 5 styles never collapse into one
            w[k] = min(max(w[k], max(0.0, anchor[k] - 0.25)), anchor[k] + 0.25)
        tot = sum(w.values()) or 1
        for k in SIGNALS:
            w[k] = round(w[k] / tot, 4)

    def _update(self, sig, target, lr, regime):
        for w in (self.w_r.get(regime) or self.w, self.w) if regime in self.w_r else (self.w,):
            for k in SIGNALS:
                w[k] += lr * target * sig.get(k, 0)
            self._normalise(w)

    def learn_trade(self, entry_sig: dict, net_ret: float, symbol: str, excess: float | None = None, regime=None):
        """A closed trade. Judged on what it earned beyond the market over the same time, after fees."""
        judged = excess if excess is not None else net_ret
        self._update(entry_sig, math.tanh(judged / 0.02), self._lr(0.05), regime)
        m = self.sym.setdefault(symbol, {"n": 0, "wins": 0, "net": 0.0})
        m["n"] += 1; m["wins"] += net_ret > 0; m["net"] = round(m["net"] + net_ret, 5)
        self.streak = self.streak + 1 if net_ret <= 0 else min(self.streak, 0) - 1
        self.history.append({"symbol": symbol, "net_ret": round(net_ret, 5), "excess": round(judged, 5),
                             "regime": regime, "sig": {k: round(entry_sig.get(k, 0), 3) for k in SIGNALS}})
        self.remember(entry_sig, judged)

    def observe(self, t, regime, rows, cost, eval_after, bench=None, market=None):
        """Remember what it saw (top and bottom candidates) to check against what actually happened later."""
        for sym, price, sig, sc in rows:
            self.obs.append({"t": t, "due": t + eval_after, "sym": sym, "p": price, "r": regime, "sc": round(sc, 3),
                             "c": cost, "b": bench, "sig": {k: round(sig.get(k, 0), 3) for k in SIGNALS},
                             "m": market, "h": self.local_hour(market, t),
                             "rules": [k for k, f in RULES.items() if f(sig)]})
        if len(self.obs) > 600:
            del self.obs[:-500]

    def evaluate(self, prices, now, bench_now=None):
        """Shadow learning: for observations whose time has come, learn from the forward return."""
        due = [o for o in self.obs if o["due"] <= now and o["sym"] in prices]
        if not due:
            return 0
        keep = [o for o in self.obs if o not in due]
        for o in due:
            bench_ret = bench_now / o["b"] - 1 if o.get("b") and bench_now else 0.0
            fwd = prices[o["sym"]] / o["p"] - 1 - o["c"] - bench_ret      # after costs, beyond the market
            target = math.tanh(fwd / 0.02)
            relevance = 1.0 if o["sc"] >= self.entry else 0.5
            self._update(o["sig"], target, self._lr(0.01) * relevance, o["r"])
            bucket = "above" if o["sc"] >= self.entry else ("near" if o["sc"] >= self.entry - 0.15 else None)
            if bucket:
                self.calib[bucket].append(round(fwd, 5))
            if bucket == "above":
                self.remember(o["sig"], fwd)
                if o.get("m") and o.get("h") is not None:
                    self._bump(self.hours.setdefault(o["m"], {}), o["h"], fwd)
            for rule in o.get("rules", []):
                if rule in SHADOW_RULES and not bucket:
                    continue   # a coach lesson is judged only on stocks it would actually have bought (or nearly)
                self._bump(self.rules, rule, fwd)
            self.n_obs += 1
        self.obs = keep
        self._calibrate()
        return len(due)

    def _calibrate(self, a=None, n=None):
        """Tighten the buy threshold if what it buys loses; loosen it if near-misses would have won."""
        live = a is None
        a = self.calib["above"][-60:] if a is None else a
        n = self.calib["near"][-60:] if n is None else n
        if len(a) < 20 or len(n) < 20:
            return
        avg_a, avg_n = sum(a) / len(a), sum(n) / len(n)
        base = self.persona["entry"]
        if avg_a < 0:
            self.entry = min(self.entry + 0.01, base * 1.25)   # pickier, but never so picky it stops trading
        elif avg_n > 0 and avg_n > avg_a:
            self.entry = max(self.entry - 0.01, base * 0.6)
        if live:
            self.calib = {"above": a[-30:], "near": n[-30:]}

    # ── night school (see train.py) ────────────────────────
    def study(self, rows, market):
        """Learn from a replay of the recent past. rows: (regime, sig, score, hour, fwd_excess, rules_hit).
        One bounded mean-gradient step per regime (the whole night moves a weight ≤ 0.3), plus the
        time-of-day / rule tables, the calibration rule and the recall bank."""
        if not rows:
            return 0
        acc = {r: {k: 0.0 for k in SIGNALS} | {"_n": 0} for r in REGIMES}
        above, near = [], []
        for regime, sig, sc, hour, fwd, hit in rows:
            rel = 1.0 if sc >= self.entry else 0.5
            tgt = math.tanh(fwd / 0.02) * rel
            a = acc.get(regime) or acc["choppy"]
            for k in SIGNALS:
                a[k] += tgt * sig.get(k, 0)
            a["_n"] += 1
            if sc >= self.entry:
                above.append(fwd)
                if hour is not None:
                    self._bump(self.hours.setdefault(market, {}), hour, fwd)
            elif sc >= self.entry - 0.15:
                near.append(fwd)
            for rule in hit:
                if rule in SHADOW_RULES and sc < self.entry - 0.15:
                    continue   # coach lessons: judged only on stocks it would have bought (or nearly)
                self._bump(self.rules, rule, fwd)
        step = 0.3 / (1 + self.n_train / 20000)     # slows as it matures, like the live rates
        for r, a in acc.items():
            if a["_n"] >= 30 and r in self.w_r:
                for k in SIGNALS:
                    self.w_r[r][k] += step * a[k] / a["_n"]
                self._normalise(self.w_r[r])
        tot = sum(a["_n"] for a in acc.values())
        if tot >= 30:
            for k in SIGNALS:
                self.w[k] += step * sum(a[k] for a in acc.values()) / tot
            self._normalise(self.w)
        self._calibrate(above[-300:], near[-300:])
        stride = max(1, len(rows) // 300)         # the recall bank keeps a spread of the night's situations
        for regime, sig, sc, hour, fwd, hit in rows[::stride]:
            if sc >= self.entry - 0.15:
                self.remember(sig, fwd)
        self.n_train += len(rows)
        return len(rows)

    def fit_exits(self, grid_train: dict, grid_test: dict):
        """Exit-rule sweep. grid: (tp_mult, sl_mult) → [n, sum of excess]. The best rule on the older part
        of the history only counts if it also beats the current rule on the newest part (walk-forward),
        and even then the brain moves only a quarter of the way."""
        cur = (round(self.tp_mult, 2), round(self.sl_mult, 2))
        near = lambda g: min(g, key=lambda k: abs(k[0] - cur[0]) + abs(k[1] - cur[1]))
        ok = {k: v for k, v in grid_train.items() if v[0] >= 40}
        if not ok:
            return None
        best = max(ok, key=lambda k: ok[k][1] / ok[k][0])
        c_tr = near(grid_train)
        gain_tr = ok[best][1] / ok[best][0] - grid_train[c_tr][1] / max(1, grid_train[c_tr][0])
        c_te, b_te = near(grid_test) if grid_test else None, grid_test.get(best) if grid_test else None
        if not b_te or b_te[0] < 15 or not grid_test[c_te][0]:
            return {"best": best, "verdict": "not enough recent data to confirm"}
        gain_te = b_te[1] / b_te[0] - grid_test[c_te][1] / grid_test[c_te][0]
        if gain_tr < 0.002 or gain_te <= 0:
            return {"best": best, "verdict": "current exits are fine"}
        self.tp_mult = round(min(max(0.75 * self.tp_mult + 0.25 * best[0], 0.5), 2.5), 3)
        self.sl_mult = round(min(max(0.75 * self.sl_mult + 0.25 * best[1], 0.5), 2.0), 3)
        return {"best": best, "verdict": f"moved toward tp ×{best[0]} / stop ×{best[1]} "
                                         f"(+{gain_tr:.2%} older, +{gain_te:.2%} recent per trade)"}

    def imitate(self, leader: "Brain", rate=0.15):
        for k in SIGNALS:
            self.w[k] = (1 - rate) * self.w[k] + rate * leader.w[k]
        for r in REGIMES:
            if r in self.w_r and r in leader.w_r:
                for k in SIGNALS:
                    self.w_r[r][k] = (1 - rate) * self.w_r[r][k] + rate * leader.w_r[r][k]
                self._normalise(self.w_r[r])
        self.entry = (1 - rate) * self.entry + rate * leader.entry
        self._normalise(self.w)
        self.generation += 1

    def knowledge(self) -> dict:
        """What it has learned, for the app."""
        good = sorted((k for k, m in self.sym.items() if m["n"] >= 3), key=lambda k: -self.sym[k]["net"])
        return {"trades": len(self.history), "observations": self.n_obs, "pending_checks": len(self.obs),
                "streak": self.streak, "avoid": [k for k in self.sym if self.sym_bias(k) < 0][:6],
                "favours": [k for k in good if self.sym_bias(k) > 0][:6],
                "regime_weights": self.w_r, "trained": self.n_train, "memories": len(self.memory),
                "hours": {m: self.hours_summary(m) for m in ("US", "KR") if self.hours_summary(m)},
                "rules": {k: {"on": self.rule_on(k), "n": v[0], "avg": round(v[1] / v[0], 4) if v[0] else 0}
                          for k, v in self.rules.items()},
                "night_school": self.train.get("date")}

    def reflect(self, round_summary: dict, news_digest: dict, regimes: dict | None = None):
        """Claude coach with memory: sees its record, the market regime and its earlier lessons."""
        recent = self.history[-20:]
        wins = [h for h in recent if h["net_ret"] > 0]
        prompt = (
            f"You coach an AI stock trader named {self.persona['name']} ({self.persona['style']}). Be wise and "
            "conservative: protect capital first, profit second; never chase. Its goal is to BEAT simply holding the "
            "index (S&P 500 in the US, KOSPI 200 in Korea) after fees — at higher risk settings by a wider margin. "
            "Trades that earn less than the index are failures even when they make money.\n"
            f"Market regimes now: {json.dumps(regimes or {})}\n"
            f"Its weights by regime: {json.dumps(self.w_r)}; entry threshold {self.entry:.2f}; "
            f"take-profit mult {self.tp_mult:.2f}; stop mult {self.sl_mult:.2f}; losing streak {max(self.streak, 0)}.\n"
            f"Recent closed trades ({len(wins)}/{len(recent)} wins; 'excess' = beyond the index after fees): "
            f"{json.dumps(recent)}\n"
            f"Per-stock record: {json.dumps({k: v for k, v in list(self.sym.items())[-25:]})}\n"
            f"Shadow learning: {self.n_obs} past decisions re-checked; near-miss vs bought forward returns "
            f"{json.dumps({k: round(sum(v[-30:]) / max(1, len(v[-30:])), 4) for k, v in self.calib.items()})}\n"
            f"Night school (replay of recent history): {self.n_train} decisions studied; "
            f"last session {json.dumps(self.train, ensure_ascii=False)[:800]}\n"
            f"Hours of day (local) that lose it money: {json.dumps({m: (self.hours_summary(m) or {}).get('avoid') for m in ('US', 'KR')})}; "
            f"rule check (forward excess of what each rule skips): {json.dumps(self.rules)}\n"
            f"Its earlier lessons (oldest first): {json.dumps(self.lessons[-6:], ensure_ascii=False)}\n"
            f"League standings: {json.dumps(round_summary)}\n"
            f"News sentiment: {json.dumps(news_digest, ensure_ascii=False)[:1200]}\n"
            "Did its earlier lessons help? Write ONE new concise lesson (<=25 words) grounded in the data above, "
            "and suggest multipliers in [0.85,1.15] for entry, tp_mult, sl_mult. "
            "Return ONLY JSON: {\"lesson\": str, \"entry\": f, \"tp_mult\": f, \"sl_mult\": f}")
        try:
            r = llm.haiku(prompt, timeout=120)
            j = json.loads(re.search(r"\{.*\}", r.stdout, re.S).group(0))
        except Exception as e:
            return f"(reflection skipped: {e.__class__.__name__})"
        clip = lambda x: min(1.15, max(0.85, float(x)))
        base = self.persona
        self.entry = min(max(self.entry * clip(j.get("entry", 1)), base["entry"] * 0.6), base["entry"] * 1.25)
        self.tp_mult = min(max(self.tp_mult * clip(j.get("tp_mult", 1)), 0.5), 2.5)
        self.sl_mult = min(max(self.sl_mult * clip(j.get("sl_mult", 1)), 0.5), 2.0)
        self.lessons.append(j.get("lesson", ""))
        return j.get("lesson", "")
