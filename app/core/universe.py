"""Which stocks a plan may trade.

  large : the risk level's hand-picked large caps / ETFs (profiles.RISK)
  all   : large caps + liquid small/mid caps
  small : liquid small/mid caps only

Small/mid caps come from exchange data — no Yahoo (2026-10-18):
  US: Nasdaq's full US listing (api.nasdaq.com), market cap $300M–$10B, price ≥ $5, ≥ 1M shares/day
  KR: Naver Finance's KOSPI/KOSDAQ market-cap tables, ₩100B–₩3T, price ≥ ₩1,000, ≥ ₩2B traded/day
Penny stocks, illiquid names, ETFs/ETNs, preferred shares, units and warrants are excluded.
The list is refreshed every 6 h in a background thread; plans keep trading the previous list meanwhile.

Every small/mid cap — including today's spiking stocks — must pass a reliability check:
  • profitable (positive EPS over the last four reported quarters — Nasdaq for US, Naver for KR)
  • analysts' consensus Buy or better when analysts cover it (US: Nasdaq buy/hold/sell counts → 1–5 scale ≤ 2.5;
    KR: Naver consensus ≥ 3.5/5)
  • listed for ≥ 2 years (US IPO year; no fresh IPOs / SPACs)
  • no earnings report within the next 2 days (US; Nasdaq earnings calendar)
  • today's move < +20% (no blow-off spikes)
Lower risk levels then pick the biggest/steadiest reliable names; higher levels prefer the
reliable names that are spiking today.
"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import requests
from trader import config as C
from . import profiles
from .markets import KOSDAQ, NAMES

MODES = {"large": "Large caps", "all": "Large + small/mid caps", "small": "Small/mid caps only"}
CACHE = C.STATE_DIR / "universe.json"
TTL = 6 * 3600
PER_PLAN = 10
SOURCE = {"US": "nasdaq", "KR": "naver"}
_lock = threading.Lock()
_cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
_refreshing: set = set()
ADV: dict = {}  # symbol -> average daily traded value (for the liquidity check)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36", "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-US,en;q=0.9"}
NASDAQ = {**UA, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}

SCREENS = {
    "US": dict(cap=(3e8, 1e10), px=5, vol=1_000_000, min_value=5e6),
    "KR": dict(cap=(1e11, 3e12), px=1000, vol=100_000, min_value=2e9),
}
SORTS = (("volume", 80), ("cap", 80), ("chg", 120))   # most traded, biggest, today's movers
_SUFFIX = re.compile(r"\s*[-,]?\s*(Common (Stock|Shares?)|Capital Stock|Ordinary Shares?|Class [A-C]\b.*|(American )?Depositary Shares?.*|"
                     r"Depositary Receipts?.*|Units?\b.*|Warrants?\b.*|Preferred\b.*|Rights?\b.*)$", re.I)
_SKIP_US = re.compile(r"Depositary|Warrant|Unit|Preferred|Right|Acquisition Corp|Trust\b|Fund\b|ETF", re.I)
_PREF_KR = re.compile(r"우(선주)?[A-C]?$")


def _num(v):
    try:
        return float(re.sub(r"[,$%원배조억\s]", "", str(v)))
    except (TypeError, ValueError):
        return None


def clean_name(n: str) -> str:
    return _SUFFIX.sub("", n or "").strip()


def _nasdaq(path):
    r = requests.get("https://api.nasdaq.com/api/" + path, headers=NASDAQ, timeout=20)
    r.raise_for_status()
    return r.json().get("data") or {}


def _pick(rows):
    """Top N per sort key, deduplicated — same shape the old Yahoo screens produced."""
    key = {"volume": lambda r: -r["value"], "cap": lambda r: -r["cap"], "chg": lambda r: -r["chg"]}
    seen, uniq = set(), []
    for sort, n in SORTS:
        for r in sorted(rows, key=key[sort])[:n]:
            if r["symbol"] not in seen:
                seen.add(r["symbol"]); r["sort"] = sort; uniq.append(r)
    return uniq


def _screen_us():
    c = SCREENS["US"]
    rows = _nasdaq("screener/stocks?tableonly=true&limit=0&download=true").get("rows") or []
    if len(rows) < 1000:
        raise RuntimeError(f"Nasdaq list looks truncated ({len(rows)} rows)")
    year = datetime.now().year
    out = []
    for x in rows:
        sym, name = (x.get("symbol") or "").strip(), x.get("name") or ""
        if not sym.isalpha() or _SKIP_US.search(name):
            continue
        NAMES.setdefault(sym, clean_name(name))   # lets the news filter recognise any US name in a headline
        px, vol, cap, chg = _num(x.get("lastsale")), _num(x.get("volume")), _num(x.get("marketCap")), _num(x.get("pctchange"))
        if not cap or not (c["cap"][0] <= cap <= c["cap"][1]) or (px or 0) < c["px"] or (vol or 0) < c["vol"] \
                or px * vol < c["min_value"]:
            continue
        ipo = _num(x.get("ipoyear"))
        out.append({"symbol": sym, "name": clean_name(name), "cap": cap, "value": px * vol, "chg": chg or 0.0,
                    "kq": False, "years": (year - ipo) if ipo else None})
    picked = _pick(out)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(nasdaq_quality, picked))
    errs = sum(1 for r in picked if r.get("_err"))
    if picked and errs > len(picked) / 2:
        raise RuntimeError(f"Nasdaq fundamentals failed for {errs}/{len(picked)} stocks")
    return picked


def nasdaq_quality(row):
    """US reliability from Nasdaq: EPS summed over the last 4 reported quarters, analyst consensus from the
    buy/hold/sell counts (1 = strong buy … 5 = sell), earnings date from the earnings calendar."""
    s, now = row["symbol"], time.time()
    eps = rating = earn = None
    errs = 0
    try:
        q = [e for e in (_nasdaq(f"quote/{s}/eps").get("earningsPerShare") or [])
             if e.get("type") == "PreviousQuarter" and e.get("earnings") is not None]
        eps = round(sum(float(e["earnings"]) for e in q[-4:]), 2) if q else None
    except Exception:
        errs += 1
    try:
        co = _nasdaq(f"analyst/{s}/targetprice").get("consensusOverview") or {}
        b, h, sl = (float(co.get(k) or 0) for k in ("buy", "hold", "sell"))
        if b + h + sl:
            rating = round((1.5 * b + 3 * h + 4.5 * sl) / (b + h + sl), 2)
    except Exception:
        errs += 1
    try:
        m = re.search(r":\s*([A-Z][a-z]{2} \d{1,2}, \d{4})", _nasdaq(f"analyst/{s}/earnings-date").get("announcement") or "")
        if m:
            earn = datetime.strptime(m.group(1), "%b %d, %Y").timestamp()
    except Exception:
        errs += 1
    fails = []
    if eps is None or eps <= 0:
        fails.append("not profitable")
    if rating is not None and rating > 2.5:
        fails.append(f"analysts {rating:.1f}/5")
    if row.get("years") is not None and row["years"] < 2:
        fails.append("listed < 2 yrs")
    if earn and -86400 <= earn - now < 2 * 86400:
        fails.append("earnings in < 2 days")
    if row["chg"] >= 20:
        fails.append(f"spiked {row['chg']:+.0f}% today")
    row.update(eps=eps, rating=rating, earnings=earn, reliable=not fails, why_not=fails, _err=errs >= 2)


def _screen_kr():
    """Naver's market-cap tables, biggest first: skip above ₩3T, stop below ₩100B. Stocks only (no ETF/ETN/preferred)."""
    c = SCREENS["KR"]
    out = []
    for board, kq in (("KOSPI", False), ("KOSDAQ", True)):
        for page in range(1, 80):
            j = requests.get(f"https://m.stock.naver.com/api/stocks/marketValue/{board}?page={page}&pageSize=100",
                             headers=UA, timeout=15).json()
            stocks = j.get("stocks") or []
            if not stocks:
                break
            stop = False
            for x in stocks:
                cap = (_num(x.get("marketValue")) or 0) * 1e6          # 백만원
                if cap < c["cap"][0]:
                    stop = True; break
                code, name = x.get("itemCode") or "", x.get("stockName") or ""
                if cap > c["cap"][1] or x.get("stockEndType") != "stock" or not (code.isdigit() and len(code) == 6) \
                        or _PREF_KR.search(name) or (x.get("tradeStopType") or {}).get("code") not in (None, "1"):
                    continue
                px, vol = _num(x.get("closePrice")) or 0, _num(x.get("accumulatedTradingVolume")) or 0
                val = (_num(x.get("accumulatedTradingValue")) or 0) * 1e6
                if px < c["px"] or vol < c["vol"] or val < c["min_value"]:
                    continue
                out.append({"symbol": code, "name": name, "cap": cap, "value": val,
                            "chg": _num(x.get("fluctuationsRatio")) or 0.0, "kq": kq})
            if stop:
                break
    if len(out) < 20:
        raise RuntimeError(f"Naver list too short ({len(out)} rows)")
    picked = _pick(out)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(naver_quality, picked))
    errs = sum(1 for r in picked if r.get("_err"))
    if errs > len(picked) / 2:
        raise RuntimeError(f"Naver fundamentals failed for {errs}/{len(picked)} stocks")
    return picked


