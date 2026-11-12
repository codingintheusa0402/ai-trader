"""Daily report to your own chat webhook (ALERT_WEBHOOK_URL in .env): what every AI investor traded and why,
how it went, and what each one learned. Sent once a day (07:30 KST, after the US close) by the engine itself.
Days with no trading and no learning are skipped. No webhook set = no report is sent."""
import json
import time
from datetime import datetime
from pathlib import Path

import requests

from trader import config as C
from . import data
from .brain import PERSONAS, SIGNALS, RULE_WORDS, SHADOW_RULES, SHADOW_OBSERVE_ONLY, SHADOW_HELPS
from .markets import CCY
from .alerts import plain, short_reason, usd, split

STATE = C.STATE_DIR / "app" / "report.json"
from .webhook import url as hook_url
SEND_AT = (7, 30)
LIMIT = 3800   # Chat text messages are split below this size and posted as one thread


def _load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def _ts(t):
    """Trade time: new trades carry ts; older ones only 'MM-DD HH:MM:SS'."""
    if t.get("ts"):
        return t["ts"]
    try:
        now = datetime.now()
        d = datetime.strptime(f"{now.year}-{t['time']}", "%Y-%m-%d %H:%M:%S")
        if d > now:
            d = d.replace(year=now.year - 1)
        return d.timestamp()
    except (KeyError, ValueError):
        return 0


def _money(v, ccy):
    sign = "-" if v < 0 else ""
    return f"{sign}₩{abs(v):,.0f}" if ccy == "KRW" else f"{sign}${abs(v):,.2f}"


def _hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def snap_brain(b):
    k = b.knowledge()
    return {"entry": round(b.entry, 3), "tp": round(b.tp_mult, 3), "sl": round(b.sl_mult, 3), "n_obs": b.n_obs,
            "trades": len(b.history), "lessons": len(b.lessons), "w_r": b.w_r, "avoid": k["avoid"], "favours": k["favours"]}


def round_trips(trades, since):
    """Each SELL in the window, paired with the BUY that opened it (and its reason)."""
    out, last_buy = [], {}
    for t in trades:
        if t["side"] == "BUY":
            last_buy[t["symbol"]] = t
        elif _ts(t) >= since:
            out.append((last_buy.get(t["symbol"]), t))
    return out


def plan_section(p, since, fees_too=True):
    ccy = CCY[p.market]
    lines = []
    for t in p.book.trades:
        if _ts(t) < since:
            continue
        if t["side"] == "BUY":
            lines.append(f"🟢 {_hm(_ts(t))} BUY {t['symbol']} ×{t['qty']:g} @ {t['price']:,.4g} — "
                         f"_{t.get('why', 'reason not recorded (older trade)')}_")
        else:
            lines.append(f"🔴 {_hm(_ts(t))} SELL {t['symbol']} ×{t['qty']:g} @ {t['price']:,.4g} — {t.get('reason', '')} "
                         f"→ *{t['pnl']:+,.2f}* ({t['net_ret']:+.2%} after fees)")
    sells = [t for t in p.book.trades if t["side"] == "SELL" and _ts(t) >= since]
    fees = sum(t.get("fee", 0) for t in p.book.trades if _ts(t) >= since)
    pnl = sum(t["pnl"] for t in sells)
    wins = sum(1 for t in sells if t["pnl"] > 0)
    vs = ""
    if p.bench_start and p.bench_last and p.status != "merged":
        idx = p.bench_last / p.bench_start - 1
        ret = p.book.net({}) / p.budget - 1 if not p.book.positions else None
        name = {"US": "S&P 500", "KR": "KOSPI 200"}[p.market]
        vs = (f"\nPlan {ret:+.2%} vs {name} {idx:+.2%} over the same time → *{ret - idx:+.2%}* "
              + ("✅ beat the index" if ret > idx else "❌ lost to the index")) if ret is not None \
            else f"\n{name} {idx:+.2%} since this plan started"
    head = (f"*{PERSONAS[p.pid]['name']}* · {p.profile.risk_name} · {p.profile.horizon_label} · {p.status}"
            f"{' (' + p.reason + ')' if p.reason else ''}\n"
            f"Closed P&L {_money(pnl, ccy)} · {wins}/{len(sells)} winning sells · fees {_money(fees, ccy)}"
            + (f" · still holding {', '.join(p.book.positions)}" if p.book.positions else "") + vs)
    return head, lines


