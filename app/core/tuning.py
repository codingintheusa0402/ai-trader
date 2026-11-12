"""Tuned real-money settings for 대길 and 아귀, applied once and only if you create state/app/tuning_pending.json.
The budgets below are placeholders (MAX_PLAN_USD caps them anyway). The league, dry runs and plans started normally keep
the standard rules from profiles.py. Positions held when this is applied keep their original exit rules."""
PENDING = "tuning_pending.json"
REBASE = "rebase_pending.json"   # {"vector": budget, "blaze": budget}: one-time reset to the real Toss balance, see Engine.apply_rebase
SPLIT = {"vector": 0.78, "blaze": 0.22}   # how a withdrawal comes out of the traders' budgets
DRIFT = 2.0              # USD: a smaller unexplained cash change is rounding, not a transfer
COMBINED_DROP = 0.15     # everything stops when real value falls this far below the combined budget

PLANS = {
    "vector": dict(       # 대길: aggressive both ways
        target_budget=150.0, horizon="1d",
        entry_delta=-0.10, tp=0.04, sl=0.02, max_pos=3, pos_pct=0.33, invest=1.0, order_share=0.33, inv_pct=0.50,
        sig_loss_only=True, sig_readings=4, sig_gap=840, stop_window=10800,
        regular_only=True, no_buy_last=900, no_pacing=True, no_hour_adj=True, one_x_only=True,
        market_call=True, call_move=-0.003, call_readings=2, call_gap=840, max_switches=2,
        eod_min=0.004, eod_keep=0.15, max_loss=0.20),
    "blaze": dict(        # 아귀: intraday; take-profit, stop and flat-before-close stay as they were
        target_budget=100.0, no_pacing=True, max_loss=0.20),
}


def apply(prof, t):
    """Overlay a plan's settings on its profile."""
    if t:
        if "invest" in t:
            prof.invest_frac = t["invest"]
        if "max_pos" in t:
            prof.max_positions = t["max_pos"]
        if "pos_pct" in t:
            prof.pos_pct = t["pos_pct"]
        if "max_loss" in t:
            prof.max_loss = t["max_loss"]
    return prof
DAILY_BUFFER = 0.005     # v1.12: share of the account kept back from the daily budget for fees/FX (min $10)