def naver_quality(row):
    """KR reliability: positive EPS (Naver), analyst consensus ≥ 3.5/5 when covered (Naver: 5 = strong buy), no +20% spike."""
    try:
        j = requests.get(f"https://m.stock.naver.com/api/stock/{row['symbol']}/integration",
                         headers=UA, timeout=8).json()
    except Exception:
        row.update(reliable=False, why_not=["no fundamentals data"], _err=True); return
    ti = {x["code"]: x["value"] for x in j.get("totalInfos", [])}
    eps = _num(ti.get("eps"))
    rec = _num((j.get("consensusInfo") or {}).get("recommMean"))
    fails = []
    if eps is None or eps <= 0:
        fails.append("not profitable")
    if rec is not None and rec < 3.5:
        fails.append(f"analysts {rec:.1f}/5")
    if row.get("chg", 0) >= 20:
        fails.append(f"spiked {row['chg']:+.0f}% today")
    if j.get("stockName"):
        row["name"] = j["stockName"]
    row.update(eps=eps, rating=rec, reliable=not fails, why_not=fails, _err=False)


def _screen(market):
    return _screen_us() if market == "US" else _screen_kr()


def _refresh(market):
    try:
        rows = _screen(market)
        with _lock:
            _cache[market] = {"t": time.time(), "rows": rows, "src": SOURCE[market]}
            CACHE.write_text(json.dumps(_cache, ensure_ascii=False))
        print(f"universe {market}: {len(rows)} small/mid caps from {SOURCE[market]}, "
              f"{sum(1 for r in rows if r.get('reliable'))} reliable", flush=True)
    except Exception as e:
        print(f"screen failed {market}: {e}", flush=True)
        with _lock:   # keep the old list, retry in 30 min
            hit = _cache.setdefault(market, {"rows": [], "src": None})
            hit["t"] = time.time() - TTL + 1800
    finally:
        with _lock:
            _refreshing.discard(market)