def league_section(eng, since):
    out = []
    board = eng.leaderboard()
    out.append("*🏆 League (fake money — practice that never stops). The index is a player too: beat it or rank below it*")
    for i, r in enumerate(board, 1):
        if r.get("baseline"):
            out.append(f"{i}. 📊 *{r['name']}* (buy & hold) {r['ret']:+.2%}")
            continue
        out.append(f"{i}. {r['name']} {r['ret']:+.2%} total · {r['trades']} trades · {r['risk_name']}·{r['horizon_label']}")
    out.append("\n*vs the index (last 20 trading days) · ready for real money?*\n"
               "_Bar: 20 days + 40 trades, and beat the index after fees by +0% (Very Low/Low), +0.5% (Medium), "
               "+1% (High), +2% (Very High) — riskier settings must earn more than the index to be worth it._")
    for r in board:
        if r.get("baseline"):
            continue
        parts = []
        for m, v in r["vs"].items():
            rd = r["readiness"][m]
            parts.append(f"{m}: {v['ret']:+.2%} vs {v['name']} {v['index']:+.2%} → *{v['excess']:+.2%}* "
                         + ("✅ ready" if rd["ready"] else f"❌ ({rd['why']})"))
        out.append(f"• {r['name']}: " + (" | ".join(parts) or "no market days recorded yet"))
    for pid in PERSONAS:
        ps = [p for p in eng.plans.values() if p.league and p.pid == pid]
        trips = [(p, b, s) for p in ps for b, s in round_trips(p.book.trades, since)]
        if not trips:
            continue
        trips.sort(key=lambda x: _ts(x[2]))
        pnl = {}
        for p, _, s in trips:
            pnl[CCY[p.market]] = pnl.get(CCY[p.market], 0) + s["pnl"]
        wins = sum(1 for *_, s in trips if s["pnl"] > 0)
        out.append(f"\n*{PERSONAS[pid]['name']}* — {wins}/{len(trips)} wins · "
                   + " · ".join(_money(v, c) for c, v in pnl.items()))
        for p, b, s in trips[-12:]:
            bought = (b or {}).get("why", "")
            out.append(f"• {s['symbol']} {(b or {}).get('price', 0):,.4g}→{s['price']:,.4g} *{s['net_ret']:+.2%}* — "
                       f"sold: {s.get('reason', '')}" + (f" · bought: _{bought.split(';')[0]}_" if bought else ""))
        if len(trips) > 12:
            out.append(f"  …and {len(trips) - 12} earlier round trips")
    return out


