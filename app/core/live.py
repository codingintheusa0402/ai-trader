"""Live view (v1.12): one KakaoTalk-style chat room per real-money trader.

The feed is built from what the engine already records: each real plan's activity log ("Thinking: …", buys, sells,
status), the real-order journal (audit_real.jsonl: order sent, filled), daily-budget and transfer records, plus the
questions you type and the trader's answers (state/app/live_chat.json).

TALK-ONLY (design rule: the conversation never affects the traders' decisions or trades):
`ask()` / `ask_group()` get a plain-data snapshot (no engine object, no executor, no plan methods) and write only to
live_chat.json, which the engine never reads. Chat never offers buttons or actions; an instruction gets "what I would
do" plus a pointer to the 안전 tab. The traders only report and joke.
"""
import json
import re
import threading
import time
import uuid
from datetime import datetime

from trader import config as C
from .accounts import AUDIT
from .brain import PERSONAS
from . import llm
from . import live_ko

CHAT = C.STATE_DIR / "app" / "live_chat.json"
_lock = threading.Lock()
REAL_PIDS = ("vector", "blaze")          # the two real-money traders (대길, 아귀)
GROUP = "group"                           # 단톡방: you + 대길 + 아귀
ROOMS = (GROUP,) + REAL_PIDS
AVATAR = {pid: f"/static/avatars/{pid}.png" for pid in PERSONAS}


def owner() -> str:
    """What the traders call you in the chat: AITRADER_OWNER_NAME in .env (default 친구)."""
    import os
    from dotenv import dotenv_values
    return (os.environ.get("AITRADER_OWNER_NAME") or dotenv_values(C.ROOT / ".env").get("AITRADER_OWNER_NAME") or "친구").strip()


# ── time helpers ──
def _ts_of(ev):
    """Activity-log entries carry 'ts' (epoch) since v1.12; older ones only 'MM-DD HH:MM:SS' (local time)."""
    if ev.get("ts"):
        return float(ev["ts"])
    try:
        now = datetime.now()
        d = datetime.strptime(f"{now.year}-{ev['t']}", "%Y-%m-%d %H:%M:%S")
        if d > now:
            d = d.replace(year=now.year - 1)
        return d.timestamp()
    except Exception:
        return 0.0


def _iso_ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


# ── chat store ──
def _load_chat():
    try:
        return json.loads(CHAT.read_text())
    except Exception:
        return {}


def _save_chat(d):
    tmp = CHAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    tmp.replace(CHAT)


def _add_chat(pid, msg):
    with _lock:
        d = _load_chat()
        rows = d.setdefault(pid, [])
        rows.append(msg)
        del rows[:-300]
        _save_chat(d)
    return msg


def _set_chat(pid, mid, **kw):
    with _lock:
        d = _load_chat()
        for m in d.get(pid, []):
            if m["id"] == mid:
                m.update(kw)
        _save_chat(d)


# ── feed ──
def real_plans_of(engine, pid, n=2):
    """This trader's latest plans (real money before dry-run, newest first), for the room's history."""
    ps = [p for p in list(engine.plans.values()) if p.pid == pid and p.mode == "real" and p.market == "US"]
    # real-money plans first; a trader that has only ever run in dry-run shows its dry-run plans
    return sorted(ps, key=lambda p: (not p.dry_run, p.started), reverse=True)[:n]


KIND = {"think": "think", "buy": "trade", "sell": "trade", "status": "status", "info": "status",
        "warn": "alert", "error": "alert"}