def small_caps(market, risk) -> list[str]:
    with _lock:
        hit = _cache.get(market)
        stale = not hit or time.time() - hit["t"] > TTL or hit.get("src") != SOURCE[market]
        if stale and market not in _refreshing:
            _refreshing.add(market)
            threading.Thread(target=_refresh, args=(market,), daemon=True, name=f"screen-{market}").start()
        rows = (hit or {}).get("rows", [])
    for r in rows:  # make KOSDAQ codes resolvable + give news search a name
        if r["kq"]:
            KOSDAQ.add(r["symbol"])
        NAMES.setdefault(r["symbol"], r["name"])
        ADV[r["symbol"]] = r["value"]
    ok = [r for r in rows if r.get("reliable")]
    key = {1: lambda r: -r["cap"],                               # biggest, steadiest
           2: lambda r: -r["cap"],
           3: lambda r: -r["value"],                             # most actively traded
           4: lambda r: -(r["value"] * (1 + max(r["chg"], 0) / 10)),  # active + spiking
           5: lambda r: -r["chg"]}[risk]                         # today's reliable spikers first
    return [r["symbol"] for r in sorted(ok, key=key)[:PER_PLAN]]


def info(sym) -> dict | None:
    for m in _cache.values():
        for r in m.get("rows", []):
            if r["symbol"] == sym:
                return r
    return None


def symbols(market, risk, mode="large") -> list[str]:
    large = profiles.RISK[risk][market]
    if mode == "large":
        return list(large)
    small = small_caps(market, risk)
    if mode == "small":
        return small or list(large)  # screen unavailable → fall back rather than trade nothing
    return list(dict.fromkeys(large + small))


def is_small(sym) -> bool:
    return sym in ADV