def learning_section(eng, prev):
    out = ["*🧠 What they learned*"]
    for pid, b in eng.brains.items():
        now, old = snap_brain(b), (prev or {}).get(pid)
        name = PERSONAS[pid]["name"]
        if not old:   # first report: compare with the personality it started from
            base = PERSONAS[pid]
            old = {"entry": base["entry"], "tp": base["tp_mult"], "sl": base["sl_mult"], "n_obs": 0, "trades": 0,
                   "lessons": 0, "w_r": {r: dict(base["w"]) for r in now["w_r"]}, "avoid": [], "favours": []}
            name += " (since it started)"
        bits = []
        d_obs, d_tr = now["n_obs"] - old["n_obs"], now["trades"] - old["trades"]
        if d_obs or d_tr:
            bits.append(f"studied {d_obs} what-if checks and {d_tr} of its own trades")
        if abs(now["entry"] - old["entry"]) >= 0.005:
            bits.append(f"buy bar {old['entry']:.2f}→{now['entry']:.2f} "
                        f"({'pickier' if now['entry'] > old['entry'] else 'bolder'})")
        if abs(now["sl"] - old["sl"]) >= 0.005:
            bits.append(f"stop-loss {'tighter' if now['sl'] < old['sl'] else 'looser'} ×{old['sl']:.2f}→×{now['sl']:.2f}")
        if abs(now["tp"] - old["tp"]) >= 0.005:
            bits.append(f"profit target ×{old['tp']:.2f}→×{now['tp']:.2f}")
        shifts = sorted(((now["w_r"][r][k] - old["w_r"].get(r, {}).get(k, now["w_r"][r][k]), r, k)
                         for r in now["w_r"] for k in SIGNALS), key=lambda x: -abs(x[0]))
        for d, r, k in shifts[:2]:
            if abs(d) >= 0.01:
                bits.append(f"in {r} markets trusts {k} {'more' if d > 0 else 'less'} "
                            f"({old['w_r'][r][k]:.2f}→{now['w_r'][r][k]:.2f})")
        new_avoid = [s for s in now["avoid"] if s not in old["avoid"]]
        new_fav = [s for s in now["favours"] if s not in old["favours"]]
        if new_avoid:
            bits.append(f"now avoids {', '.join(new_avoid)}")
        if new_fav:
            bits.append(f"now favours {', '.join(new_fav)}")
        out.append(f"*{name}*: " + ("; ".join(bits) if bits else "no change (no market time)"))
        for lesson in b.lessons[old["lessons"]:][-2:] if now["lessons"] > old["lessons"] else []:
            out.append(f"   💡 _{lesson}_")
        cal = b.calib.get("above", [])[-30:]
        if len(cal) >= 10:
            out.append(f"   its above-the-bar picks moved {sum(cal) / len(cal):+.2%} on average after costs "
                       f"(last {len(cal)}, vs the index)")
        out += night_school_lines(b)
    return out


def night_school_lines(b):
    """What last night's replay of recent history taught this investor (train.py)."""
    t = b.train
    if not t or t.get("date") != datetime.now().strftime("%Y-%m-%d") or not t.get("n"):
        return []
    modes = [(k, v) for k, v in t.get("modes", {}).items() if "error" not in v]
    stocks = sum(v.get("stocks", 0) for k, v in modes)
    line = f"   🌙 night school: replayed {t['n']:,} decisions over {stocks} stocks ({', '.join(k for k, v in modes)})"
    bits = []
    ch = [v for k, v in modes if v.get("entry") and v["entry"].split("→")[0] != v["entry"].split("→")[1]]
    if ch:
        bits.append(f"buy bar {ch[0]['entry'].split('→')[0]}→{ch[-1]['entry'].split('→')[1]}")
    for k, v in modes:
        ex = v.get("exits") or {}
        if ex.get("verdict", "").startswith("moved"):
            bits.append(f"exits {ex['verdict']}")
            break
    for m, h in (t.get("hours") or {}).items():
        if h and h.get("avoid"):
            tz = "ET" if m == "US" else "KST"
            bits.append(f"{m}: pickier at {', '.join(f'{x}h' for x in h['avoid'])} {tz} (its worst hours)")
    for rule, v in (t.get("rules") or {}).items():
        if rule in SHADOW_RULES:   # coach lesson: off until proven
            if b.pid not in SHADOW_RULES[rule][1]:
                continue
            verdict = ((f"in shadow ({v['n']}/40 cases)" if v["n"] < 40 else
                        "would have HELPED — enforce it only with your OK" if v["avg"] <= SHADOW_HELPS else
                        "wouldn't have helped") if rule in SHADOW_OBSERVE_ONLY else
                       "now ENFORCED — the stocks it would have skipped trailed the index" if v["on"] else
                       f"in shadow ({v['n']}/40 cases)" if v["n"] < 40 else "stays in shadow — skipping wouldn't have helped")
            bits.append(f"coach rule '{RULE_WORDS[rule]}' {verdict} ({v['avg']:+.2%} vs index)")
            continue
        if v["n"] >= 40:
            verdict = ("switched OFF — the stocks it skipped went on to beat the index" if not v["on"] else
                       "confirmed — the stocks it skipped lost" if v["avg"] <= 0 else
                       "kept for now — skipped stocks gained a little, not decisive")
            bits.append(f"rule '{RULE_WORDS[rule]}' {verdict} ({v['avg']:+.2%} vs index, {v['n']} cases)")
    return [line] + [f"      · {x}" for x in bits]


