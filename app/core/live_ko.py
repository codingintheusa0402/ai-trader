"""Live view: the engine's English log lines -> short, casual Korean chat lines (친구끼리 카톡 말투, 반말, 주식 은어: 떡상/떡락/존버/불장/물림/줍줍/익절/손절).

Display only. The engine keeps writing English (alerts, reports and QA depend on it); the Live page shows each event
as a few short bubbles, the way a friend texts. No emoji; strong words (좆망) only for really bad moments. Anything not recognised is shown as it is.
"""
import re
import zlib

MOOD = {"bull": "오늘 불장임. 분위기 좋음", "bear": "하락장임. 분위기 별로", "choppy": "장이 갈팡질팡함 (오르락내리락)"}
STATUS_WORD = {
    "below my bar": None,
    "close to my bar, watching it": "거의 다 옴, 보는 중",
    "already ran up today": "오늘 이미 많이 올라서 패스",
    "rally on thin volume": "거래량 없이 오른 거라 패스",
    "inverse, but the market isn't falling": "인버스인데 장이 안 빠져서 패스",
    "inverse, but it already holds stocks (no self-hedging)": "주식 들고 있어서 인버스는 안 삼",
}


def _n(x):
    """'+0.18' -> '+0.18', '0.46' -> '0.46' (scores); '$1,456' stays."""
    return x


def _money(x):
    try:
        return f"${float(str(x).replace(',', '').replace('$', '')):,.2f}"
    except ValueError:
        return f"${x}"


def _qty(q):
    try:
        f = float(q)
        return f"{int(f)}" if f == int(f) else f"{f:g}"
    except ValueError:
        return q


def _think_part(t):
    t = t.strip()
    m = re.match(r"holding (\S+) ([-+][\d.]+%) \(sell at \+([\d.]+)% or -([\d.]+)%\)(?: — (.*))?$", t)
    if m:
        sym, ret, tp, sl, extra = m.groups()
        r = float(ret.rstrip("%"))
        feel = ("떡상 중 ㅋㅋ" if r >= 3 else "조금 오르는 중" if r > 0.3 else "물려있음 ㅠ" if r <= -2 else
                "살짝 물림" if r < -0.3 else "거의 본전")
        out = [f"{sym} 들고 있음 {ret}, {feel}", f"+{tp}% 오면 익절(수익 확정), -{sl}% 가면 손절(손실 끊기)"]
        if extra and "trailing" in extra:
            out.append("수익 난 거 안 뺏기게 따라가면서 지키는 중")
        elif extra and "weakening" in extra:
            out.append("근데 힘이 좀 빠짐. 지켜보는 중")
        else:
            out.append("일단 존버")
        return out
    m = re.match(r"market (bull|bear|choppy)( and jumpy)?, my buy bar is ([\d.]+)$", t)
    if m:
        mood, jumpy, bar = m.groups()
        return [MOOD[mood] + (", 출렁임도 심함" if jumpy else ""), f"나는 {bar}점 넘는 종목만 삼 (점수 = 지금 살 만한 정도)"]
    m = re.match(r"best now: (.*)$", t)
    if m:
        items = re.findall(r"(\S+) ([-+][\d.]+) \(([^)]*\)?)\)", m.group(1))
        if not items:
            return [t]
        names = ", ".join(f"{s} {sc}점" for s, sc, _ in items)
        out = [f"지금 제일 나은 건 {names}"]
        notes = [f"{s}: {STATUS_WORD[st]}" for s, _, st in items if STATUS_WORD.get(st)]
        if all(st == "below my bar" for _, _, st in items):
            out.append("근데 다 기준 못 넘음. 안 삼")
        out += notes
        return out
    if t == "nothing good enough yet":
        return ["아직 살 만한 거 없음", "괜히 들어갔다 물리느니 기다림"]
    if t == "nothing to consider right now":
        return ["지금은 볼 게 없음"]
    if t == "no stock has enough price data yet":
        return ["아직 차트 데이터가 부족해서 판단 보류"]
    m = re.match(r"(swing: )?buys only in the regular session, not in its last (\d+) min( — flat by the close)?$", t)
    if m:
        swing, mins, flat = m.groups()
        if swing or flat:
            return ["정규장(밤 11:30~)에서만 사고", f"마감 {mins}분 전부턴 안 삼. 장 끝나기 전에 다 털고 나옴"]
        return ["정규장(밤 11:30~)에서만 삼", f"마감 {mins}분 전부턴 안 삼"]
    if t.startswith("last stretch of the plan"):
        return ["이번 판 막바지라 새로 안 사고", "들고 있는 것만 관리 중"]
    m = re.match(r"going in: (\d+) stocks? cleared my bar, budget split (\d+) ways?$", t)
    if m:
        return [f"{m.group(1)}개 기준 넘었음", "줍줍 들어간다"]
    m = re.match(r"(\d+) stop-losses recently, pausing new buys$", t)
    if m:
        return [f"최근에 손절 {m.group(1)}번 했음..", "멘탈 정비 좀 하고 잠깐 쉼"]
    return [t]


