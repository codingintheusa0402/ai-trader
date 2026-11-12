"""Real news → per-symbol sentiment in [-1, 1].

Headlines, last ~2 days, from two places:
  · the trusted outlets' OWN feeds (FEEDS below: CNBC, MarketWatch, WSJ; 연합뉴스, 한국경제, 머니투데이, 이데일리,
    조선비즈) — fetched once per refresh and matched to every stock they name (2026-10-18), and
  · Google News RSS per stock (KR in Korean, US in English) — kept ONLY when (2026-10-17)
    the outlet is a trusted financial/wire source (TRUSTED below; spam and blog sites are dropped),
  · the headline names the company or its ticker, and
  · it isn't a near-duplicate of a headline already kept (syndicated copies count once).
Scoring: one batched `claude -p --model haiku` call per refresh, and only for stocks whose kept headlines
changed since the last scoring — unchanged stocks keep their score. Falls back to a keyword lexicon.
Refreshed in the background every 30 min while a market is open, every 2 h otherwise; never blocks trading.
"""
import email.utils
import html
import json
import re
import subprocess
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from . import llm
from .markets import NAMES, is_open

from trader import config as C
CACHE = C.STATE_DIR / "news.json"
REFRESH = 30 * 60          # while the US or KR market is open
REFRESH_OFF = 2 * 3600     # both closed: headlines barely change and nothing trades on them