def rules_section(eng, since):
    skips = [(p, k) for p in eng.plans.values() for k in p.skips if k["ts"] >= since]
    if not skips:
        return []
    q = data.quotes_prices({k["sym"] for _, k in skips})
    out = ["*🛡️ Safety rule check — buys it refused (\"already ran up today, at the high\")*"]
    moves = []
    for p, k in skips[-15:]:
        now = q.get(k["sym"])
        if not now:
            continue
        mv = now / k["px"] - 1
        moves.append(mv)
        out.append(f"• {PERSONAS[p.pid]['name']} {'(real) ' if p.mode == 'real' and not p.dry_run else ''}"
                   f"{k['sym']} @ {k['px']:,.4g} ({k['why']}) → now {now:,.4g} {mv:+.2%} "
                   + ("✅ skip was right" if mv < 0.003 else "❌ would have gained"))
    if moves:
        good = sum(1 for m in moves if m < 0.003)
        out.append(f"Rule was right {good}/{len(moves)} times; skipped stocks moved {sum(moves) / len(moves):+.2%} on average.")
    return out


def build(eng, since, prev_brains):
    msgs = []
    today = datetime.now()
    head = [f"*📈 AI Trader daily report — {today:%a %m-%d}*",
            f"_{datetime.fromtimestamp(since):%m-%d %H:%M} → {today:%m-%d %H:%M} KST_"]
    try:
        mon = {r["name"]: r for r in data.monitor()}
        head.append(" · ".join(f"{n} {mon[n]['change']:+.2%}" for n in ("S&P 500", "NASDAQ", "KOSPI", "KOSDAQ") if n in mon)
                    + (f" · USD/KRW {mon['USD/KRW']['price']:,.1f}" if "USD/KRW" in mon else ""))
        head.append("Market mood: " + ", ".join(f"{m} {eng.regime(m)['label']}" for m in ("US", "KR")))
    except Exception:
        pass
    msgs.append(head)

    real = [p for p in eng.plans.values() if p.mode == "real" and not p.dry_run
            and (p.status in eng.ACTIVE or any(_ts(t) >= since for t in p.book.trades))]
    sec = ["*💵 Real money*"]
    if not real:
        sec.append("No real-money plans traded. (Start one from the app when you want the AIs to use real money.)")
    for p in real:
        h, lines = plan_section(p, since)
        sec += ["", h] + (lines or ["no trades in this period"])
    try:
        from .accounts import TossExecutor
        if data.toss():
            ex = TossExecutor(data.toss())
            sec.append(f"\nToss cash: ${ex.buying_power('USD'):,.2f} · ₩{ex.buying_power('KRW'):,.0f}")
    except Exception:
        pass
    msgs.append(sec)

    msgs.append(league_section(eng, since))
    r = rules_section(eng, since)
    if r:
        msgs.append(r)
    msgs.append(learning_section(eng, prev_brains))
    return msgs


def _chunks(msgs):
    """Join sections into Chat-sized messages without splitting a line."""
    out, cur = [], ""
    for sec in msgs:
        for line in sec + [""]:
            if len(cur) + len(line) + 1 > LIMIT:
                out.append(cur.rstrip()); cur = ""
            cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip())
    return out


def had_activity(eng, since, prev):
    if any(_ts(t) >= since for p in eng.plans.values() for t in p.book.trades):
        return True
    return any(b.n_obs != (prev or {}).get(pid, {}).get("n_obs") for pid, b in eng.brains.items())


