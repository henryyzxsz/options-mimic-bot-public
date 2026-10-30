"""Trigger-based entry logic on 5-minute bars.

  * TURN (dip-and-turn / rip-and-roll): the underlying moved one way for ~15 min, then the last 15 min turned
    back. Trade in the direction of the turn (calls after a dip starts bouncing).
  * MOMENTUM: both halves of the last 30 min point the same way; chase it.
  * OPENING DRIVE: 09:35-10:00 on a gap day, follow the first move.
  * LEVEL TOUCH: price tags VWAP / prior-day high-low / a round number; play the bounce.
  * LATE-DAY LOTTERY: 15:30+ cheap 0DTE in the direction of the last 15 min.
  * NEWS: a headline naming the ticker in the last 15 min -> trade the reaction (backtest only, bring your own feed).
  * EARNINGS: report after the close today -> afternoon position in the day's direction.

Every trigger returns a *suggestion*. Nothing here can place an order (see broker.LiveOrderTicket).
mode="mimic"       -> all triggers
mode="disciplined" -> turn, level and news only, 10:00-15:00 ET
mode="active"      -> disciplined triggers plus momentum (gated by the SPY 200-day rule in paper_loop);
                      the entry window comes from the profile
Thresholds in DEFAULTS are example values.
"""
import json, math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ET = timezone(timedelta(hours=-4))      # fixed EDT offset; paper_loop.to_tape_bars compensates outside DST
UTC = timezone.utc

DEFAULTS = dict(  # example thresholds
    turn_early_min=0.15,   # % move in the first half of the 30-min window
    turn_recent_min=0.12,  # % move back in the last 15 min
    mom_min=0.25,          # % move in the last 15 min for a momentum chase (same sign as the earlier 15)
    open_drive_min=0.15,   # % from open, 09:35-10:00, on a gap >= gap_min
    gap_min=0.30,
    level_band=0.03,       # % distance that counts as "at the level"
    late_min=0.05,         # % last-15m move for a late-day lottery entry
    cooldown_min=30,       # minutes before the same symbol can fire again
    news_window_min=15,
)
MODES = {
    "mimic": ["earnings", "news", "open_drive", "turn", "level", "momentum", "late_lottery"],
    "disciplined": ["news", "turn", "level"],
    "active": ["news", "turn", "level", "momentum"],   # momentum is gated by the SPY 200-day rule in paper_loop
}

def _p(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))

def load_bars(path=ROOT / "data" / "bars_5m.json"):
    """Your own 5-min bars: {"SPY": [[iso_ts_utc, o, h, l, c, volume], ...]}. Not included in the repo."""
    try:
        raw = json.load(open(path))
    except FileNotFoundError:
        raise SystemExit(f"needs your own 5-min bar data at {path} (format in mimic_bot/triggers.py)")
    out = {}
    for sym, v in raw.items():
        out[sym] = [[_p(b[0])] + b[1:] for b in v if "13:30" <= b[0][11:16] < "20:00"]
    return out

class Tape:
    """5-min bars for one symbol with per-day indexes. Decisions at time t only see bars that closed by t."""
    def __init__(self, bars):
        self.b = bars; self.end = [x[0] + timedelta(minutes=5) for x in bars]
        self.day_first, self.prev_day = {}, {}
        days = []
        for n, x in enumerate(bars):
            d = x[0].date()
            if d not in self.day_first: self.day_first[d] = n; days.append(d)
        for k in range(1, len(days)): self.prev_day[days[k]] = (self.day_first[days[k - 1]], self.day_first[days[k]] - 1)
        self._vw = {}
    def idx(self, t):
        return bisect_right(self.end, t) - 1
    def features(self, i):
        b = self.b; d = b[i][0].date(); f0 = self.day_first[d]
        if i < f0: return None
        c = b[i][4]; o = b[f0][1]
        def back(k): j = i - k; return b[j][4] if j >= f0 else (o if j == f0 - 1 else None)
        c3, c6 = back(3), back(6)
        seg = b[f0:i + 1]
        vol = sum(x[5] for x in seg) or 1
        vwap = sum((x[2] + x[3] + x[4]) / 3 * x[5] for x in seg) / vol
        pr = self.prev_day.get(d)
        pc = b[pr[1]][4] if pr else None
        pdh = max(x[2] for x in b[pr[0]:pr[1] + 1]) if pr else None
        pdl = min(x[3] for x in b[pr[0]:pr[1] + 1]) if pr else None
        step = 10 ** math.floor(math.log10(c)) / 20
        t_et = (b[i][0] + timedelta(minutes=5)).astimezone(ET)
        pct = lambda a, z: (a / z - 1) * 100 if a and z else None
        return dict(t=t_et, px=c, ret15=pct(c, c3), early15=pct(c3, c6) if c6 else None, from_open=pct(c, o),
                    gap=pct(o, pc), vwap=pct(c, vwap), pdh=pct(c, pdh), pdl=pct(c, pdl), rnd=pct(c, round(c / step) * step),
                    hi=max(x[2] for x in seg), lo=min(x[3] for x in seg), last_lo=b[i][3], last_hi=b[i][2])