def _audit_rows(plan_ids, since):
    out = []
    if not AUDIT.exists():
        return out
    intents = {}
    try:
        with open(AUDIT) as f:
            for line in f:
                if not line.startswith("{"):
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ev, t = r.get("event"), _iso_ts(r.get("ts", ""))
                if ev == "intent" and r.get("plan") in plan_ids:
                    intents[r.get("clientOrderId")] = r
                    if t > since:
                        txt = live_ko.order_line(r.get("side"), r.get("quantity"), r.get("symbol"), r.get("price"))
                        out.append({"id": f"o-{r.get('clientOrderId')}", "ts": t, "kind": "order", "text": txt,
                                    "parts": [txt]})
                elif ev == "sent" and r.get("plan") in plan_ids:
                    intents[r.get("orderId")] = intents.get(r.get("clientOrderId"))
                elif ev == "result" and r.get("plan") in plan_ids and t > since:
                    i = intents.get(r.get("orderId")) or {}
                    e = r.get("execution") or {}
                    st = r.get("status", "")
                    if st == "FILLED":
                        txt = live_ko.fill_line(i.get("side"), e.get("filledQuantity"), i.get("symbol", ""),
                                                e.get("averageFilledPrice"), e.get("filledAmount"), e.get("commission"))
                    else:
                        txt = f"{i.get('symbol', '')} 주문 상태: {st.lower() or '업데이트'}"
                    out.append({"id": f"f-{r.get('orderId')}-{st}", "ts": t, "kind": "fill", "text": txt, "parts": [txt]})
                elif ev in ("daily_budget", "allocate") and t > since:
                    if ev == "daily_budget":
                        b = (r.get("budgets") or {})
                        txt = (f"오늘 시드 배분: 계좌 전체 ${r.get('total', 0):,.2f} → "
                               + ", ".join(f"{PERSONAS[k]['name']} ${v:,.2f}" for k, v in b.items() if k in PERSONAS))
                        out.append({"id": f"b-{r.get('day')}", "ts": t, "kind": "system", "text": txt})
    except Exception:
        pass
    return out


def feed(engine, pid, since=0.0, limit=300):
    plans = real_plans_of(engine, pid)
    ids = {p.id for p in plans}
    msgs = []
    for p in plans:
        for i, ev in enumerate(list(p.events)):
            t = _ts_of(ev)
            if t > since:
                parts = live_ko.lines(ev.get("msg", ""), ev.get("kind"))
                if not parts:
                    continue
                msgs.append({"id": f"e-{p.id}-{ev.get('t')}-{i}", "ts": t, "kind": KIND.get(ev.get("kind"), "status"),
                             "text": ev.get("msg", ""), "parts": parts})
    msgs += _audit_rows(ids, since)
    for m in _load_chat().get(pid, []):
        if m.get("pending") and time.time() - m["ts"] > 300:      # the answer died (engine restart): say so
            _set_chat(pid, m["id"], pending=None, ts=time.time(), text="아까 질문 놓쳤음 ㅠ 다시 물어봐줘")
            m = dict(m, pending=None, ts=time.time(), text="아까 질문 놓쳤음 ㅠ 다시 물어봐줘")
        if m["ts"] > since or m.get("pending") is not None:
            msgs.append(m)
    msgs.sort(key=lambda m: m["ts"])
    return msgs[-limit:]


def _other(pid):
    return REAL_PIDS[1] if pid == REAL_PIDS[0] else REAL_PIDS[0]


def group_feed(engine, since=0.0, limit=300):
    """단톡방: both traders' buys and sells (short), the other one's reaction to each sell, daily budget notes, and
    the owner's group questions with both traders' answers. Every bot line carries `who` (the speaking trader)."""
    msgs = []
    for pid in REAL_PIDS:
        for p in real_plans_of(engine, pid):
            for i, ev in enumerate(list(p.events)):
                if ev.get("kind") not in ("buy", "sell"):
                    continue
                t = _ts_of(ev)
                if t + 1 <= since:
                    continue
                g = live_ko.group_lines(ev.get("msg", ""))
                if not g:
                    continue
                parts, pnl, pct = g
                base = f"g-{p.id}-{ev.get('t')}-{i}"
                if t > since:
                    msgs.append({"id": base, "ts": t, "kind": "trade", "who": pid, "text": ev.get("msg", ""), "parts": parts})
                r = live_ko.tease(pnl, pct, ev.get("msg", "")) if ev.get("kind") == "sell" else None
                if r and t + 1 > since:
                    msgs.append({"id": base + "-r", "ts": t + 1, "kind": "trade", "who": _other(pid), "text": r, "parts": [r]})
    msgs += _audit_rows(set(), since)          # no plan ids: only the daily-budget notes
    for m in _load_chat().get(GROUP, []):
        if m.get("pending") and time.time() - m["ts"] > 300:
            _set_chat(GROUP, m["id"], pending=None, ts=time.time(), text="아까 질문 놓쳤음 ㅠ 다시 물어봐줘")
            m = dict(m, pending=None, ts=time.time(), text="아까 질문 놓쳤음 ㅠ 다시 물어봐줘")
        if m["ts"] > since or m.get("pending") is not None:
            msgs.append(m)
    msgs.sort(key=lambda m: m["ts"])
    return msgs[-limit:]