def _reason_sell(r):
    m = re.match(r"stop loss ([-+][\d.]+%)", r)
    if m:
        return f"떡락해서 손절 라인 닿음. 더 물리기 전에 탈출 ({m.group(1)})"
    m = re.match(r"take profit ([-+][\d.]+%)", r)
    if m:
        return f"목표가 도착 ㅋㅋ 익절 ({m.group(1)})"
    m = re.match(r"signal turned \(([-+][\d.]+)\) at ([-+][\d.]+%)", r)
    if m:
        return f"흐름 꺾여서 미련 없이 정리 (그때 {m.group(2)})"
    m = re.match(r"trailing stop ([-+][\d.]+%) \(peak ([-+][\d.]+%)\)", r)
    if m:
        return f"고점 {m.group(2)} 찍고 밀리길래 {m.group(1)}에서 수익 챙김"
    if r.startswith("swing: flat before the close"):
        return "장 끝나기 전이라 털고 나옴 (밤새 들고 안 있음)"
    if r.startswith("month-end"):
        return "월말이라 싹 정리함"
    if "side switch" in r:
        return "장 방향 바뀌어서 갈아탐"
    return r


def _reason_buy(r):
    out = []
    for part in [x.strip() for x in r.split(",")]:
        if part.startswith("breaking to new highs"):
            out.append("신고가 뚫는 중 (떡상 각)")
        elif part.startswith("strong momentum"):
            out.append("상승세 강함")
        elif re.match(r"[-+][\d.]+% today", part):
            out.append("오늘 " + part.split()[0])
        elif re.match(r"volume [\d.]+× normal", part):
            out.append("거래량 평소 " + part.split()[1].replace("×", "배"))
        elif "below today's high" in part:
            out.append("오늘 고점에서 " + part.split()[0] + " 아래")
        elif re.match(r"\d+ similar past setups averaged", part):
            m = re.match(r"(\d+) similar past setups averaged ([-+][\d.]+%)", part)
            if m:
                out.append(f"비슷한 판 {m.group(1)}번 평균 {m.group(2)}")
        if len(out) >= 3:
            break
    return ", ".join(out)


# MZ reaction after a sell: profit -> 개이득/야르/개꿀, loss -> 삼시세끼 라면. Picked by a hash of the line, so the
# same trade always shows the same words (the page re-renders every few seconds). Live page only, never KakaoTalk.
HYPE_SMALL = ["소소하게 익절 ㅋ", "티끌 모아 태산 ㅋ", "작아도 수익은 수익 ㅋ"]
HYPE_WIN = ["개이득~", "야르~", "개꿀~", "개꿀이쥬?"]
HYPE_BIG = ["개이득~ 떡상 먹었다", "야르~ 오늘 치킨 각", "개꿀이쥬? 이 맛에 주식함"]
OOF_SMALL = ["아깝다.. 다음 판에 복구함", "이 정도는 수업료임"]
OOF = ["내일부터 삼시세끼 라면 각이다", "내일부터 삼시세끼 라면 각이다.. 계란은 넣자", "조졌다 ㅅㅂ"]
OOF_BIG = ["내일부터 삼시세끼 라면 각이다 ㅠ", "조졌다 ㅅㅂ", "한강 수온 체크해야겠다"]


def _hype(pnl, pct, key):
    if abs(pnl) < 0.005:
        return None
    pool = ((HYPE_BIG if pct >= 2 else HYPE_WIN if pct >= 0.5 else HYPE_SMALL) if pnl > 0 else
            (OOF_BIG if pct <= -3 else OOF if pct <= -1 else OOF_SMALL))
    return pool[zlib.crc32(key.encode()) % len(pool)]


