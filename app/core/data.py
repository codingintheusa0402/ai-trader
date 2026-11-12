"""Market data. Toss first, Yahoo only as the fallback (2026-10-17).
  · Live quotes: Toss real-time → yfinance only for symbols Toss didn't return.
  · Bars: the history comes from yfinance, but the NEWEST bar is corrected with the live Toss price, so every signal
    (trend, momentum, breakout, today's move) is computed on Toss's real-time price, not Yahoo's delayed one
    (KR on Yahoo runs ~15–20 min late). Full Toss candles are used instead of Yahoo when TOSS_BARS=on in .env and a
    probe has confirmed the response format (see probe_toss_candles) — every Toss candle set is validated, and
    anything that doesn't check out falls back to Yahoo for that symbol."""
import logging
import threading
import time
from datetime import datetime, timezone
import pandas as pd
import trader  # noqa: F401  (sets CA bundle before yfinance)
import yfinance as yf
from .markets import yf_symbol
MARKET_TZ_NAME = {"KR": "Asia/Seoul", "US": "America/New_York"}

FEED = {"yf_ok": 0.0, "reset": 0.0, "last_error": ""}   # price feed health, shown in alerts


class _YfLog(logging.Handler):
    """yfinance reports failures only through its logger: keep the latest for alerts instead of printing the spam."""
    def emit(self, record):
        FEED["last_error"] = record.getMessage()[:120]


logging.getLogger("yfinance").setLevel(logging.ERROR)
logging.getLogger("yfinance").propagate = False
logging.getLogger("yfinance").addHandler(_YfLog())
_lock = threading.Lock()
_cache: dict = {}
_toss = None
_toss_retry = 0.0


def toss():
    """The broker client (BROKER in .env: toss | kis), or None when no keys are set (dry-run on Yahoo prices).
    If creating it failed, try again every 60 s instead of giving up forever. Named toss() for history: it returns
    whichever broker adapter is configured."""
    global _toss, _toss_retry
    if _toss is None or (_toss is False and time.time() > _toss_retry):
        try:
            from trader.brokers import make_client
            _toss = make_client()
        except Exception:
            _toss, _toss_retry = False, time.time() + 60
    return _toss or None


broker = toss


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    if len(_cache) > 80:   # drop the oldest entries so memory can't grow forever
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:20]:
            _cache.pop(k, None)
    return val


def quotes(symbols) -> dict:
    """{sym: {"price": float, "age": seconds since exchange timestamp}}"""
    symbols = sorted(set(symbols))
    if not symbols:
        return {}

    def fetch():
        out = {}
        t = toss()
        if t:
            for i in range(0, len(symbols), 20):          # one bad batch / quote no longer drops every other symbol
                try:
                    batch = t.prices(symbols[i:i + 20])["result"]
                except Exception as e:
                    print("toss quotes failed:", e)
                    continue
                for q in batch:
                    try:
                        price = float(q["lastPrice"])
                        # Toss sends timestamp=None for a stock with no trade yet in this session (small caps in
                        # pre/after-hours): keep its last price but never treat it as live
                        ts = q.get("timestamp")
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() if ts else 9999.0
                        out[q["symbol"]] = {"price": price, "age": age} if ts else {"price": price, "age": age, "src": "toss-no-trade"}
                    except Exception as e:
                        print(f"toss quote skipped {q.get('symbol')}: {e}")
        # Toss refuses this machine's login (IP allow list: e.g. running from a new network): Yahoo's
        # last price is the best there is, so treat it as live and keep the mock/dry-run plans trading. Real plans
        # can't place orders without a Toss login anyway, so this never puts real money on a Yahoo price.
        # No broker configured at all (dry-run only, no keys): Yahoo is the only price there is, so it counts as live
        # for dry-run too. The risk gate still refuses any "yf…" price for a real-money order.
        try:
            from trader.toss import auth_block
            yf_live = bool(auth_block()) or t is None
        except Exception:
            yf_live = t is None
        for s in symbols:
            if s not in out:
                try:
                    with _lock:
                        p = float(yf.Ticker(yf_symbol(s)).fast_info["last_price"])
                    out[s] = {"price": p, "age": 30.0 if yf_live else 9999.0, "src": "yf-live" if yf_live else "yf"}
                except Exception:
                    pass
        return out
    return _cached(("q", tuple(symbols)), 4, fetch)


