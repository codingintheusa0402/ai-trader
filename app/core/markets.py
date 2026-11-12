"""Markets: symbols, fees (Toss actual), trading hours, tick sizes."""
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

CCY = {"US": "USD", "KR": "KRW"}

# Toss actual (verified from account cost fields 2026-10-09)
FEES = {
    "US": {"buy": 0.001, "sell": 0.001, "sell_tax": 0.0000278},  # 0.1% + SEC fee
    "KR": {"buy": 0.00015, "sell": 0.00015, "sell_tax": 0.0020},  # 0.015% + 거래세/농특세
}
SLIPPAGE = 0.0005

# KOSDAQ listings need ".KQ" on yfinance; everything else KR is ".KS"
KOSDAQ = {"086520", "247540", "196170", "028300", "091990", "263750", "293490"}

NAMES = {  # for news search + UI
    "069500": "KODEX 200", "360750": "TIGER 미국S&P500", "148070": "KOSEF 국고채10년",
    "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER", "005380": "현대차",
    "373220": "LG에너지솔루션", "207940": "삼성바이오로직스", "105560": "KB금융",
    "055550": "신한지주", "012450": "한화에어로스페이스", "042700": "한미반도체",
    "086520": "에코프로", "247540": "에코프로비엠", "196170": "알테오젠",
    "122630": "KODEX 레버리지", "233740": "KODEX 코스닥150레버리지", "035720": "카카오",
    "114800": "KODEX 인버스", "252670": "KODEX 200선물인버스2X", "251340": "KODEX 코스닥150선물인버스",
    # US large caps (profiles.RISK) — so a CNBC/WSJ headline that says "Nvidia" counts for NVDA
    "AAPL": "Apple", "MSFT": "Microsoft", "COST": "Costco", "JNJ": "Johnson & Johnson", "NVDA": "Nvidia",
    "GOOGL": "Alphabet", "AMZN": "Amazon", "META": "Meta Platforms", "AVGO": "Broadcom", "JPM": "JPMorgan",
    "TSLA": "Tesla", "AMD": "Advanced Micro Devices", "PLTR": "Palantir", "COIN": "Coinbase", "MU": "Micron",
    "NFLX": "Netflix", "MSTR": "MicroStrategy", "SMCI": "Super Micro Computer", "HOOD": "Robinhood",
}


def yf_symbol(sym: str) -> str:
    if sym.isdigit():
        return f"{sym}.KQ" if sym in KOSDAQ else f"{sym}.KS"
    return sym


def market_of(sym: str) -> str:
    return "KR" if sym.isdigit() else "US"


# Trading windows — every session Toss accepts orders in (KST, from Toss's market calendar, which also knows
# holidays and US daylight saving):
#   US  day market 09:00–17:00 · pre-market 17:00–22:30 · regular 22:30–05:00 · after-market 05:00–08:50
#   KR  pre-market 08:00–08:50 (NXT) · regular 09:00–15:20 · after-market 15:40–20:00
#   The KR single-price auctions (08:50–09:00, 15:20–15:40) are skipped.
# Fallback when the calendar can't be read: the same windows from fixed local times.
_cal = {}


def _calendar(market):
    hit = _cal.get(market)
    if hit and time.time() - hit[0] < (3600 if hit[1] else 300):
        return hit[1]
    days = None
    try:
        from .data import toss
        t = toss()
        if t:
            r = t.market_calendar(market)
            days = [r.get(k) for k in ("previousBusinessDay", "today", "nextBusinessDay") if r.get(k)]
    except Exception:
        days = None
    _cal[market] = (time.time(), days)
    return days