# Outlets whose headlines may move a score. Matched against the RSS <source> name (or the " - Outlet" title suffix).
TRUSTED = {
    "KR": ("연합뉴스", "한국경제", "한경", "매일경제", "매경", "머니투데이", "이데일리", "조선비즈", "뉴스핌"),
    "US": ("reuters", "bloomberg", "cnbc", "wall street journal", "wsj", "marketwatch", "barron's", "barrons"),
}
# The trusted outlets' own RSS feeds (outlet, url). Google News rarely surfaces these for US stocks, so without
# them most US names had no news signal at all. Reuters/Bloomberg/Barron's publish no free feed.
FEEDS = {
    "US": [("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),      # top news
           ("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),       # markets
           ("CNBC", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),       # finance
           ("CNBC", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),       # earnings
           ("CNBC", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),       # technology
           ("CNBC", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),       # business
           ("CNBC", "https://www.cnbc.com/id/10000108/device/rss/rss.html"),       # health care
           ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
           ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
           ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
           ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_bulletins"),
           ("Wall Street Journal", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
           ("Wall Street Journal", "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness"),
           ("Wall Street Journal", "https://feeds.content.dowjones.io/public/rss/RSSWSJD")],
    "KR": [("연합뉴스", "https://www.yna.co.kr/rss/economy.xml"),
           ("한국경제", "https://www.hankyung.com/feed/finance"),
           ("한국경제", "https://www.hankyung.com/feed/economy"),
           ("머니투데이", "https://rss.mt.co.kr/mt_news.xml"),
           ("이데일리", "http://rss.edaily.co.kr/stock_news.xml"),
           ("조선비즈", "https://biz.chosun.com/arc/outboundfeeds/rss/category/stock/?outputType=xml"),
           ("조선비즈", "https://biz.chosun.com/arc/outboundfeeds/rss/category/industry/?outputType=xml"),
           ("조선비즈", "https://biz.chosun.com/arc/outboundfeeds/rss/category/it-science/?outputType=xml"),
           ("연합뉴스", "https://www.yna.co.kr/rss/market.xml")],
}
_BAD_XML = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def _parse_xml(raw: bytes):
    try:
        return ET.fromstring(raw)
    except ET.ParseError:   # some KR feeds ship control characters / bare ampersands
        fixed = re.sub(rb"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", b"&amp;", _BAD_XML.sub(b"", raw))
        return ET.fromstring(fixed)
MAX_AGE = 2 * 86400
_feeds = {"t": 0.0, "items": {"US": [], "KR": []}}
# First words that are not a company on their own ("Advanced", "American", "General" …)
GENERIC = {"advanced", "american", "applied", "first", "general", "global", "international", "national", "new", "united",
           "world", "digital", "energy", "capital", "financial", "north", "south", "west", "east", "pacific", "atlantic",
           "standard", "universal", "western", "southern", "northern", "eastern", "royal", "super", "micro", "health",
           "medical", "power", "solar", "gold", "silver", "oil", "gas", "united", "modern", "liberty", "central"}
_CORP = re.compile(r"\b(inc|corp|corporation|co|ltd|plc|holdings?|group|company|class [a-c]|the)\b\.?", re.I)
_state = {"updated": 0, "symbols": {}}
_lock = threading.Lock()
if CACHE.exists():
    try:
        _state = json.loads(CACHE.read_text())
    except Exception:
        pass


def _outlet(item) -> str:
    src = (item.findtext("source") or "").strip()
    if not src and " - " in (item.findtext("title") or ""):
        src = item.findtext("title").rsplit(" - ", 1)[1]
    return src


def _strip_outlet(title, outlet):
    return title[: -len(outlet) - 3].strip() if outlet and title.endswith(" - " + outlet) else title.strip()


def trusted(outlet: str, market: str) -> bool:
    """Outlet name starts with (or contains as a separate word) a trusted name — so 마이데일리 ≠ 이데일리."""
    o = outlet.lower()
    return any(re.search(r"(?<!\w)" + re.escape(t.lower()), o) for t in TRUSTED[market])


def _name_keys(sym):
    """Ways a headline can name the stock by NAME (the ticker is matched separately, case-sensitively)."""
    name = NAMES.get(sym, "")
    keys = set()
    if name:
        keys |= {name.lower(), name.replace(" ", "").lower()}
        core = re.sub(r"\s+", " ", _CORP.sub("", name).replace(",", " ")).strip()
        if len(core) >= 3:
            keys.add(core.lower())
            words = core.split()
            first = words[0].lower().removesuffix(".com")
            # "Apple", "Nvidia", "Insulet" — but not "Advanced" (Micro Devices) or 2–3 letter fragments
            if len(first) >= 4 and len(words) <= 2 and first not in GENERIC:
                keys.add(first)
    return keys


def names_company(title, sym) -> bool:
    t = title.lower()
    if not sym.isdigit() and re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", title):
        return True   # US ticker as its own word, case-sensitive ("NVDA", not "nvdaily")
    return any((k in t) if not k.isascii() else bool(re.search(rf"\b{re.escape(k)}\b", t))
               for k in _name_keys(sym) if len(k) >= 2)


def _dupe_key(title):
    return re.sub(r"[^0-9a-z가-힣]", "", title.lower())[:28]


def _parse_date(s):
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        return 0.0


def fetch_feeds():
    """Pull every trusted outlet's own feed once; keep the last 2 days. A feed that fails is skipped."""
    now, items = time.time(), {"US": [], "KR": []}
    for market, feeds in FEEDS.items():
        seen = set()
        for outlet, url in feeds:
            try:
                root = _parse_xml(requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12).content)
            except Exception as e:
                print(f"news feed {outlet} failed: {str(e)[:80]}", flush=True)
                continue
            for i in root.iter("item"):
                title = html.unescape(re.sub(r"<[^>]+>", "", i.findtext("title") or "")).strip()
                ts = _parse_date(i.findtext("pubDate") or "")
                k = _dupe_key(title)
                if not title or k in seen or (ts and now - ts > MAX_AGE):
                    continue
                seen.add(k)
                items[market].append({"title": title, "source": outlet, "link": i.findtext("link"),
                                      "date": i.findtext("pubDate"), "ts": ts})
        items[market].sort(key=lambda h: -h["ts"])
    with _lock:
        _feeds["t"], _feeds["items"] = now, items
    print(f"news feeds: {len(items['US'])} US + {len(items['KR'])} KR headlines from trusted outlets", flush=True)


def feed_headlines(sym: str, market: str) -> list[dict]:
    with _lock:
        items = list(_feeds["items"].get(market, []))
    return [{k: v for k, v in h.items() if k != "ts"} for h in items if names_company(h["title"], sym)]


def headlines(sym: str, n=6) -> list[dict]:
    """Direct-feed headlines naming the stock first, then Google News (trusted outlets only); n newest, deduplicated."""
    market = "KR" if sym.isdigit() else "US"
    out, seen = [], set()
    for h in feed_headlines(sym, market):
        k = _dupe_key(h["title"])
        if k not in seen:
            seen.add(k); out.append(h)
    for h in _google_headlines(sym, market):
        k = _dupe_key(h["title"])
        if k not in seen:
            seen.add(k); out.append(h)
    return out[:n]


def _google_headlines(sym: str, market: str, n=6) -> list[dict]:
    if market == "KR":
        q, loc = f'"{NAMES.get(sym, sym)}" when:2d', "hl=ko&gl=KR&ceid=KR:ko"
    else:
        q, loc = f"{sym} stock when:2d", "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{loc}"
    try:
        root = ET.fromstring(requests.get(url, timeout=10).content)
    except Exception:
        return []
    out, seen = [], set()
    for i in root.iter("item"):   # look through the whole feed, keep the first n that pass all three checks
        outlet = _outlet(i)
        title = _strip_outlet(i.findtext("title") or "", outlet)
        if not title or not trusted(outlet, market) or not names_company(title, sym):
            continue
        k = _dupe_key(title)
        if k in seen:
            continue
        seen.add(k)
        out.append({"title": title, "source": outlet, "link": i.findtext("link"), "date": i.findtext("pubDate")})
        if len(out) >= n:
            break
    return out


POS = r"beat|surge|record|upgrade|soar|jump|rally|strong|raise|growth|buyback|상승|급등|호실적|최대|흑자|상향|수주|돌파"
NEG = r"miss|plunge|downgrade|fall|drop|lawsuit|probe|recall|weak|cut|loss|delay|하락|급락|적자|하향|소송|리콜|우려|부진"


def _lexicon(titles):
    s = sum(len(re.findall(POS, t, re.I)) - len(re.findall(NEG, t, re.I)) for t in titles)
    return max(-1.0, min(1.0, s / max(3, len(titles))))


def _llm_score(batch: dict) -> dict:
    prompt = ("You are a sell-side news analyst. For each stock, rate the net short-term price impact "
              "of its recent headlines from -1 (very negative) to +1 (very positive); 0 if irrelevant/mixed. "
              "Return ONLY compact JSON: {\"SYMBOL\": {\"score\": float, \"why\": \"<=12 words\"}}.\n\n"
              + json.dumps({s: [h["title"] for h in hs] for s, hs in batch.items()}, ensure_ascii=False))
    r = llm.haiku(prompt, timeout=180)
    if r.returncode != 0:
        print(f"news: claude exited {r.returncode}: {r.stderr[:300]}", flush=True)
    m = re.search(r"\{.*\}", r.stdout, re.S)
    return json.loads(m.group(0)) if m else {}


def _sig(hs):
    return sorted(_dupe_key(h["title"]) for h in hs)


def refresh(symbols):
    fetch_feeds()
    fetched = {s: headlines(s) for s in symbols}
    now = time.time()
    with _lock:
        prev = dict(_state["symbols"])
    # only stocks whose trusted headlines changed go to Claude; the rest keep their score (and stay fresh)
    changed = {s: h for s, h in fetched.items() if h and (prev.get(s, {}).get("sig") != _sig(h)
                                                          or prev.get(s, {}).get("engine") != "claude-haiku")}
    try:
        scored = _llm_score(changed) if changed else {}
        engine = "claude-haiku"
    except Exception as e:
        print("news LLM failed, using lexicon:", e)
        scored, engine = {}, "lexicon"
    out = {}
    for s, hs in fetched.items():
        if not hs:   # no trusted headline naming it: no news signal (instead of spam or another company's story)
            out[s] = {"score": 0.0, "why": "no trusted headlines", "headlines": [], "engine": "none", "t": now, "sig": []}
        elif s not in changed:
            out[s] = {**prev[s], "headlines": hs[:4], "t": now}
        else:
            r = scored.get(s) or {"score": _lexicon([h["title"] for h in hs]), "why": "keyword scan"}
            out[s] = {"score": max(-1.0, min(1.0, float(r.get("score", 0)))), "why": r.get("why", ""),
                      "headlines": hs[:4], "engine": engine if s in scored else "lexicon", "t": now, "sig": _sig(hs)}
    with _lock:
        _state["symbols"].update(out)
        _state["llm_calls"] = _state.get("llm_calls", 0) + bool(changed)
        _state["last_batch"] = {"t": now, "stocks": len(fetched), "rescored": len(changed)}
        # mostly-failed LLM pass → try again in ~5 min instead of waiting a full interval
        _state["updated"] = now - (interval() - 300 if changed and len(scored) < len(changed) / 2 else 0)
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(_state, ensure_ascii=False))


def interval():
    try:
        return REFRESH if is_open("US") or is_open("KR") else REFRESH_OFF
    except Exception:
        return REFRESH


def sentiment(sym: str) -> float:
    d = _state["symbols"].get(sym, {})
    if time.time() - d.get("t", time.time()) > 6 * 3600:   # old news no longer moves the score
        return 0.0
    return d.get("score", 0.0)


def snapshot():
    with _lock:
        return {"updated": _state.get("updated", 0), "symbols": dict(_state["symbols"])}


def start_background(get_symbols):
    def loop():
        while True:
            if time.time() - _state.get("updated", 0) > interval():
                try:
                    refresh(get_symbols())
                except Exception as e:
                    print("news refresh error:", e)
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="news").start()
