"""Simple fade signal for the configured style profile.

Single-leg long 0DTE calls/puts, strike near the configured OTM target, premium inside [premium_min, premium_max].
`mode="fade"` (default): buy calls after the underlying dipped over the prior 30 min, puts after it ripped.
`mode="mimic_raw"`: picks chase or fade at random (a noisy comparison baseline).
"""
import math, random
from datetime import datetime, date, timedelta
from .pricing import bs_price, iv_for

STRIKE_STEP = {"SPY": 1, "QQQ": 1, "IWM": 1}

def round_strike(sym, x, kind):
    step = STRIKE_STEP.get(sym, 2.5 if x > 100 else 1 if x > 20 else 0.5)
    return (math.ceil(x / step) * step) if kind == "call" else (math.floor(x / step) * step)

def propose(sym, bars, i, cfg, mode="fade", rng=None, quote_fn=None):
    """bars: list of [iso_ts_ET, o, h, l, c] (10-min). i: index of the bar we are deciding at (use its open).
    quote_fn(sym, strike, kind, expiry) -> premium, if real quotes available (live paper mode)."""
    rng = rng or random.Random(0)
    ts = datetime.fromisoformat(bars[i][0]); S = bars[i][1]
    if i < 3 or bars[i - 3][0][:10] != bars[i][0][:10]:
        return None
    prior = bars[i - 3][1]
    move = (S - prior) / prior * 100
    thr = cfg["entry"]["fade_min_move_pct"]
    if abs(move) < thr:
        return None
    if mode == "fade":
        kind = "call" if move < 0 else "put"
    else:  # raw mode: chase or fade at random, slightly call-skewed
        kind = ("put" if move < 0 else "call") if rng.random() < 0.52 else ("call" if move < 0 else "put")
    otm = cfg["contract"]["otm_pct_target"] / 100
    K = round_strike(sym, S * (1 + otm) if kind == "call" else S * (1 - otm), kind)
    close = ts.replace(hour=16, minute=0)
    mins = (close - ts).total_seconds() / 60
    prem = quote_fn(sym, K, kind, ts.date()) if quote_fn else bs_price(S, K, mins, iv_for(sym), kind)
    if not quote_fn and mins < 30:   # BS is meaningless for the last 30 min of a 0DTE; need real quotes there
        return None
    if prem is None or prem < 0.05:
        return None
    lo, hi = cfg["contract"]["premium_min"], cfg["contract"]["premium_max"]
    if prem > hi:  # move the strike further OTM to get under premium_max
        for _ in range(10):
            K = K + (STRIKE_STEP.get(sym, 1) if kind == "call" else -STRIKE_STEP.get(sym, 1))
            prem = quote_fn(sym, K, kind, ts.date()) if quote_fn else bs_price(S, K, mins, iv_for(sym), kind)
            if prem <= hi: break
    budget = cfg["sizing"]["cost_per_trade_median"]
    contracts = max(1, min(3, round(budget / (prem * 100))))
    return dict(time=ts, symbol=sym, kind=kind, strike=K, expiry=ts.date().isoformat(), premium=round(prem, 2),
                contracts=contracts, underlying=S, prior30_move_pct=round(move, 3),
                thesis=f"{'fade dip' if kind=='call' else 'fade rip'}: {sym} moved {move:+.2f}% in 30m",
                earnings_before_expiry=False)