def realized_all():
    """Realized USD since the first real order: every engine fill from the order journal, minus what is still open."""
    from .accounts import AUDIT
    sent, cash, qty, cost = {}, 0.0, {}, {}
    try:
        rows = [json.loads(ln) for ln in open(AUDIT)]
    except OSError:
        return None
    intents = {r.get("clientOrderId"): r for r in rows if r.get("event") == "intent"}
    sent = {r.get("orderId"): r for r in rows if r.get("event") == "sent"}
    for r in rows:
        ex = r.get("execution") or {}
        if r.get("event") != "result" or not float(ex.get("filledQuantity") or 0):
            continue
        i = intents.get((sent.get(r.get("orderId")) or {}).get("clientOrderId")) or {}
        sym, side = i.get("symbol", ""), i.get("side")
        if not sym or sym.isdigit():
            continue
        q, amt = float(ex["filledQuantity"]), float(ex["filledAmount"])
        fees = float(ex.get("commission") or 0) + float(ex.get("tax") or 0)
        if side == "BUY":
            qty[sym] = qty.get(sym, 0) + q; cost[sym] = cost.get(sym, 0) + amt + fees
        else:
            share = cost.get(sym, 0) * q / qty[sym] if qty.get(sym) else 0
            cash += amt - fees - share
            qty[sym] = qty.get(sym, 0) - q; cost[sym] = cost.get(sym, 0) - share
    return cash


def profit_report(eng, since):
    """The 07:30 KakaoTalk report: plain text, split into 200-character messages. P&L is after trading fees. The trades
    are in USD, so they carry no FX cost; the KRW figure is today's Toss rate, for scale only."""
    plans = [p for p in eng.plans.values() if p.mode == "real" and not p.dry_run and p.market == "US"
             and (p.status in eng.ACTIVE or any(_ts(t) >= since for t in p.book.trades))]
    today, lines, day = datetime.now(), [], 0.0
    closed = sorted(((_ts(t), p, t) for p in plans for t in p.book.trades if t["side"] == "SELL" and _ts(t) >= since), key=lambda x: x[0])
    for _, p, t in closed:
        lines.append(f"{PERSONAS[p.pid]['name']} {t['symbol']} {usd(t['pnl'])} {short_reason(t.get('reason'))}")
        day += t["pnl"]
    if not closed:
        lines.append("No closed trades")
    active = [p for p in plans if p.status in eng.ACTIVE]
    cycle = sum(t["pnl"] for p in active for t in p.book.trades if t["side"] == "SELL")
    allt = realized_all()
    fx = next((m["price"] for m in data.monitor() if m["name"] == "USD/KRW"), None)
    lines.append(f"Day {usd(day)}" + (f" (about {'+' if day >= 0 else '-'}₩{abs(day) * fx:,.0f})" if fx else "")
                 + f" | Cycle {usd(cycle)}" + (f" | Since start {usd(allt)}" if allt is not None else ""))
    held = {s: p for p in active for s in p.book.positions}
    px = data.quotes_prices(set(held)) if held else {}
    lines.append("Open: " + (", ".join(f"{PERSONAS[p.pid]['name']} {s} {p.book.pos_net_ret(s, px[s]):+.1%}" if px.get(s) else f"{PERSONAS[p.pid]['name']} {s}"
                                       for s, p in held.items()) or "none"))
    vs = [f"{PERSONAS[p.pid]['name']} {p.book.net(px) / p.budget - 1:+.2%}" for p in active]
    idx = next(((p.bench_last / p.bench_start - 1) for p in active if p.bench_start and p.bench_last), None)
    lines.append((f"S&P {idx:+.2%} vs " if idx is not None else "") + ", ".join(vs))
    return split([f"Profit report {today:%m/%d}"] + lines)


def send(eng, force=False, dry=False, full=False):
    """Build and post the report. Returns the message texts. full=True builds the old long report (dry only)."""
    st = _load()
    since = st.get("last_ts") or time.time() - 86400
    prev = st.get("brains")
    if full:
        return [plain(t) for t in _chunks(build(eng, since, prev))]
    texts = [plain(t) for t in profit_report(eng, since)]   # plain text: works in any chat app
    if dry:
        return texts
    hook = hook_url()
    for i, t in enumerate(texts):
        if i:
            time.sleep(1.2)
        r = requests.post(hook, json={"text": t, "content": t}, timeout=15)
        r.raise_for_status()
    st.update(last_ts=time.time(), last_date=f"{datetime.now():%F}",
              brains={pid: snap_brain(b) for pid, b in eng.brains.items()})
    _save(st)
    return texts


def due():
    now = datetime.now()
    return (now.hour, now.minute) >= SEND_AT and _load().get("last_date") != f"{now:%F}" and bool(hook_url())
