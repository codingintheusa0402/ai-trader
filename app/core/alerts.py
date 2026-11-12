"""Plain-text alert messages: no emojis or Chat markup anywhere, and real-money trade
alerts cut down to who / what / why. Everything posted to the alert webhook (trade alerts, the daily report, engine
notices) goes through plain(), so it reads well in any chat app. Standard library only."""
import re

# emoji + pictographs, dingbats/misc symbols (✅ ❌ ⚠ ✔), variation selectors, zero-width joiner, keycaps
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F"
                    "\U0000200D\U000020E3\U0001F1E6-\U0001F1FF]")
_BOLD = re.compile(r"\*([^*\n]+)\*")
_ITAL = re.compile(r"(?<![\w])_([^_\n]+)_(?![\w])")


def plain(text: str) -> str:
    """Strip emojis and Google Chat *bold* / _italic_ markers; tidy the spaces they leave behind."""
    t = _ITAL.sub(r"\1", _BOLD.sub(r"\1", _EMOJI.sub("", str(text))))
    return "\n".join(re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in t.split("\n")).strip()


# The ETFs the plans may trade have no entry in markets.NAMES (that dict also drives news search)
ETF_NAMES = {
    "SH": "ProShares Short S&P 500", "PSQ": "ProShares Short QQQ", "SDS": "ProShares UltraShort S&P 500",
    "QID": "ProShares UltraShort QQQ", "SQQQ": "ProShares UltraPro Short QQQ", "SPXU": "ProShares UltraPro Short S&P 500",
    "SOXS": "Direxion Semiconductor Bear 3X", "SSO": "ProShares Ultra S&P 500", "QLD": "ProShares Ultra QQQ",
    "UPRO": "ProShares UltraPro S&P 500", "TQQQ": "ProShares UltraPro QQQ", "SOXL": "Direxion Semiconductor Bull 3X",
    "114800": "KODEX 인버스", "251340": "KODEX 코스닥150선물인버스", "252670": "KODEX 200선물인버스2X",
    "122630": "KODEX 레버리지", "233740": "KODEX 코스닥150 레버리지",
}


def stock_label(sym, names=None, info=None) -> str:
    """'Tesla (TSLA)', '삼성전자 (005930)'; just the ticker when no name is known."""
    name = (names or {}).get(sym) or ETF_NAMES.get(sym) or ((info or {}).get("name") if info else None)
    return f"{name} ({sym})" if name and name != sym else sym


BUY_WORDS = {"trend": "a strong upward trend", "momentum": "rising momentum",
             "reversion": "a recent dip that looked due to bounce back", "breakout": "a breakout to new highs",
             "news": "positive news"}


def _cap(s):
    return s[:1].upper() + s[1:]


def buy_reason(weights, sig, inverse=False, sym_bias=0.0, recall=(0.0, 0)) -> str:
    """1–2 plain sentences: the strongest signals that pushed the buy, plus one supporting fact if there is one."""
    if inverse:
        first = "The market was falling, so it bought a fund that rises when the market drops."
    else:
        top = sorted(((weights.get(k, 0) * sig.get(k, 0), k) for k in BUY_WORDS), reverse=True)
        keys = []
        for c, k in top:   # strongest two; a breakout and a dip-bounce contradict each other, keep the stronger
            if c > 0.02 and sig.get(k, 0) > 0 and len(keys) < 2 and not {k, *keys} >= {"breakout", "reversion"}:
                keys.append(k)
        first = (_cap(" and ".join(BUY_WORDS[k] for k in keys)) + "." if keys
                 else "Its signals were positive overall, though none stood out.")
    avg, n = recall
    second = ("Trading was much busier than usual." if (sig.get("_rvol") or 0) >= 1.5 else
              "Similar setups have worked well for it before." if n >= 6 and avg > 0 else
              "It has done well on this stock before." if sym_bias > 0 else "")
    return f"{first} {second}".strip()


def sell_reason(reason: str) -> str:
    """The engine's exit reason (which carries numbers) → one plain sentence, no numbers."""
    r = (reason or "").lower()
    table = [
        ("end-of-day profit", "End-of-day: it sold a gain before the close so the profit is realized."),
        ("side switch", "Market call changed, so it sold to switch sides."),
        ("take profit", "Take-profit: it rose to its profit target."),
        ("trailing stop", "Take-profit: it pulled back from its peak, so it locked in the gain."),
        ("stop loss", "Stop-loss: it fell to its loss limit."),
        ("month-end", "Month-end: the monthly cycle sells everything before the month closes."),
        ("horizon ended", "Time limit: the plan's time window ended."),
        ("flat before the close", "Time limit: swing trades are closed before the market closes."),
        ("held a day", "Time limit: leveraged market-drop funds are held for one day at most."),
        ("market turned up", "The market turned up, so it closed its bet against the market."),
        ("signal turned", "Its signals turned negative, so it got out."),
        ("target reached", "Take-profit: the whole plan reached its profit goal."),
        ("profit floor", "Take-profit: the plan's gain slipped back to its minimum, so it locked it in."),
        ("max loss", "Stop-loss: the whole plan reached its maximum loss, so it sold everything."),
        ("stopped by you", "You stopped the plan."),
        ("kill switch", "Kill switch: it sold everything."),
    ]
    return next((txt for key, txt in table if key in r), "The plan is closing its positions.")


def buy_text(trader, label, reason) -> str:
    return f"{trader} bought {label}\nWhy: {reason}"


def sell_text(trader, label, reason) -> str:
    return f"{trader} sold {label}\nWhy: {reason}"


SHORT = (("end-of-day", "end-of-day profit"), ("side switch", "side switch"), ("take profit", "take-profit"), ("trailing stop", "trailing stop"),
         ("stop loss", "stop-loss"), ("month-end", "month-end"), ("horizon ended", "time limit"), ("flat before", "flat before close"),
         ("held a day", "time limit"), ("market turned up", "market turned up"), ("signal turned", "signal turned"),
         ("max loss", "loss limit"), ("combined loss", "loss limit"), ("stopped by you", "stopped by you"), ("kill switch", "kill switch"))


def short_reason(reason: str) -> str:
    r = (reason or "").lower()
    return next((txt for key, txt in SHORT if key in r), "closed")


def usd(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def split(lines, limit=195):
    """Whole lines into KakaoTalk-sized messages (200 characters each), numbered when there is more than one."""
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + 1 + len(ln) > limit - 6:
            out.append(cur); cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return [f"({i}/{len(out)}) {t}" for i, t in enumerate(out, 1)] if len(out) > 1 else out