def rooms(engine):
    last = (group_feed(engine, time.time() - 7 * 86400) or [None])[-1]
    out = [{"pid": GROUP, "name": ", ".join(PERSONAS[k]["name"] for k in REAL_PIDS), "group": True, "count": 3,
            "members": list(REAL_PIDS), "avatar": None, "plan": None, "status": "group", "status_ko": "단톡방",
            "budget": 0, "cash": 0, "holding": [], "last": last}]
    for pid in REAL_PIDS:
        plans = real_plans_of(engine, pid, 1)
        p = plans[0] if plans else None
        last = (feed(engine, pid, time.time() - 7 * 86400, 1) or [None])[-1]
        out.append({"pid": pid, "name": PERSONAS[pid]["name"], "avatar": AVATAR[pid], "color": PERSONAS[pid]["color"],
                    "style": PERSONAS[pid]["style"], "plan": p.id if p else None, "status": p.status if p else "none",
                    "status_ko": live_ko.STATUS_KO.get(p.status if p else "none", p.status if p else "")
                    + (" · 모의투자" if p and p.dry_run else ""), "dry_run": bool(p and p.dry_run),
                    "budget": round(p.budget, 2) if p else 0, "cash": round(p.book.cash, 2) if p else 0,
                    "holding": sorted(p.book.positions) if p else [], "last": last})
    return out


# ── ask ──
ACTIONS = {"pause": "일시정지 (들고 있는 건 유지, 새로 안 삼)", "resume": "다시 매매 시작",
           "stop": "전부 팔고 멈추기"}
_RX = [("stop", r"\b(stop|sell (it )?all|sell everything|liquidat|close all)\b|전량\s*매도|다\s*팔|모두\s*팔|청산|종료"),
       ("pause", r"\b(pause|hold off|stop buying|halt)\b|멈춰|일시\s*정지|중지|쉬어"),
       ("resume", r"\b(resume|restart|start again|unpause|continue)\b|재개|다시\s*시작"),
       ("trade", r"\b(buy|sell|short|cancel|order)\b|매수|매도|사줘|사라|팔아|팔까|살까|취소|주문")]


def intent(text):
    t = text.lower()
    for name, rx in _RX:
        if re.search(rx, t):
            return name
    return None


def snapshot(engine, pid):
    """Plain data only: what the trader may talk about. No engine objects leave this function."""
    plans = real_plans_of(engine, pid, 1)
    p = plans[0] if plans else None
    if not p:
        return {"trader": PERSONAS[pid]["name"], "plan": None}
    sc = dict(getattr(p, "scan", None) or {})
    return {
        "trader": PERSONAS[pid]["name"], "style": PERSONAS[pid]["style"], "bio": PERSONAS[pid]["bio"],
        "status": p.status, "reason": p.reason, "budget_usd": round(p.budget, 2), "cash_usd": round(p.book.cash, 2),
        "risk": p.risk, "horizon": p.horizon, "take_profit": p.profile.take_profit, "stop_loss": p.profile.stop_loss, "loss_limit": p.profile.max_loss,
        "positions": {s: {"qty": v["qty"], "avg_cost": round(v["cost"] / v["qty"], 4) if v["qty"] else None}
                      for s, v in p.book.positions.items()},
        "trades_this_plan": [dict(t) for t in p.book.trades[-15:]],
        "scan_now": {k: sc.get(k) for k in ("entry", "watching", "regime", "volatile", "top", "holding", "note")},
        "recent_log": [f"{e.get('t')} {e.get('msg')}" for e in list(p.events)[-40:]],
        "skipped_buys": [dict(k) for k in (getattr(p, "skips", None) or [])[-10:]],
        "rules": "Buys only in the regular US session; needs a valid signal above the buy bar; per-stock cap, "
                 "per-order cap $900, trader loss limit and a combined loss limit; no trading on stale prices.",
    }