PERIOD = {"1m": "5d", "5m": "1mo", "15m": "1mo", "60m": "6mo", "1d": "2y"}
SECS = {"1m": 60, "5m": 300, "15m": 900, "60m": 3600, "1d": 86400}
_PROBE = None   # result of probe_toss_candles(): {"ok": bool, "params": {...}, "keys": [...]} — kept in state/


def _yf_bars(symbols, interval):
    tick = [yf_symbol(s) for s in symbols]
    with _lock:
        raw = yf.download(tick, period=PERIOD[interval], interval=interval, auto_adjust=True,
                          progress=False, group_by="ticker", threads=False)
    out = {}
    for s, t in zip(symbols, tick):
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df.dropna(subset=["Close"])
            if len(df):
                out[s] = df
        except KeyError:
            pass
    return out


def _yf_safe(symbols, interval):
    try:
        out = _yf_bars(symbols, interval)
    except Exception as e:
        FEED["last_error"] = f"{e.__class__.__name__}: {e}"[:120]
        return {}
    if out:
        FEED["yf_ok"], FEED["last_error"] = time.time(), ""
    return out


def _yf_reset():
    """Fresh Yahoo session, at most every 5 min. On 11-02 and 11-06 Yahoo went silent inside the running server for good
    while a new process worked at once, so the session itself is thrown away."""
    if time.time() - FEED["reset"] < 300:
        return False
    FEED["reset"] = time.time()
    try:
        from yfinance.data import YfData
        with _lock:
            type(YfData)._instances.pop(YfData, None)
        print("price feed: Yahoo session reset", flush=True)
        return True
    except Exception as e:
        print(f"price feed: Yahoo reset failed: {e}", flush=True)
        return False


# ── price history on disk (v1.11): survives restarts and feed outages ───────────────────────────────────────────
KEEP_BARS = 600
_saved: dict = {}
_no_toss: dict = {}


def _bar_file(sym, interval):
    from trader import config as C
    return C.STATE_DIR / "bars" / interval / f"{sym}.pkl"


def _disk_save(sym, interval, df):
    if time.time() - _saved.get((sym, interval), 0) < 900:
        return
    try:
        f = _bar_file(sym, interval)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        df.tail(KEEP_BARS).to_pickle(tmp)
        tmp.replace(f)
        _saved[(sym, interval)] = time.time()
    except Exception as e:
        print(f"bar save failed {sym} {interval}: {e}", flush=True)


def _disk_load(sym, interval, max_days=7):
    """Saved bars, unless they are too old to mean anything."""
    try:
        f = _bar_file(sym, interval)
        if not f.exists() or time.time() - f.stat().st_mtime > max_days * 86400:
            return None
        df = pd.read_pickle(f)
        return df if len(df) else None
    except Exception:
        return None


REGULAR = {"US": ("09:30", "15:59"), "KR": ("09:00", "15:29")}


def _toss_frame(sym, interval, disk):
    """Toss only serves 1m and 1d candles. 5m/15m/60m are built from 1m, regular hours only like Yahoo's. With saved
    history one page of 200 minutes is enough; without it, twelve pages cover two trading days (scoring needs 28 15m bars)."""
    t = toss()
    if not t:
        return None
    mkt = "KR" if sym.isdigit() else "US"
    if interval == "1d":
        df = parse_toss_candles(t.candles(sym, interval="1d", count=200))
        if df is None:
            return None
        df.index = df.index.tz_convert(MARKET_TZ_NAME[mkt]).tz_localize(None).normalize()
    else:
        rows, before = [], None
        for _ in range(1 if disk is not None else 12):
            r = t.candles(sym, interval="1m", count=200, **({"before": before} if before else {}))["result"]
            rows += r.get("candles") or []
            before = r.get("nextBefore")
            if not before:
                break
        df = parse_toss_candles({"result": rows})
        if df is None:
            return None
        df.index = df.index.tz_convert(MARKET_TZ_NAME[mkt])
        df = df.between_time(*REGULAR[mkt])
        if not len(df):
            return None
        if interval != "1m":
            df = df.resample(f"{SECS[interval] // 60}min").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    if disk is not None:
        df = pd.concat([disk[disk.index < df.index[0]][["Open", "High", "Low", "Close", "Volume"]], df])
    return df


