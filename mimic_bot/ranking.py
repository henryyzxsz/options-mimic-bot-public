"""Setup scoring: rank trigger signals with a 0-1 quality score.

The raw triggers fire far more often than anyone would want to trade. This module gives every signal a
0-1 score from a few simple priors:

  * trigger   - prior win rate for the setup type (turn, level, news, momentum, ...)
  * level     - which level a "level" signal touched (VWAP, prior-day high/low, round number)
  * time      - prior record by entry window, shrunk toward 50/50 so small buckets don't swing the score
  * symbol    - prior record on the underlying, also shrunk
  * context   - trading against the day's trend vs with it

ALL NUMBERS BELOW ARE EXAMPLE PRIORS, not measured results. Replace them with numbers from your own
backtests or paper trading before relying on the score.

The paper loop only takes a signal when its score clears the profile's `min_score`, keeps at least
`min_gap_between_entries_min` minutes between entries across the whole watchlist, and when several symbols
fire on the same tick, takes the highest score first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Mapping

# EXAMPLE priors. (wins, n) records are shrunk toward even odds with PRIOR_N pseudo-trades.
PRIOR_N = 10
TRIGGER_WIN: dict[str, float] = {
    "news": 0.62, "turn": 0.64, "level": 0.60, "earnings": 0.52,
    "momentum": 0.46, "late_lottery": 0.40, "open_drive": 0.35,
}
LEVEL_FACTOR: dict[str, float] = {"vwap": 1.0, "rnd": 0.85, "pdh": 0.8, "pdl": 0.8}
TOD_RECORD: list[tuple[time, tuple[int, int]]] = [   # (window start ET, (wins, n)) - example
    (time(9, 30), (4, 10)), (time(10, 0), (6, 10)), (time(11, 0), (6, 10)),
    (time(13, 0), (4, 10)), (time(15, 0), (4, 10)),
]
SYMBOL_RECORD: dict[str, tuple[int, int]] = {"SPY": (5, 10), "QQQ": (6, 10), "IWM": (6, 10)}   # example
SYMBOL_ALIAS: dict[str, str] = {"XSP": "SPY", "SPX": "SPY", "SPXW": "SPY"}
AGAINST_DAY_WIN, WITH_DAY_WIN = 0.62, 0.44   # example

WEIGHTS: dict[str, float] = {"trigger": 0.45, "time": 0.25, "symbol": 0.10, "context": 0.20}


def shrink(wins: int, n: int, k: int = PRIOR_N) -> float:
    """Win rate pulled toward even odds by k pseudo-trades (so 4/5 doesn't count as 80%)."""
    return (wins + 0.5 * k) / (n + k)


def tod_win(t: datetime | time) -> float:
    hm = t.time() if isinstance(t, datetime) else t
    rec = None
    for start, r in TOD_RECORD:
        if hm >= start: rec = r
    return shrink(*rec) if rec else 0.5


def symbol_win(sym: str) -> float:
    rec = SYMBOL_RECORD.get(SYMBOL_ALIAS.get(sym, sym))
    return shrink(*rec) if rec else 0.5


def level_kind(why: str) -> str | None:
    """'at vwap (+0.01%) after ...' -> 'vwap'."""
    if why.startswith("at "):
        return why.split()[1]
    return None


@dataclass(frozen=True)
class Scored:
    score: float
    parts: dict[str, float]


def score_signal(sym: str, trigger: str, kind: str, why: str, f: Mapping[str, Any]) -> Scored:
    """0-1 quality score for one trigger signal. `f` is triggers.Tape.features(...) at the signal bar."""
    trig = TRIGGER_WIN.get(trigger, 0.5)
    if trigger == "level":
        trig *= LEVEL_FACTOR.get(level_kind(why) or "", 0.85)
    fo = f.get("from_open")
    if fo is None or fo == 0:
        ctx = 0.5
    else:
        against = (kind == "call") == (fo < 0)
        ctx = AGAINST_DAY_WIN if against else WITH_DAY_WIN
    parts = dict(trigger=trig, time=tod_win(f["t"]), symbol=symbol_win(sym), context=ctx)
    s = sum(WEIGHTS[k] * v for k, v in parts.items())
    return Scored(round(s, 4), {k: round(v, 3) for k, v in parts.items()})