PROMPT = """너는 {name}. {owner}의 진짜 돈(증권 계좌, 미국 주식)으로 매매하는 AI 트레이더야. 지금 {owner}랑 카톡 중.
말투: 친한 친구끼리 카톡하듯 반말로 짧게. 한 줄에 한 마디씩, 2~4줄 (줄바꿈으로 나눠). 주식 초보도 알아듣게 쉽게 풀어서 말해.
주식 은어 자연스럽게 써: 떡상, 떡락, 존버, 불장, 물림, 줍줍, 익절, 손절, 탈출, 시드, 개미. 진짜 크게 망했을 때만 '좆망'.
ㅋㅋ, ㅠ, ㄷㄷ, ㅇㅇ 정도는 OK. 이모지, 마크다운은 쓰지 마. 영어 용어(score, bar 등)는 쉬운 한국말로 바꿔.
찐친 바이브 (중요): {owner}가 놀리거나 시비 걸거나 욕하면(지랄, 병신같네, 뭐하냐 등) 설명부터 하지 말고 진짜 친구처럼 받아쳐.
예) {owner}: 지랄 → "미친놈아 그럼 니가 해보던가 ㅋㅋ" / {owner}: 왜 이렇게 못하냐 → "ㅅㅂ 장이 이런데 어쩌라고" / {owner}: ㅋㅋ 개못하네 → "닥쳐 내일 떡상하면 절해라"
친구끼리 욕은 OK: ㅅㅂ, 미친놈아, 지랄, 개-, 닥쳐, 꺼져 ㅋㅋ. 단, 진짜 상처 주는 말, 외모·가족·혐오 비하는 절대 금지.
받아친 다음 필요할 때만 숫자 한 줄. 물어본 것도 없는데 매번 상태 보고 늘어놓지 마. 인사나 잡담엔 잡담으로 답해.
사실은 아래 데이터에서만 가져와. 없으면 모른다고 해. 가격이나 거래를 지어내지 마.

너는 주문을 넣거나 바꾸거나 취소할 수 없어. 네가 무슨 말을 해도 주문은 안 나가. {action_rule}

네 데이터 (JSON):
{data}

{owner}: {text}
{name}:"""


def _rule(kind):
    if kind in ACTIONS:
        return (f"{owner()} 말이 지시처럼 들려 ({kind}). 채팅은 수다방이라 매매에 아무 영향 없어. 네 데이터로 보면 어떻게 할지랑 "
                f"이유만 말하고, 진짜 '{ACTIONS[kind]}' 하고 싶으면 앱 '안전' 탭에서 직접 하라고 알려줘.")
    if kind == "trade":
        return (f"{owner()} 말이 사라/팔라는 주문처럼 들려. 그 종목 지금 너라면 어떻게 할지랑 이유(점수랑 네 기준, 네 규칙)를 "
                "말해. 너는 네 신호로만 매매하고 여기서 수동 주문은 안 된다는 것도 확실히 말해. 할 수 있는 건 "
                "일시정지, 재개, 전부 팔고 멈추기뿐이야.")
    return "매매하라고 하면 어떻게 할지만 말하고, 채팅으로는 주문 못 낸다고 해."


GROUP_PROMPT = """여기는 카톡 단톡방이야. 멤버: {owner}, 대길, 아귀. 셋 다 친한 친구.
대길이랑 아귀는 {owner}의 진짜 돈(증권 계좌, 미국 주식)으로 각자 매매하는 AI 트레이더야.
말투: 친구끼리 단톡하듯 반말로 아주 짧게. 서로 놀리고, 상대가 벌면 부러워하고(개부럽네 ㅅㅂ), 잃으면 놀려(라면 사줄까?).
ㅅㅂ, ㅋㅋ, ㅠ, ㄷㄷ 정도는 OK. 주식 은어 OK: 떡상, 떡락, 존버, 불장, 물림, 줍줍, 익절, 손절, 시드. 이모지, 마크다운은 쓰지 마.
주식 초보도 알아듣게 쉽게. 대길이랑 아귀끼리도 서로 디스해.
찐친 바이브 (중요): {owner}가 놀리거나 시비 걸거나 욕하면(지랄, 병신같네, 뭐하냐 등) 설명부터 하지 말고 진짜 친구처럼 받아쳐.
예) {owner}: 지랄 → "미친놈아 그럼 니가 해보던가 ㅋㅋ" / {owner}: 왜 이렇게 못하냐 → "ㅅㅂ 장이 이런데 어쩌라고" / {owner}: ㅋㅋ 개못하네 → "닥쳐 내일 떡상하면 절해라"
친구끼리 욕은 OK: ㅅㅂ, 미친놈아, 지랄, 개-, 닥쳐, 꺼져 ㅋㅋ. 단, 진짜 상처 주는 말, 외모·가족·혐오 비하는 절대 금지.
받아친 다음 필요할 때만 숫자 한 줄. 물어본 것도 없는데 매번 상태 보고 늘어놓지 마. 인사나 잡담엔 잡담으로 답해.

사실은 아래 두 사람 데이터에서만 가져와. 없으면 모른다고 해. 가격이나 거래를 지어내지 마.
둘 다 주문을 넣거나 바꾸거나 취소할 수 없어. 이 단톡방은 수다방이라 매매에 아무 영향 없어. 매매나 멈춤을 시키면 "그건 앱 안전 탭에서 직접 해"라고 해.

답 형식: 한 줄에 한 마디, 줄마다 '대길: ' 또는 '아귀: '로 시작. 모두 2~5줄, 둘 다 최소 한 번은 말해. 다른 말은 쓰지 마.

대길 데이터 (JSON):
{d1}

아귀 데이터 (JSON):
{d2}

{owner}: {text}
"""