def _backup(symbols, interval):
    """Yahoo gave nothing for these: Toss candles (first choice, up to 30 s of them), else the saved bars."""
    out = {}
    stop = time.time() + 30
    for s in symbols:
        disk = _disk_load(s, interval)
        df = None
        if time.time() < stop and time.time() - _no_toss.get((s, interval), 0) > 3600:
            try:
                df = _toss_frame(s, interval, disk)
            except Exception as e:
                FEED["last_error"] = FEED["last_error"] or f"Toss candles: {e}"[:120]
            if df is None:
                _no_toss[(s, interval)] = time.time()
        if df is not None:
            _disk_save(s, interval, df)
            df.attrs["src"] = "toss"
            out[s] = df
        elif disk is not None:
            disk.attrs["src"] = "disk"
            out[s] = disk
    return out


def _pick(d, *names):
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n in low and low[n] is not None:
            return low[n]
    return None


def parse_toss_candles(resp) -> pd.DataFrame | None:
    """Toss /api/v1/candles → OHLCV frame like yfinance's, or None if the response doesn't look right.
    Tolerant of field naming (open/openPrice, time/timestamp/dateTime…), strict about the data itself."""
    rows = resp.get("result") if isinstance(resp, dict) else resp
    if isinstance(rows, dict):
        rows = _pick(rows, "candles", "items", "list", "data")
    if not isinstance(rows, list) or len(rows) < 30:
        return None
    rec = []
    for r in rows:
        if not isinstance(r, dict):
            return None
        o, h, l, c = (_pick(r, f, f + "price") for f in ("open", "high", "low", "close"))
        t = _pick(r, "timestamp", "time", "datetime", "date", "dt", "t", "basedatetime")
        v = _pick(r, "volume", "tradevolume", "accumulatedvolume") or 0
        if None in (o, h, l, c, t):
            return None
        rec.append((pd.Timestamp(t), float(o), float(h), float(l), float(c), float(v)))
    df = pd.DataFrame(rec, columns=["t", "Open", "High", "Low", "Close", "Volume"]).set_index("t").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    ok = ((df["High"] >= df[["Open", "Close"]].max(axis=1) - 1e-9) & (df["Low"] <= df[["Open", "Close"]].min(axis=1) + 1e-9)
          & (df["Close"] > 0)).mean()
    return df if ok > 0.98 and len(df) >= 30 else None


def _toss_bars(symbols, interval, yf_out):
    """Replace Yahoo bars with Toss candles where Toss's validate AND agree with Yahoo's recent closes (≤1.5%)."""
    t = toss()
    if not (t and _PROBE and _PROBE.get("ok")):
        return yf_out
    out = dict(yf_out)
    for s in symbols:
        try:
            df = parse_toss_candles(t.candles(s, **{**_PROBE["params"], "interval": interval}))
            ref = yf_out.get(s)
            if df is None:
                continue
            if ref is not None and len(ref) >= 5:
                common = df["Close"].reindex(ref.index[-20:], method="nearest", tolerance=pd.Timedelta(seconds=SECS[interval]))
                diff = (common / ref["Close"].iloc[-20:] - 1).abs().dropna()
                if len(diff) and diff.median() > 0.015:
                    continue   # Toss and Yahoo disagree: don't trust either blindly, keep Yahoo for this one
            out[s] = df
        except Exception:
            continue
    return out


def _overlay_live(out, interval):
    """Correct the newest bar with Toss's live price (no extra API calls: quotes are cached and already fetched)."""
    syms = [s for s, df in out.items() if len(df)]
    if not syms:
        return out
    try:
        q = quotes(syms)
    except Exception:
        return out
    now = pd.Timestamp.now(tz="UTC")
    res = {}
    for s, df in out.items():
        v = q.get(s)
        if not v or v.get("src") or v.get("age", 9999) > 120 or not len(df):
            res[s] = df; continue    # only a live Toss price corrects a bar
        last = df.index[-1]
        lt = last.tz_convert("UTC") if last.tzinfo else last.tz_localize(MARKET_TZ_NAME["KR" if s.isdigit() else "US"]).tz_convert("UTC")
        fresh = (now - lt).total_seconds() < (SECS[interval] * 3 if interval != "1d" else 86400)
        if not fresh and df.attrs.get("src") != "disk":
            res[s] = df; continue    # last bar is from a closed session: leave history alone
        px = float(v["price"])
        if abs(px / float(df["Close"].iloc[-1]) - 1) > 0.15:
            res[s] = df; continue    # wildly different (split, bad tick): don't touch
        df = df.copy()
        if not fresh:   # saved bars only: the live price opens a new bar, so today's scoring starts from the real price
            at = now.floor(f"{SECS[interval] // 60}min").tz_convert(last.tzinfo) if last.tzinfo else now.tz_convert(MARKET_TZ_NAME["KR" if s.isdigit() else "US"]).tz_localize(None).normalize()
            df.loc[at, ["Open", "High", "Low", "Close", "Volume"]] = [px, px, px, px, 0.0]
        i = df.index[-1]
        df.loc[i, "Close"] = px
        df.loc[i, "High"] = max(float(df.loc[i, "High"]), px)
        df.loc[i, "Low"] = min(float(df.loc[i, "Low"]), px)
        res[s] = df
    return res


