"""Pre-trade checks (behavioral guardrails: entry windows, caps, cooldowns, premium floor, risk cap).
Returns a list of reasons to SKIP; empty list = allowed."""
from __future__ import annotations

from datetime import time, timedelta
from typing import Any

def check(proposal: dict, state: Any, profile: Any, cfg: dict) -> list[str]:
    why = []
    t = proposal["time"]
    start = getattr(profile, "entry_start_et", None); end = getattr(profile, "entry_end_et", None)
    if start and t.time() < time.fromisoformat(start):
        why.append(f"no entries before {start} ET")
    elif not start and not profile.allow_opening_30min and t.time() < time(10, 0):
        why.append("no entries 09:30-10:00 ET")
    if end and t.time() >= time.fromisoformat(end):
        why.append(f"no new entries after {end} ET")
    elif not end and not profile.allow_last_hour and t.time() >= time(15, 0):
        why.append("no entries after 15:00 ET")
    if len(state.today_trades(t.date())) >= profile.max_trades_per_session:
        why.append(f"daily cap {profile.max_trades_per_session} entries reached")
    open_pos = getattr(state, "open", None)
    cap_open = getattr(profile, "max_open_per_symbol", None)
    if open_pos is not None and cap_open and sum(1 for x in open_pos if x["symbol"] == proposal["symbol"]) >= cap_open:
        why.append(f"already {cap_open} open {proposal['symbol']} position(s)")
    cap = profile.max_trades_per_symbol_per_day.get(proposal["symbol"])
    if cap is not None and sum(1 for x in state.today_trades(t.date()) if x["symbol"] == proposal["symbol"]) >= cap:
        why.append(f"max {cap} {proposal['symbol']} trade(s)/day")
    last_loss = state.last_loss_exit()
    if last_loss and profile.reentry_cooldown_after_loss_min and t - last_loss < timedelta(minutes=profile.reentry_cooldown_after_loss_min):
        why.append(f"{profile.reentry_cooldown_after_loss_min}-min cooldown after a loss (revenge-trade guard)")
    if not profile.allow_lottery_premium and proposal["premium"] < cfg["contract"]["premium_min"]:
        why.append(f"premium ${proposal['premium']:.2f} < ${cfg['contract']['premium_min']:.2f}")
    if profile.max_risk_pct_of_account:
        risk = proposal["premium"] * 100 * proposal["contracts"] * abs(cfg["exit"]["stop_loss_pct"])
        if risk > profile.max_risk_pct_of_account * state.equity:
            why.append(f"planned risk ${risk:.0f} > {profile.max_risk_pct_of_account:.0%} of ${state.equity:.0f}")
    if proposal.get("earnings_before_expiry") and not profile.allow_earnings_hold:
        why.append("earnings before expiry")
    return why