# 단톡방: 대길, 아귀 and you are friends. After one trader's sell the other one reacts: envy on a win
# (개부럽네 ㅅㅂ), teasing on a loss. Same hash rule as _hype so a line never changes between refreshes.
TEASE_WIN = ["개부럽네 ㅅㅂ", "운빨 ㅇㅈ?", "ㅋㅋ 한턱 쏴라", "오늘 치킨은 니가 사는 걸로", "나도 그거 볼걸 ㅅㅂ"]
TEASE_BIGWIN = ["개부럽네 ㅅㅂ 진짜", "미쳤네 ㄷㄷ 소고기 사라", "ㅅㅂ 나만 못 먹었네"]
TEASE_LOSS = ["ㅋㅋㅋㅋ 라면 사줄까?", "그러게 그거 사지 말랬지 ㅋ", "수업료 잘 냈다 ㅋㅋ", "존버하지 그랬냐 ㅋ"]
TEASE_BIGLOSS = ["ㅋㅋㅋ 한강 가지 마라", "라면 박스로 보내줄게 ㅋㅋ", "괜찮아 나도 언젠간 그럴 듯.. 아마도"]


def tease(pnl, pct, key):
    if abs(pnl) < 0.005:
        return None
    pool = ((TEASE_BIGWIN if pct >= 2 else TEASE_WIN) if pnl > 0 else (TEASE_BIGLOSS if pct <= -3 else TEASE_LOSS))
    return pool[zlib.crc32(("t" + key).encode()) % len(pool)]


def group_lines(msg):
    """A buy/sell log line -> (short lines for the group room, pnl, pct); None for anything else."""
    m0 = msg.strip()
    m = re.match(r"BUY (\S+) ×([\d.]+) @ ([\d.]+)", m0)
    if m:
        sym, q, px = m.groups()
        return [f"{sym} {_qty(q)}주 줍줍함 ({_money(px)})"], 0.0, 0.0
    m = re.match(r"SELL (\S+) ×([\d.]+) @ ([\d.]+) — (.*?) — P&L ([-+]?[\d.]+) \(([-+][\d.]+%) net\)", m0)
    if m:
        out = lines(m0)
        return [out[0]] + out[2:], float(m.group(5)), float(m.group(6).rstrip("%"))
    return None


