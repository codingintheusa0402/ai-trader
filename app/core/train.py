"""Night school: while the markets are closed, every AI investor replays the recent past.

For each investment mode it is trading (its league mode plus any live plan's mode), on both markets:
  * step through the last weeks of bars for every stock in that mode's universe, compute the same
    signals it uses live, score them with its current weights and the market regime of that moment,
    and look up what the stock then did versus the index over the plan's horizon (after fees)
  * learn from all of it at once (Brain.study): weights per regime, buy-bar calibration, which hours
    of the day lose money, whether its skip-rules (extended / thin volume) actually help, and a bank
    of situations it can recall before a decision
  * sweep take-profit × stop-loss multipliers on the older 70 % of the history and keep the best only
    if it also beats the current rule on the newest 30 % (walk-forward, as FinRL/Qlib retrain)

Thousands of decisions a night instead of a handful of live trades — bounded by the same persona
anchors, so Blaze stays a momentum rider and Echo a mean-reverter.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from . import data, profiles, universe
from .brain import PERSONAS, RULES, signals
from .markets import FEES, SLIPPAGE, is_open

BENCH = {"US": "SPY", "KR": "069500"}
BAR_SECS = {"1m": 60, "5m": 300, "15m": 900, "60m": 3600, "1d": 86400}
GRID = [(tp, sl) for tp in (0.7, 0.85, 1.0, 1.15, 1.3, 1.5) for sl in (0.6, 0.8, 1.0, 1.2)]
MAX_PER_SYMBOL = 220       # decision points sampled per stock
START_KST = (6, 10)        # after the US close, before the 07:30 report and the 09:00 KR open
KST = ZoneInfo("Asia/Seoul")


def due(last_date: str) -> bool:
    now = datetime.now(KST)
    return (now.strftime("%Y-%m-%d") != last_date and (now.hour, now.minute) >= START_KST and now.hour < 8
            and not is_open("US", regular=True) and not is_open("KR", regular=True))


def regime_by_date(bench_daily) -> dict:
    """bull / bear / choppy for every date, from the index's own history (same rule as Engine.regime)."""
    px = bench_daily["Close"].astype(float)
    out = {}
    for i in range(51, len(px)):
        last, ma50, r20 = float(px.iloc[i]), float(px.iloc[i - 49:i + 1].mean()), float(px.iloc[i] / px.iloc[i - 20] - 1)
        out[px.index[i].date()] = "bull" if last > ma50 and r20 > 0 else "bear" if last < ma50 and r20 < 0 else "choppy"
    return out


