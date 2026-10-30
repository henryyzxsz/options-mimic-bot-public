"""Paper backtest on 10-minute underlying bars with Black-Scholes-approximated option prices.
Rough by construction (no real option quotes, 10-min granularity, fixed IV). Use for comparing
profiles (mimic vs disciplined), not for expecting real P&L."""
import json, random, re, statistics as st
from datetime import datetime, timedelta
from pathlib import Path
from .rules import load_config, load_profile
from .signals import propose
from .guardrails import check
from .broker import PaperBroker
from .pricing import bs_price, iv_for

ROOT = Path(__file__).resolve().parent.parent
BARS_PATH = ROOT / "data" / "underlying_10m.json"


def load_bars_10m(path=BARS_PATH):
    """Your own 10-minute bars: {"SPY": [[iso_ts_ET, open, high, low, close], ...], ...}. Not included in the repo."""
    try:
        return json.load(open(path))
    except FileNotFoundError:
        raise SystemExit(f"backtest needs your own bar data at {path} (format in mimic_bot/backtest.py)")

def run(profile_name="disciplined", mode="fade", symbols=("SPY", "QQQ"), seed=0, verbose=False):
    cfg = load_config(); prof = load_profile(cfg, profile_name)
    bars = load_bars_10m()
    broker = PaperBroker(cfg["account"]["paper_starting_equity"])
    rng = random.Random(seed)
    tgt, stop, tstop = cfg["exit"]["profit_target_pct"], cfg["exit"]["stop_loss_pct"], cfg["exit"]["time_stop_min"]
    eff_stop = stop if prof.honor_stop else min(-0.95, stop)   # stop not honored: only a near-total loss exits early
    skipped = {}
    timeline = sorted({b[0] for s in symbols for b in bars[s]})
    idx = {s: {b[0]: n for n, b in enumerate(bars[s])} for s in symbols}
    last_size_mult = 1.0
    for ts in timeline:
        now = datetime.fromisoformat(ts)
        # manage open positions
        for p in list(broker.open):
            b = bars[p["symbol"]][idx[p["symbol"]][ts]] if ts in idx[p["symbol"]] else None
            if b is None: continue
            mins_left = (now.replace(hour=16, minute=0) - now).total_seconds() / 60 + 10
            px = max(0.01, bs_price(b[4], p["strike"], max(mins_left - 10, 1), iv_for(p["symbol"]), p["kind"]))
            ret = (px - p["premium"]) / p["premium"]; held = (now - p["time"]).total_seconds() / 60 + 10
            if ret >= tgt: broker.sell(p, px, now, "target")
            elif ret <= eff_stop: broker.sell(p, px, now, "stop")
            elif held >= tstop: broker.sell(p, px, now, "time")
            elif now.hour == 15 and now.minute >= 50: broker.sell(p, px, now, "eod")
            if p not in broker.open:
                last_size_mult = prof.size_mult_after_loss if p["pnl"] < 0 else 1.0
        if broker.open: continue
        for s in rng.sample(list(symbols), len(symbols)):
            if ts not in idx[s]: continue
            prop = propose(s, bars[s], idx[s][ts], cfg, mode=mode, rng=rng)
            if not prop: continue
            if not prof.no_size_up_after_loss and last_size_mult > 1:
                prop["contracts"] = max(1, round(prop["contracts"] * last_size_mult))
            if prof.max_risk_pct_of_account:   # size down to the risk budget instead of skipping
                per = prop["premium"] * 100 * abs(stop)
                prop["contracts"] = max(1, min(prop["contracts"], int(prof.max_risk_pct_of_account * broker.equity // per) or 1))
            if prof.name == "mimic" and rng.random() > cfg["profiles"]["mimic"].get("take_prob", 1.0):
                continue
            why = check(prop, broker, prof, cfg)
            if why:
                for w in why: k = re.sub(r"\$[\d.]+", "$x", w.split(" (")[0]); skipped[k] = skipped.get(k, 0) + 1
                continue
            broker.buy(prop); break
    T = broker.trades
    res = dict(profile=profile_name, mode=mode, trades=len(T))
    if T:
        res.update(win_rate=round(sum(t["pnl"] > 0 for t in T) / len(T), 3), pnl=round(sum(t["pnl"] for t in T), 2),
                   avg_pct=round(st.mean(t["pnl_pct"] for t in T), 4), end_equity=round(broker.equity, 2),
                   exits={r: sum(t["exit_reason"] == r for t in T) for r in ("target", "stop", "time", "eod")})
    res["skipped_by_guardrail"] = skipped
    if verbose:
        for t in T[-10:]:
            print(f"{t['time']:%m-%d %H:%M} {t['symbol']} {t['strike']}{t['kind'][0].upper()} x{t['contracts']} ${t['premium']:.2f}->${t['exit_premium']:.2f} {t['exit_reason']} ${t['pnl']:.0f}")
    return res
