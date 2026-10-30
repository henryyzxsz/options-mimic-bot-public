"""Side lens: label each signal with a stock-sleeve style (momentum vs balanced) and SPY 200-day regime. Annotates only — never vetoes guardrails.

The scoreboard compares Momentum vs Balanced stock sleeves with a 200-day SPY cash rule. This module maps
options-bot triggers to the closest scoreboard sleeve and prints context for logs and suggest tickets.
It is not an entry rule and does not change sizing, triggers, or guardrails.
"""
from __future__ import annotations

INDEX_SYMBOLS = frozenset({"SPY", "QQQ", "IWM", "XSP"})

# mimic_bot/triggers.py names + chase-style labels
MOMENTUM_TRIGGERS = frozenset({"momentum", "open_drive"})
BALANCED_TRIGGERS = frozenset({"turn", "level"})

RED_FLAG_ITEMS = (
    "new share issuance",
    "going-concern doubt",
    "fraud or accounting problems",
    "delisting notice",
    "binary event (trial result)",
    "pending buyout",
    "top executive leaving",
    "cut forecast",
    "trading halt",
)

REGIME_UNKNOWN_NOTE = (
    "200-day cash rule needs daily SPY history the bot does not store"
)


def sleeve_for(*, trigger: str | None = None, thesis: str | None = None) -> str:
    """Map a trigger name or fade/chase thesis to scoreboard sleeve."""
    t = (trigger or "").strip().lower()
    if t in MOMENTUM_TRIGGERS:
        return "momentum"
    if t in BALANCED_TRIGGERS:
        return "balanced"
    th = (thesis or "").lower()
    if "fade" in th or ("turn" in th and "(turn)" in th):
        return "balanced"
    if any(x in th for x in ("chase", "breakout", "gap-and-go", "opening-gap", "gap ")):
        return "momentum"
    if t:
        return "unlabeled"
    return "unlabeled"


def regime_from_spy_closes(closes: list[float] | None) -> str:
    if not closes or len(closes) < 200:
        return "unknown"
    window = closes[-200:]
    avg200 = sum(window) / 200.0
    last = closes[-1]
    return "cash" if last < avg200 else "invested"


def sample_note_for(session_count: int | None) -> str | None:
    if session_count is None:
        return None
    if session_count < 20:
        return (
            f"scoreboard paper at {session_count} sessions (early; fair test is 126 sessions)"
        )
    return None


def red_flags_for(symbol: str) -> str:
    sym = (symbol or "").upper()
    if sym in INDEX_SYMBOLS:
        return "Index symbol: company red flags do not apply."
    items = "; ".join(f"{i + 1}) {item}" for i, item in enumerate(RED_FLAG_ITEMS))
    return f"Red-flag checklist (manual review only, does not block): {items}"


def format_line(
    *,
    symbol: str,
    sleeve: str,
    regime: str,
    sample_note: str | None = None,
    red_flags: str | None = None,
    trigger: str | None = None,
) -> str:
    bits = [f"scoreboard lens {symbol}: sleeve={sleeve}", f"SPY regime={regime}"]
    if trigger:
        bits.append(f"trigger={trigger}")
    if regime == "unknown":
        bits.append(REGIME_UNKNOWN_NOTE)
    if sample_note:
        bits.append(sample_note)
    if red_flags and symbol.upper() not in INDEX_SYMBOLS:
        bits.append(red_flags)
    elif red_flags and symbol.upper() in INDEX_SYMBOLS:
        bits.append(red_flags)
    return " | ".join(bits)


def annotate(
    *,
    symbol: str,
    trigger: str | None = None,
    thesis: str | None = None,
    spy_daily_closes: list[float] | None = None,
    session_count: int | None = None,
) -> dict:
    """Build scoreboard context for a proposal or trigger signal."""
    sleeve = sleeve_for(trigger=trigger, thesis=thesis)
    regime = regime_from_spy_closes(spy_daily_closes)
    note = sample_note_for(session_count)
    flags = red_flags_for(symbol)
    line = format_line(
        symbol=symbol,
        sleeve=sleeve,
        regime=regime,
        sample_note=note,
        red_flags=flags,
        trigger=trigger,
    )
    return dict(
        sleeve=sleeve,
        regime=regime,
        sample_note=note,
        red_flags=flags,
        line=line,
    )