def bars(symbols, interval: str, ttl: int = 60) -> dict[str, pd.DataFrame]:
    symbols = sorted(set(symbols))

    def fetch():
        out = _yf_safe(symbols, interval)
        if not out and _yf_reset():
            out = _yf_safe(symbols, interval)
        if _toss_bars_on():
            out = _toss_bars(symbols, interval, out)
        for s, df in out.items():
            _disk_save(s, interval, df)
        miss = [s for s in symbols if s not in out]
        if miss:
            out.update(_backup(miss, interval))
        return out
    key = ("b", interval, tuple(symbols))
    base = _cached(key, ttl, fetch)
    if not base:   # an empty answer must not stick for the whole cache time
        _cache.pop(key, None)
    return _overlay_live(base, interval)


def _toss_bars_on():
    try:
        from dotenv import dotenv_values
        from trader import config as C
        return (dotenv_values(C.ROOT / ".env").get("TOSS_BARS") or "").strip() == "on"
    except Exception:
        return False


def probe_toss_candles(symbol="AAPL"):
    """Once at start-up: ask Toss for candles and record what came back (state/toss_candles_probe.json), so the
    format is known and Toss bars can be switched on with TOSS_BARS=on. Read-only; never places anything."""
    global _PROBE
    import json
    from trader import config as C
    path = C.STATE_DIR / "toss_candles_probe.json"
    t = toss()
    if not t:
        return None
    res = {"t": time.time(), "ok": False}
    for params in ({"interval": "1d", "count": 200}, {"interval": "1d", "limit": 200}, {"period": "1d", "count": 200}):
        try:
            r = t.candles(symbol, **params)
            rows = r.get("result") if isinstance(r, dict) else r
            first = rows[0] if isinstance(rows, list) and rows else rows
            res.update(params=params, keys=sorted(first.keys())[:20] if isinstance(first, dict) else str(type(first)),
                       n=len(rows) if isinstance(rows, list) else None)
            if parse_toss_candles(r) is not None:
                res["ok"] = True
                break
        except Exception as e:
            res.setdefault("errors", []).append(f"{params}: {e.__class__.__name__}: {str(e)[:160]}")
    try:
        path.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    except Exception:
        pass
    _PROBE = res
    return res


MONITOR = [
    ("NASDAQ", "^IXIC"), ("S&P 500", "^GSPC"), ("Dow", "^DJI"), ("KOSPI", "^KS11"),
    ("KOSDAQ", "^KQ11"), ("Nikkei 225", "^N225"), ("Shanghai", "000001.SS"),
    ("USD/KRW", "KRW=X"), ("JPY/KRW", "JPYKRW=X"), ("VIX", "^VIX"),
    ("US 10Y", "^TNX"), ("Gold", "GC=F"), ("Silver", "SI=F"), ("WTI", "CL=F"), ("Bitcoin", "BTC-USD"),
]


def monitor():
    def fetch():
        with _lock:
            raw = yf.download([t for _, t in MONITOR], period="1mo", interval="1d",
                              progress=False, group_by="ticker", auto_adjust=False, threads=False)
        out = []
        for name, t in MONITOR:
            try:
                c = raw[t]["Close"].dropna()
                out.append({"name": name, "symbol": t, "price": float(c.iloc[-1]),
                            "change": float(c.iloc[-1] / c.iloc[-2] - 1),
                            "spark": [round(float(x), 4) for x in c.tail(22)]})
            except Exception:
                pass
        tc = toss()
        if tc:  # live FX from Toss overrides yfinance's
            try:
                fx = tc.usd_krw()
                for row in out:
                    if row["name"] == "USD/KRW":
                        row["price"] = fx
            except Exception:
                pass
        return out
    return _cached("monitor", 60, fetch)


def usdkrw() -> float:
    for row in monitor():
        if row["name"] == "USD/KRW":
            return row["price"]
    return 1350.0


def quotes_prices(symbols) -> dict:
    return {s: v["price"] for s, v in quotes(symbols).items()}