def evaluate(sym, f, p=DEFAULTS, mode="mimic", news=None, earnings=None):
    """Return the first matching trigger as (name, 'call'|'put', why) or None. f = Tape.features(...)."""
    if not f or f["ret15"] is None: return None
    hm = f["t"].strftime("%H:%M"); r15 = f["ret15"]; e15 = f["early15"]; d = lambda x: "call" if x > 0 else "put"
    allowed = MODES[mode]
    if not ("09:35" <= hm < "16:00"): return None
    if mode == "disciplined" and not ("10:00" <= hm < "15:00"): return None
    for name in allowed:
        if name == "earnings" and earnings and hm >= "15:00":
            rep = earnings.get(sym, {}).get(f["t"].date().isoformat())
            if rep == "pm" and abs(f["from_open"]) >= 0.10:
                return name, d(f["from_open"]), f"reports after the close; {f['from_open']:+.2f}% on the day"
        if name == "news" and news:
            h = news(sym, f["t"])
            if h and abs(r15) >= 0.05: return name, d(r15), f"headline {h['t']:%H:%M}: {h['text'][:80]}"
        if name == "open_drive" and hm <= "10:00" and f["gap"] is not None and abs(f["gap"]) >= p["gap_min"] \
                and abs(f["from_open"]) >= p["open_drive_min"]:
            return name, d(f["from_open"]), f"gap {f['gap']:+.2f}%, {f['from_open']:+.2f}% since open"
        if name == "turn" and e15 is not None and abs(e15) >= p["turn_early_min"] and abs(r15) >= p["turn_recent_min"] \
                and (e15 > 0) != (r15 > 0) and hm < "15:30":
            return name, d(r15), f"{e15:+.2f}% then {r15:+.2f}% (turn)"
        if name == "level" and hm < "15:30":
            for k in ("vwap", "pdh", "pdl", "rnd"):
                if f[k] is not None and abs(f[k]) <= p["level_band"] and abs(r15) >= 0.05:
                    # bounce off the level: coming down into it -> call, coming up into it -> put
                    return name, ("call" if r15 < 0 else "put"), f"at {k} ({f[k]:+.2f}%) after {r15:+.2f}%/15m"
        if name == "momentum" and e15 is not None and abs(r15) >= p["mom_min"] and (e15 > 0) == (r15 > 0) and hm < "15:30":
            return name, d(r15), f"{e15:+.2f}% then {r15:+.2f}% (chase)"
        if name == "late_lottery" and hm >= "15:30" and abs(r15) >= p["late_min"]:
            return name, d(r15), f"{hm} ET, {r15:+.2f}%/15m"
    return None

def scan_day(sym, tape, day, p=DEFAULTS, mode="mimic", news=None, earnings=None):
    """Edge-triggered signals over one session (fires once, then cooldown)."""
    out = []; last = None
    f0 = tape.day_first.get(day)
    if f0 is None: return out
    i = f0
    while i < len(tape.b) and tape.b[i][0].date() == day:
        f = tape.features(i)
        s = evaluate(sym, f, p, mode, news, earnings)
        if s and (last is None or (f["t"] - last) >= timedelta(minutes=p["cooldown_min"])):
            out.append(dict(t=f["t"], sym=sym, trigger=s[0], kind=s[1], why=s[2], px=f["px"])); last = f["t"]
        i += 1
    return out