def _windows(market):
    """[(start, end, session, trading-day)] epoch windows, from Toss's calendar (None if unavailable)."""
    days = _calendar(market)
    if days is None:
        return None
    ts = lambda x: datetime.fromisoformat(x).timestamp()
    out = []
    for d in days:
        if market == "US":
            for key, name in (("dayMarket", "day"), ("preMarket", "pre"), ("regularMarket", "regular"), ("afterMarket", "after")):
                w = d.get(key)
                if w:
                    out.append((ts(w["startTime"]), ts(w["endTime"]), name, d["date"]))
        else:
            g = d.get("integrated") or {}
            pre, reg, aft = g.get("preMarket"), g.get("regularMarket"), g.get("afterMarket")
            if pre:
                out.append((ts(pre["startTime"]), ts(pre.get("singlePriceAuctionStartTime") or pre["endTime"]), "pre", d["date"]))
            if reg:
                out.append((ts(reg["startTime"]), ts(reg.get("singlePriceAuctionStartTime") or reg["endTime"]), "regular", d["date"]))
            if aft:
                out.append((ts(aft.get("singlePriceAuctionEndTime") or aft["startTime"]), ts(aft["endTime"]), "after", d["date"]))
    return out


def _fixed(market, now):
    """(session, end-of-trading-day epoch) from fixed times — used only when the calendar is unavailable."""
    k = now.astimezone(ZoneInfo("Asia/Seoul"))
    if market == "US":
        et = now.astimezone(ZoneInfo("America/New_York"))
        t = et.time()
        if et.weekday() >= 5:   # (Toss's KST day session is only known from its calendar, not assumed here)
            sess = None
        else:
            sess = ("pre" if dtime(4, 0) <= t < dtime(9, 30) else "regular" if dtime(9, 30) <= t < dtime(16, 0)
                    else "after" if dtime(16, 0) <= t < dtime(19, 50) else None)
        end = k.replace(hour=8, minute=50, second=0, microsecond=0)
        if end <= k:
            end += timedelta(days=1)
        return sess, (end.timestamp() if sess else None)
    if k.weekday() >= 5:
        return None, None
    t = k.time()
    sess = ("pre" if dtime(8, 0) <= t < dtime(8, 50) else "regular" if dtime(9, 0) <= t < dtime(15, 20)
            else "after" if dtime(15, 40) <= t < dtime(20, 0) else None)
    return sess, (k.replace(hour=20, minute=0, second=0, microsecond=0).timestamp() if sess else None)


def _now_info(market, now=None):
    """(session or None, end of the current trading day's last session as epoch or None)."""
    if now is None:
        w = _windows(market)
        if w is not None:
            t = time.time()
            cur = next(((name, day) for a, b, name, day in w if a <= t < b), None)
            if not cur:
                return None, None
            return cur[0], max(b for a, b, name, day in w if day == cur[1])
    return _fixed(market, now or datetime.now(ZoneInfo("Asia/Seoul")))


def session(market: str, now: datetime | None = None) -> str | None:
    """"day", "pre", "regular", "after", or None (closed)."""
    return _now_info(market, now)[0]


def is_open(market: str, now: datetime | None = None, regular: bool = False) -> bool:
    """Trading allowed now (all sessions). regular=True: regular session only (bars, statistics)."""
    s = session(market, now)
    return s == "regular" if regular else s is not None


def session_close(market: str, now: datetime | None = None) -> float | None:
    """Epoch seconds when today's trading ends (end of the last session of the trading day; None if closed now)."""
    s, end = _now_info(market, now)
    return end if s else None


_TZ = {"US": "America/New_York", "KR": "Asia/Seoul"}


def month_end(market: str, t: float | None = None) -> float:
    """Epoch of the first moment of the next calendar month in the market's own time zone — the hard end of a
    monthly-cycle plan (it normally sells out earlier, before the month's last regular close)."""
    d = datetime.fromtimestamp(t or time.time(), ZoneInfo(_TZ[market]))
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime(y, m, 1, tzinfo=ZoneInfo(_TZ[market])).timestamp()


