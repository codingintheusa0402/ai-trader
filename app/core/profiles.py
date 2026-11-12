"""5 risk levels × 7 horizons = 35 investment modes.

A horizon is a time box: positions are opened inside it and ALL are closed by its end.
Profit targets are aims scaled to the horizon's typical move — not guarantees.
"""
from dataclasses import dataclass, asdict

RISK = {
    1: dict(name="Very Low", vol=0.5, invest=0.5, max_pos=3, pos_pct=0.35, max_loss=0.03,
            US=["SPY", "VTI", "SCHD", "BND", "GLD"], KR=["069500", "360750", "148070"]),
    2: dict(name="Low", vol=0.75, invest=0.65, max_pos=4, pos_pct=0.30, max_loss=0.05,
            US=["SPY", "QQQ", "AAPL", "MSFT", "COST", "JNJ"], KR=["069500", "005930", "105560", "055550", "360750"]),
    3: dict(name="Medium", vol=1.0, invest=0.8, max_pos=5, pos_pct=0.25, max_loss=0.08,
            US=["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "JPM"],
            KR=["005930", "000660", "035420", "005380", "373220", "207940"]),
    4: dict(name="High", vol=1.5, invest=0.9, max_pos=5, pos_pct=0.25, max_loss=0.12,
            US=["NVDA", "TSLA", "AMD", "PLTR", "COIN", "MU", "NFLX", "AVGO"],
            KR=["000660", "012450", "042700", "086520", "247540", "196170"]),
    5: dict(name="Very High", vol=2.2, invest=1.0, max_pos=3, pos_pct=0.40, max_loss=0.20,
            US=["TQQQ", "SOXL", "MSTR", "SMCI", "COIN", "TSLA", "HOOD"],
            KR=["122630", "233740", "086520", "247540", "042700"]),
}

# bar = data interval; fast/slow/mom in bars; tick = seconds between decisions
# tp/sl = per-trade take-profit/stop before the risk multiplier (Very High × 1 Hour = +3.3%).
# Short horizons were boosted ×2.5 tapering to ×1.0 at 6 Months (2026-10-10).
HORIZON = {
    "1h": dict(label="1 Hour",   secs=3600,        bar="1m",  fast=5,  slow=20, mom=15, tick=30,   tp=0.015, sl=0.01),
    "6h": dict(label="6 Hours",  secs=6 * 3600,    bar="5m",  fast=6,  slow=24, mom=12, tick=60,   tp=0.0308, sl=0.0164),
    "1d": dict(label="1 Day",    secs=86400,       bar="15m", fast=8,  slow=26, mom=16, tick=120,  tp=0.0442, sl=0.023),
    "3d": dict(label="3 Days",   secs=3 * 86400,   bar="60m", fast=8,  slow=30, mom=20, tick=300,  tp=0.0627,  sl=0.0345),
    "1w": dict(label="1 Week",   secs=7 * 86400,   bar="60m", fast=13, slow=50, mom=35, tick=600,  tp=0.0857,  sl=0.05),
    "1M": dict(label="1 Month",  secs=30 * 86400,  bar="1d",  fast=10, slow=30, mom=20, tick=1800, tp=0.1218,  sl=0.0731),
    "6M": dict(label="6 Months", secs=182 * 86400, bar="1d",  fast=20, slow=60, mom=60, tick=3600, tp=0.25,  sl=0.12),
}


@dataclass
class Profile:
    risk: int
    horizon: str
    risk_name: str
    horizon_label: str
    secs: int
    bar: str
    fast: int
    slow: int
    mom: int
    tick: int
    take_profit: float
    stop_loss: float
    invest_frac: float
    max_positions: int
    pos_pct: float
    max_loss: float          # plan halts if equity drops this much from budget
    universe: dict

    def to_dict(self): return asdict(self)


# Trading styles (2026-10-19). "swing": short-term in & out inside any window — 15-minute bars, decisions every
# 2 min, small quick take-profits/stops (High risk: +3.0% / −1.8%), and flat before each session's close (no overnight
# gaps). It profits from intraday ups (stocks) and downs (inverse ETFs, when inverse is on). The window (how long the
# plan runs) still comes from the horizon.
STYLES = {"normal": "Normal", "swing": "Swing (short-term in & out)"}
SWING = dict(bar="15m", fast=8, slow=26, mom=16, tick=120, tp=0.02, sl=0.012)


def make(risk: int, horizon: str, style: str = "normal") -> Profile:
    r, h = RISK[risk], dict(HORIZON[horizon])
    if style == "swing":
        h.update(SWING, label=h["label"] + " · Swing")
    return Profile(risk, horizon, r["name"], h["label"], h["secs"], h["bar"], h["fast"], h["slow"],
                   h["mom"], h["tick"], round(h["tp"] * r["vol"], 4), round(h["sl"] * r["vol"], 4),
                   r["invest"], r["max_pos"], r["pos_pct"], r["max_loss"],
                   {"US": r["US"], "KR": r["KR"]})


def matrix():
    return {"risks": {k: v["name"] for k, v in RISK.items()},
            "horizons": {k: v["label"] for k, v in HORIZON.items()},
            "cells": {f"{r}_{h}": make(r, h).to_dict() for r in RISK for h in HORIZON}}
