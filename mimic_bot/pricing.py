"""Black-Scholes approximation used ONLY for paper/backtest pricing when real option quotes are absent.
0DTE options are poorly modelled by BS (IV smile, intraday IV crush); treat backtest P&L as rough."""
import math

def _cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_price(S, K, minutes_to_expiry, iv, kind):
    T = max(minutes_to_expiry, 1) / (252 * 390)          # trading-minute year
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T)); d2 = d1 - iv * math.sqrt(T)
    if kind == "call": return S * _cdf(d1) - K * _cdf(d2)
    return K * _cdf(-d2) - S * _cdf(-d1)

DEFAULT_IV = {"SPY": 0.16, "QQQ": 0.20, "IWM": 0.22}      # assumptions, not measured; override with real quotes
def iv_for(sym): return DEFAULT_IV.get(sym, 0.40)