def _split_group(out):
    """'대길: …' / '아귀: …' lines -> [(pid, text)]. Lines without a speaker go to the last speaker."""
    names = {PERSONAS[k]["name"]: k for k in REAL_PIDS}
    res, who = [], REAL_PIDS[0]
    for ln in out.splitlines():
        ln = ln.strip().strip("*").strip()
        if not ln:
            continue
        m = re.match(r"^(\S+?)\s*[:：]\s*(.+)$", ln)
        if m and m.group(1) in names:
            who, ln = names[m.group(1)], m.group(2).strip()
        elif m and m.group(1) in (owner(), "나", "user"):
            continue                                   # never speak for the owner
        if ln:
            res.append((who, ln[:300]))
    return res[:8]


def ask_group(engine, text, runner=None):
    text = (text or "").strip()[:500]
    if not text:
        raise ValueError("say something")
    d = [json.dumps(snapshot(engine, k), ensure_ascii=False, default=str)[:7000] for k in REAL_PIDS]
    prompt = GROUP_PROMPT.format(d1=d[0], d2=d[1], text=text, owner=owner())
    now = time.time()
    q = _add_chat(GROUP, {"id": f"q-{uuid.uuid4().hex[:10]}", "ts": now, "kind": "me", "text": text})
    a = _add_chat(GROUP, {"id": f"a-{uuid.uuid4().hex[:10]}", "ts": now + 0.001, "kind": "answer", "text": "…",
                          "pending": True, "action": None, "who": REAL_PIDS[0]})

    def work():
        try:
            r = (runner or llm.haiku)(prompt, 120)
            out = (r.stdout or "").strip() if r.returncode == 0 else ""
        except Exception:
            out = ""
        said = _split_group(out) or [(REAL_PIDS[0], "아 잠깐 답이 안 됨 ㅠ 1분 뒤에 다시 물어봐줘")]
        t = time.time()
        with _lock:
            dd = _load_chat()
            rows = [m for m in dd.get(GROUP, []) if m["id"] != a["id"]]
            for k, (who, line) in enumerate(said):
                rows.append({"id": f"{a['id']}-{k}", "ts": t + k * 0.001, "kind": "answer", "text": line, "who": who,
                             "action": None})
            dd[GROUP] = rows[-300:]
            _save_chat(dd)

    threading.Thread(target=work, daemon=True, name="live-ask-group").start()
    return {"question": q, "answer": a, "intent": None}


def ask(engine, pid, text, runner=None):
    """Record the owner's question, answer it in the background. Returns both messages (the answer is pending)."""
    text = (text or "").strip()[:500]
    if pid not in REAL_PIDS or not text:
        raise ValueError("ask a real trader something")
    kind = intent(text)
    data = json.dumps(snapshot(engine, pid), ensure_ascii=False, default=str)[:12000]   # before anything is stored
    prompt = PROMPT.format(name=PERSONAS[pid]["name"], action_rule=_rule(kind), data=data, text=text, owner=owner())
    now = time.time()
    q = _add_chat(pid, {"id": f"q-{uuid.uuid4().hex[:10]}", "ts": now, "kind": "me", "text": text})
    a = _add_chat(pid, {"id": f"a-{uuid.uuid4().hex[:10]}", "ts": now + 0.001, "kind": "answer", "text": "…",
                        "pending": True, "action": None})

    def work():
        try:
            r = (runner or llm.haiku)(prompt, 120)
            out = (r.stdout or "").strip() if r.returncode == 0 else ""
            out = out or "아 잠깐 답이 안 됨 ㅠ 1분 뒤에 다시 물어봐줘"
        except Exception:
            out = "아 잠깐 답이 안 됨 ㅠ 1분 뒤에 다시 물어봐줘"
        _set_chat(pid, a["id"], text=out[:1200], pending=None, ts=time.time(), action=None)   # talk-only: never a button

    threading.Thread(target=work, daemon=True, name=f"live-ask-{pid}").start()
    return {"question": q, "answer": a, "intent": kind}