def replay(brain, market, risk, horizon, uni_mode="large"):
    """One investor × one mode × one market. Returns (rows for Brain.study, exit grids, info)."""
    prof = profiles.make(risk, horizon)
    syms = universe.symbols(market, risk, uni_mode)
    bench = BENCH[market]
    bars = data.bars(set(syms) | {bench}, prof.bar, ttl=3600)
    if bench not in bars:
        return [], {}, {}, {"error": "no index bars"}
    bsec = BAR_SECS[prof.bar]
    intraday = bsec < 86400
    regimes = regime_by_date(data.bars([bench], "1d", ttl=3600)[bench])
    bclose = bars[bench]["Close"].astype(float)
    cost = FEES[market]["buy"] + FEES[market]["sell"] + FEES[market]["sell_tax"] + 2 * SLIPPAGE
    fwd_bars = max(2, min(int(prof.secs / bsec), 80 if intraday else 130))
    k2h = max(2, 7200 // bsec)
    min_exit = 2.5 * cost
    rows, grid_tr, grid_te = [], {g: [0, 0.0] for g in GRID}, {g: [0, 0.0] for g in GRID}
    stocks = 0
    for s in syms:
        df = bars.get(s)
        if df is None or len(df) < prof.slow + fwd_bars + 5:
            continue
        stocks += 1
        c = df["Close"].astype(float).to_numpy()
        ba = bclose.reindex(df.index, method="ffill").to_numpy()
        days = np.asarray([t.date() for t in df.index])
        n = len(df)
        split = int(n * 0.7)
        step = max(1, (n - prof.slow) // MAX_PER_SYMBOL)
        for i in range(prof.slow + 2, n - 2, step):
            if intraday:  # a day-or-shorter plan sells by the close: look only to the end of that day
                j_end = i
                while j_end + 1 < n and days[j_end + 1] == days[i] and j_end - i < fwd_bars:
                    j_end += 1
            else:
                j_end = min(i + fwd_bars, n - 1)
            if j_end - i < 2 or not ba[i] or not ba[j_end]:
                continue
            sig = signals(df.iloc[max(0, i - max(3 * prof.slow, 60)):i + 1], prof, 0.0)
            if not sig:
                continue
            R = regimes.get(days[i], "choppy")
            if intraday and i >= k2h and ba[i - k2h]:
                intra = ba[i] / ba[i - k2h] - 1
                if intra < -0.004:
                    R = "bear"
                elif R == "bull" and abs(intra) < 0.002:
                    R = "choppy"
            sc = brain.score(sig, R, s)
            bench_ret = ba[j_end] / ba[i] - 1
            fwd = c[j_end] / c[i] - 1 - cost - bench_ret
            hour = df.index[i].hour if intraday else None
            rows.append((R, sig, sc, hour, fwd, [k for k, f in RULES.items() if f(sig)]))
            entry = brain.entry + {"bull": 0.0, "choppy": 0.04, "bear": 0.08}[R]
            if sc >= entry:   # it would have bought: how would each exit rule have done?
                path = c[i + 1:j_end + 1] / c[i] - 1
                grid = grid_tr if i < split else grid_te
                for tp_m, sl_m in GRID:
                    tp, sl = max(prof.take_profit * tp_m, min_exit), max(prof.stop_loss * sl_m, min_exit)
                    hit = np.flatnonzero((path >= tp) | (path <= -sl))
                    if len(hit):
                        k = hit[0]
                        r = tp if path[k] >= tp else -sl
                        b = ba[i + 1 + k] / ba[i] - 1
                    else:
                        r, b = path[-1], bench_ret
                    cell = grid[(tp_m, sl_m)]
                    cell[0] += 1; cell[1] += r - cost - b
    info = {"stocks": stocks, "decisions": len(rows), "would_buy": sum(v[0] for v in grid_tr.values()) // len(GRID)
            + sum(v[0] for v in grid_te.values()) // len(GRID)}
    return rows, grid_tr, grid_te, info


def modes_for(eng, pid):
    """The investment modes this investor is actually trading: league mode + any active plan's mode."""
    pc = eng.player_cfg(pid)
    modes = {(pc["risk"], pc["horizon"], pc.get("universe", "large"))}
    for p in list(eng.plans.values()):
        if p.pid == pid and not p.league and p.status in ("running", "waiting", "paused"):
            modes.add((p.risk, p.horizon, p.universe))
    return sorted(modes)


def run(eng, pids=None, modes=None) -> dict:
    """Night school for every investor. Returns a summary and stores it on each brain (brain.train)."""
    date = datetime.now(KST).strftime("%Y-%m-%d")
    out = {"date": date, "players": {}}
    for pid in pids or PERSONAS:
        brain = eng.brains[pid]
        t0 = time.time()
        summary = {"date": date, "modes": {}, "n": 0}
        for risk, horizon, uni in modes or modes_for(eng, pid):
            for market in ("US", "KR"):
                key = f"{market}·{profiles.RISK[risk]['name']}·{profiles.HORIZON[horizon]['label']}"
                try:
                    rows, g_tr, g_te, info = replay(brain, market, risk, horizon, uni)
                    if not rows:
                        summary["modes"][key] = info or {"error": "no data"}
                        continue
                    before = (brain.entry, brain.tp_mult, brain.sl_mult)
                    brain.study(rows, market)
                    fit = brain.fit_exits(g_tr, g_te)
                    if fit:
                        fit["best"] = list(fit["best"])
                    above = [r[4] for r in rows if r[2] >= brain.entry]
                    info.update(exits=fit, above_bar_avg=round(float(np.mean(above)), 4) if above else None,
                                entry=f"{before[0]:.2f}→{brain.entry:.2f}", tp=f"×{before[1]:.2f}→×{brain.tp_mult:.2f}",
                                sl=f"×{before[2]:.2f}→×{brain.sl_mult:.2f}")
                    summary["modes"][key] = info
                    summary["n"] += len(rows)
                except Exception as e:
                    summary["modes"][key] = {"error": f"{e.__class__.__name__}: {e}"[:160]}
        summary["secs"] = round(time.time() - t0, 1)
        summary["hours"] = {m: brain.hours_summary(m) for m in ("US", "KR") if brain.hours_summary(m)}
        summary["rules"] = {k: {"on": brain.rule_on(k), "n": v[0], "avg": round(v[1] / v[0], 4) if v[0] else 0}
                            for k, v in brain.rules.items()}
        brain.train = summary
        out["players"][pid] = summary
    eng.train_date = date
    eng.save()
    return out
