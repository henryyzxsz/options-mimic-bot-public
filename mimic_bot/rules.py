import tomllib
from pathlib import Path
from dataclasses import dataclass, field

import os

CFG_PATH = Path(os.environ.get("MIMIC_BOT_CONFIG") or Path(__file__).resolve().parent.parent / "config" / "example_profile.toml")


class ConfigError(ValueError):
    pass


def validate_stop(v) -> float:
    """stop_loss_pct is required and has no built-in default: set your own risk settings."""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not (-1.0 < float(v) < 0.0):
        raise ConfigError("stop_loss_pct must be set in your config as a negative fraction between -1 and 0 "
                          "(set your own risk settings)")
    return float(v)


def load_config(path: Path | str = CFG_PATH) -> dict:
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    validate_stop(cfg.get("exit", {}).get("stop_loss_pct"))
    for name, prof in cfg.get("profiles", {}).items():
        if "stop_loss_pct" in prof:
            validate_stop(prof["stop_loss_pct"])
    return cfg

@dataclass
class Profile:
    name: str
    max_trades_per_session: int
    reentry_cooldown_after_loss_min: int
    size_mult_after_loss: float
    allow_opening_30min: bool
    allow_last_hour: bool
    allow_lottery_premium: bool
    honor_stop: bool
    allow_earnings_hold: bool
    max_risk_pct_of_account: float | None = None
    max_trades_per_symbol_per_day: dict = field(default_factory=dict)
    no_size_up_after_loss: bool = False
    watchlist: list | None = None
    max_open_per_symbol: int | None = None
    entry_start_et: str | None = None      # "HH:MM"; when set, replaces the opening-30-min / last-hour rules
    entry_end_et: str | None = None
    signal_cooldown_min: int | None = None
    min_score: float | None = None          # setup-quality floor (mimic_bot/ranking.py); None = take every signal
    min_gap_between_entries_min: int | None = None   # minutes between entries across the watchlist
    stop_loss_pct: float | None = None        # optional per-profile override of the required [exit].stop_loss_pct
    min_entries_per_hour: int | None = None    # hourly catch-up: waive the score floor late in an empty hour
    hourly_catchup_after_min: int = 30
    hourly_catchup_until_et: str | None = None      # "HH:MM" ET; no forced hourly entry at or after this (None = all day)
    hourly_catchup_min_score: float | None = None   # catch-up still needs at least this score (None = no floor)
    hourly_catchup_size_mult: float = 1.0           # catch-up entries are sized at this share of normal size
    hourly_catchup_after_loss: bool = True          # False: no catch-up while today's last closed trade is a loss
    momentum_needs_spy_above_200d: bool = False   # scoreboard idea: momentum entries only when SPY is above its 200-day
    portfolio_fraction: float | None = None   # active paper: premium outlay as fraction of equity
    # --- exits (None = use [exit] in the config) ---
    exit_plan: str = "target"                  # "target" (+X% / time stop) or "ladder" (trim ladder + breakeven + trail)
    profit_target_pct: float | None = None     # "target" plan only
    time_stop_min: int | None = None           # 0 = no time stop (FLAT_BY still applies)
    trim_ladder_gains: list | None = None      # e.g. [0.25, 0.60, 1.00, ...]; gain over entry for each rung
    trim_fractions: list | None = None         # share of the ORIGINAL qty sold at the first rungs; the rest is split evenly
    trail_arm_pct: float | None = None         # trailing stop arms once the peak bid is this far above entry
    trail_pct: float | None = None             # trailing stop sits this far below the peak bid
    daily_loss_limit_pct: float | None = None  # block new entries once today's realized loss reaches this share of equity

def load_profile(cfg, name="disciplined") -> Profile:
    p = dict(cfg["profiles"][name])
    p.pop("take_prob", None)
    return Profile(name=name, **p)