def last_trading_day_of_month(market: str) -> bool:
    """True while the current trading day is the month's last one (next business day falls in another month).
    Toss's market calendar knows holidays; without it, the next weekday in the market's time zone is used."""
    w = _windows(market)
    t = time.time()
    if w is not None:
        cur = next((day for a, b, name, day in w if a <= t < b), None)
        nxt = sorted({day for a, b, name, day in w if cur and day > cur})
        if cur and nxt:
            return nxt[0][:7] != cur[:7]
    d = datetime.fromtimestamp(t, ZoneInfo(_TZ[market])).date()
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n.month != d.month


def tick_round(price: float, market: str, up: bool, sym: str = "") -> float:
    """Round a limit price to a valid tick, toward `up` (buy) or down (sell)."""
    import math
    if market == "US":
        step = 0.01 if price >= 1 else 0.0001
    elif sym in KR_ETF:
        step = 5
    else:
        step = next(s for lim, s in [(2000, 1), (5000, 5), (20000, 10), (50000, 50),
                                     (200000, 100), (500000, 500), (float("inf"), 1000)] if price < lim)
    n = price / step
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * step, 4)


# Korean ETFs: no 증권거래세 on sale, and a 5-won price tick
KR_ETF = {"069500", "360750", "148070", "122630", "233740", "229200", "114800", "252670", "251340"}

# Inverse and leveraged (2×/3×) ETFs — plans with the "inverse & leveraged" switch on may trade all of them.
# Inverse ones are bought only while the market reads bear; leveraged inverse ones are never held past a day
# (daily-reset decay). Leveraged long ones trade under the normal rules, like any stock.
INVERSE = {"US": ["SH", "PSQ", "SDS", "QID", "SQQQ", "SPXU", "SOXS"],
           "KR": ["114800", "251340", "252670"]}
LEVERAGED_LONG = {"US": ["SSO", "QLD", "UPRO", "TQQQ", "SOXL"], "KR": ["122630", "233740"]}
LEVERAGE = {"SDS": 2, "QID": 2, "SQQQ": 3, "SPXU": 3, "SOXS": 3, "252670": 2,
            "SSO": 2, "QLD": 2, "UPRO": 3, "TQQQ": 3, "SOXL": 3, "122630": 2, "233740": 2}
INVERSE_ALL = {s for v in INVERSE.values() for s in v}


def inverse_for(market, risk=None):
    """Extra symbols a plan with the switch on may trade (risk no longer limits them)."""
    return INVERSE[market] + LEVERAGED_LONG[market]


def fee(market: str, side: str, notional: float, sym: str = "") -> float:
    f = FEES[market]
    tax = 0.0 if sym in KR_ETF else f["sell_tax"]
    rate = f["buy"] if side == "BUY" else f["sell"] + tax
    return notional * rate


def regular_open_mins(market: str) -> float | None:
    """Minutes since today's REGULAR session opened (None outside it). Feeds the observe-only 'first 15 min' rule."""
    w = _windows(market)
    t = time.time()
    if w is not None:
        cur = next((a for a, b, name, day in w if a <= t < b and name == "regular"), None)
        return None if cur is None else (t - cur) / 60
    loc = datetime.fromtimestamp(t, ZoneInfo(_TZ[market]))
    if session(market, loc) != "regular":
        return None
    o = loc.replace(hour=9, minute=30 if market == "US" else 0, second=0, microsecond=0)
    return max(0.0, (loc - o).total_seconds() / 60)


def regular_close(market: str) -> float | None:
    """Epoch seconds when today's REGULAR session ends (None if trading is closed now or that time has passed).
    Swing plans go flat here: what's left after it is thin after-hours trading and the overnight gap."""
    w = _windows(market)
    if w is None:
        return session_close(market)
    t = time.time()
    cur = next((day for a, b, name, day in w if a <= t < b), None)
    if cur is None:
        return None
    ends = [b for a, b, name, day in w if day == cur and name == "regular" and b > t]
    return min(ends) if ends else None