def lines(msg, kind=""):
    """One engine log line -> a list of short Korean chat lines."""
    m0 = msg.strip()
    if m0.startswith("Thinking: "):
        out = []
        for part in m0[len("Thinking: "):].split("; "):
            out += _think_part(part)
        return out or [m0]
    m = re.match(r"Started \S+ · .*?· ([\d,.]+) (USD|KRW)", m0)
    if m:
        money = f"${m.group(1)}" if m.group(2) == "USD" else f"{m.group(1)}원"
        if "DRY-RUN" in m0:
            return ["모의투자 시작함", f"시드 {money} (가짜 돈이라 주문은 안 나감)"]
        return ["실전 시작함 (진짜 돈)", f"시드 {money}"]
    m = re.match(r"BUY (\S+) ×([\d.]+) @ ([\d.]+) \(score ([-+][\d.]+), fee ([\d.]+)\)(?: — (.*?))?(?:; score.*)?$", m0)
    if m:
        sym, q, px, sc, fee, why = m.groups()
        out = [f"{sym} {_qty(q)}주 줍줍함 ({_money(px)})", f"점수 {sc}로 기준 넘음"]
        if why:
            w = _reason_buy(why)
            if w:
                out.append(w)
        return out
    m = re.match(r"SELL (\S+) ×([\d.]+) @ ([\d.]+) — (.*?) — P&L ([-+]?[\d.]+) \(([-+][\d.]+%) net\)", m0)
    if m:
        sym, q, px, why, pnl, pct = m.groups()
        p = float(pnl)
        q_ = float(pct.rstrip("%"))
        res = (f"+{_money(p)} ({pct}) 먹음 ㅋㅋ" if p > 0 else
               f"-{_money(-p)} ({pct}) 날림.. 좆망 ㅠ" if q_ <= -3 else
               f"-{_money(-p)} ({pct}) 손해 ㅠ" if p < 0 else "본전 ㅇㅇ")
        out = [f"{sym} {_qty(q)}주 팔았음 ({_money(px)})", _reason_sell(why), res]
        hype = _hype(p, q_, m0)
        return out + [hype] if hype else out
    m = re.match(r"blocked (BUY|SELL) (\S+): (insufficient plan cash|cooldown|market closed|per-order cap.*|"
                 r"too large for this stock's daily liquidity|price collar.*)$", m0)
    if m:
        why = {"insufficient plan cash": "시드가 모자라서", "cooldown": "방금 판 종목이라 좀 쉬는 중",
               "market closed": "장이 닫혀서", "too large for this stock's daily liquidity": "거래량 적은 종목이라 너무 큰 주문"}
        r = m.group(3)
        w = why.get(r) or ("한 번 주문 한도 넘어서" if r.startswith("per-order") else "가격이 갑자기 튀어서")
        return [f"{m.group(2)} {'사려' if m.group(1) == 'BUY' else '팔려'}다 말았음", w]
    m = re.match(r"blocked (BUY|SELL) (\S+): stale quote", m0)
    if m:
        return [f"{m.group(2)} {'사려' if m.group(1) == 'BUY' else '팔려'}고 했는데", "시세가 늦게 들어와서 일단 보류함"]
    m = re.match(r"(?:HALTED: )?combined loss limit: (\$[\d,]+) is below (\$[\d,]+)", m0)
    if m:
        return ["좆망각.. 전체 손실한도 걸림", f"지금 {m.group(1)}인데 버틸 수 있는 선이 {m.group(2)}임", "그래서 다 멈춤. 다시 켜줘야 움직임"]
    if m0.startswith("HALTED:"):
        r = m0[len("HALTED:"):].strip()
        if "401" in r and "oauth2/token" in r:
            return ["토스 로그인이 막힘 (401)", "돈 걸린 거라 일단 멈춰놨음", "새 키 들어오면 다시 움직임"]
        if "Toss API unavailable" in r:
            return ["토스 서버랑 연결이 안 됨", "안전하게 일단 멈춤"]
        if "KILL SWITCH" in r:
            return ["킬스위치 눌림", "전부 멈춤"]
        return ["멈춤", r[:120]]
    if m0 == "US market closed — waiting":
        return ["미장 끝 ㅂㅂ", "다음 장 열릴 때까지 쉼"]
    if m0 == "US market open — trading":
        return ["미장 열림 (아직 프리장)", "본게임은 정규장(밤 11:30)부터"]
    if m0.startswith("RESUMED by you"):
        return ["다시 시작함 ㄱㄱ"]
    if m0.startswith("PAUSED"):
        return ["일시정지함", "들고 있는 건 그대로 둠"]
    m = re.match(r"DONE: (.*)$", m0)
    if m:
        r = m.group(1)
        mm = re.search(r"([-+][\d.]+%) net", r)
        return ["이번 판 끝"] + ([f"결과 {mm.group(1)}"] if mm else [])
    m = re.match(r"learned to be pickier: buy threshold raised to ([\d.]+)", m0)
    if m:
        return ["좀 더 깐깐하게 고르기로 함", f"매수 기준 {m.group(1)}점으로 올림"]
    m = re.match(r"Market call today: (\S+)", m0)
    if m:
        return [f"오늘 장 판단: {'상승/보합' if 'up' in m.group(1) else '하락'}"]
    m = re.match(r"Out of my price range: (\S+)", m0)
    if m:
        return [f"{m.group(1)}는 한 주가 너무 비싸서 패스"]
    m = re.match(r"StoplossGuard: (\d+) stop-losses", m0)
    if m:
        return [f"손절 {m.group(1)}번 연속이라", "잠깐 새로 안 삼"]
    m = re.match(r"budget reset to ([\d.,]+) USD", m0)
    if m:
        return [f"시드 {_money(m.group(1))}로 맞춤"]
    m = re.match(r"SETTINGS changed: budget ([\d.,]+) USD", m0)
    if m:
        return [f"시드 바뀜: {_money(m.group(1))}"]
    m = re.match(r"\$([\d,.]+) of unallocated cash added, budget now \$([\d,.]+)", m0)
    if m:
        return [f"{_money(m.group(1))} 시드 추가 받음 ㅋㅋ", f"이제 내 시드 {_money(m.group(2))}"]
    if m0.startswith("last trading day of the month"):
        return ["오늘이 이번 달 마지막 장이라", "마감 전에 다 팔고 다음 달 새로 시작함"]
    if m0.startswith("PACE:"):
        return []                       # mock-only goal pacing: not shown in the Live view
    return [m0]


def order_line(side, qty, sym, price):
    return f"{sym} {_qty(qty)}주 {'사는' if side == 'BUY' else '파는'} 주문 넣음 ({_money(price)})"


def fill_line(side, qty, sym, avg, amount, fee):
    return f"체결 완료! {sym} {_qty(qty)}주 {_money(avg)}에 {'샀음' if side == 'BUY' else '팔았음'} (수수료 {_money(fee)})"


STATUS_KO = {"running": "거래 중", "waiting": "장 기다리는 중", "closing": "정리 중", "paused": "일시정지",
             "halted": "멈춤", "done": "끝남", "stopped": "멈춤", "none": "없음"}
