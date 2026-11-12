"""All tunable settings. Change here, not in code."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("AITRADER_STATE_DIR") or ROOT / "state")   # tests point this at a temp folder

# ── Fake money ──────────────────────────────────────────────
STARTING_CASH = {"KRW": 10_000_000, "USD": 10_000}

# ── Universe (yfinance tickers) ─────────────────────────────
UNIVERSE = {
    "KR": ["005930.KS", "000660.KS", "035420.KS", "005380.KS", "373220.KS",
           "207940.KS", "068270.KS", "105560.KS", "035720.KS", "012450.KS"],
    "US": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA",
           "JPM", "COST"],
}
BENCHMARK = {"KR": "^KS11", "US": "SPY"}
CURRENCY = {"KR": "KRW", "US": "USD"}

# ── Costs (approx. KIS 뱅키스 / 미국주식 온라인) ──────────────────
FEES = {
    "KR": {"commission": 0.00015, "sell_tax": 0.0020},  # 증권거래세+농특세
    "US": {"commission": 0.0025, "sell_tax": 0.0},
}
SLIPPAGE = 0.0005  # 0.05% worse than quoted price on every fill

# ── Strategy (trend-following + momentum) ───────────────────
FAST_MA, SLOW_MA = 20, 50
RSI_LEN, RSI_MAX_ENTRY = 14, 70
TRAILING_STOP = 0.15         # exit if price falls 15% from peak since entry

# ── Risk ────────────────────────────────────────────────────
MAX_POSITION_PCT = 0.12      # of market equity per stock
MAX_POSITIONS = 8
MIN_CASH_PCT = 0.05          # always keep 5% cash