# ── Swing picks (2026-10-19, Blaze) ────────────────────────────────────────────────────────────────────────────────
# A swing trader needs stocks that MOVE every day but are not being pumped. Candidates are only respected names:
# the risk 3–4 large caps (no leveraged ETFs) plus every small/mid cap that passed the reliability check above
# (profitable, analyst Buy or better, listed ≥ 2 yrs, no earnings within 2 days, price ≥ $5 / ₩1,000). Cheap is fine.
# From 3 months of daily bars each gets:
#   swing room = average daily high-low range over 20 days (how much it moves intraday)
# and is dropped as pump-and-dump-like if: up > 20% in 5 days, up > 40% in 20 days, today ≥ +12%,
# average range > 10% (wild), < 2.5% (too calm to swing), or less than $20M / ₩10B traded a day.
# Ranking: swing room counted up to 7%/day (more is not better), boosted by how heavily it trades — so liquid,
# well-known movers beat jumpy thin ones. The top SWING_N are traded. Recomputed hourly in the background.
SWING_N = 12
SWING_RULES = dict(run5=0.20, run20=0.40, today=12.0, wild=0.10, calm=0.025, sweet=0.07,
                   min_value={"US": 2e7, "KR": 1e10})
_swing: dict = {}          # market -> {"t": epoch, "syms": [...], "table": [...]}
_swing_busy: set = set()


def _swing_compute(market):
    from . import data
    try:
        large = list(dict.fromkeys(profiles.RISK[3][market] + profiles.RISK[4][market]))
        rows = {r["symbol"]: r for r in (_cache.get(market) or {}).get("rows", []) if r.get("reliable")}
        cands = list(dict.fromkeys(large + list(rows)))
        bars = data.bars(cands, "1d", ttl=3600)
        R, table = SWING_RULES, []
        for s in cands:
            df = bars.get(s)
            if df is None or len(df) < 25:
                continue
            c, h, lo, v = df["Close"], df["High"], df["Low"], df["Volume"]
            rng = float(((h - lo) / c).iloc[-21:-1].mean())
            run5, run20 = float(c.iloc[-1] / c.iloc[-6] - 1), float(c.iloc[-1] / c.iloc[-21] - 1)
            value = float((c * v).iloc[-21:-1].mean())
            today = (rows.get(s) or {}).get("chg") or float((c.iloc[-1] / c.iloc[-2] - 1) * 100)
            why = ("up %.0f%% in 5 days" % (run5 * 100) if run5 > R["run5"] else
                   "up %.0f%% in 20 days" % (run20 * 100) if run20 > R["run20"] else
                   "up %.0f%% today" % today if today >= R["today"] else
                   "too wild (%.0f%%/day)" % (rng * 100) if rng > R["wild"] else
                   "too calm to swing (%.1f%%/day)" % (rng * 100) if rng < R["calm"] else
                   "too thin to trade quickly" if value < R["min_value"][market] else None)
            table.append({"symbol": s, "range": round(rng, 4), "run5": round(run5, 4), "run20": round(run20, 4),
                          "value": round(value), "large": s in large, "skip": why})
        import math
        mv = R["min_value"][market]
        for t in table:
            t["score"] = round(min(t["range"], R["sweet"]) * (1 + 0.25 * math.log10(max(t["value"], mv) / mv)), 5)
        ok = sorted((t for t in table if not t["skip"]), key=lambda t: -t["score"])
        if not table:   # no price data at all: keep the last good list and look again in 10 minutes
            hit = _swing.setdefault(market, {"syms": [], "table": []})
            hit["t"] = time.time() - 3000
            print(f"swing {market}: no price data, keeping {len(hit['syms'])} stocks from the last good list", flush=True)
            return
        _swing[market] = {"t": time.time(), "syms": [t["symbol"] for t in ok[:SWING_N]], "table": table}
        print(f"swing {market}: {len(ok)} of {len(table)} respected movers ok → "
              + ", ".join(f"{t['symbol']} {t['range']:.1%}" for t in ok[:SWING_N]), flush=True)
    except Exception as e:
        print(f"swing {market} failed: {e}", flush=True)
    finally:
        _swing_busy.discard(market)


def swing_symbols(market) -> list[str]:
    """Respected stocks that move the most each day (see above). Until the first computation finishes, the
    risk-4 large caps are used."""
    small_caps(market, 4)   # keeps the reliability screen fresh
    hit = _swing.get(market)
    if (not hit or time.time() - hit["t"] > 3600) and market not in _swing_busy:
        _swing_busy.add(market)
        threading.Thread(target=_swing_compute, args=(market,), daemon=True, name=f"swing-{market}").start()
    return (hit or {}).get("syms") or list(profiles.RISK[4][market])
