"""Paper-trading loop on Alpaca's PAPER environment (`python run.py --paper`).

Every 30 seconds during market hours it:
  1. reconciles with the paper account (on the first tick, and again whenever a position shows up later):
     adopts option positions it isn't tracking, closes out local positions the broker no longer has,
     and cancels stale bot buy orders, so a relaunch or a late fill never double-enters or goes unstopped,
  2. manages exits: the profile stop (resting stop order, or a soft stop if Alpaca rejected it) and flat by
     15:25 ET, including any untracked option position on the watchlist. `disciplined` also uses the +20%
     target and 15-min time stop; `active` uses the trim ladder instead (sell part of the position at each
     rung of `trim_ladder_gains`), moves the stop to entry after the first trim, and arms a
     trailing stop (`trail_pct` below the peak bid) at `trail_arm_pct`. Each trim cancels the resting stop and re-rests it for
     the qty that is left. A daily loss limit blocks new entries once today's realized loss is too big,
  3. pulls today's 5-min bars for the watchlist (only bars that have closed) and runs the trigger model
     (turn / level; the news trigger is backtest-only),
  4. scores each signal with mimic_bot/ranking.py and tries the best one first; the `active` profile skips
     anything under `min_score` and keeps `min_gap_between_entries_min` between entries across the watchlist,
  5. picks a 0DTE contract near the configured OTM target (premium between the config min and max),
  6. runs every guardrail in mimic_bot/guardrails.py, refuses if the broker already has exposure on the symbol,
  7. sends a paper limit buy with a deterministic client_order_id, then a resting stop once it fills.
Every event is appended to logs/paper_YYYY-MM-DD.jsonl; state lives in logs/paper_state.json (atomic writes).

`python run.py --paper` uses the `active` profile from the config (default config/example_profile.toml,
override with MIMIC_BOT_CONFIG).
PaperTrader() on its own still defaults to `disciplined`. If the paper account has no options approval, the loop
falls back to signal-only mode: it logs what it would have done and sends nothing.
"""
from __future__ import annotations

import json, math, os, sys, tempfile, time as _time
from datetime import date, datetime, timedelta, timezone, time as dtime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import ranking as R, triggers as T
from .broker_alpaca_paper import AlpacaAPIError, _looks_like_occ, occ_kind, occ_root
from .guardrails import check
from .rules import load_config, load_profile
from . import scoreboard_lens as SL

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
WATCHLIST = ["SPY", "QQQ", "IWM"]          # index ETFs only: no single-stock earnings risk
ALIASES = {"SPX": "XSP", "SPXW": "XSP"}     # full-size SPX premiums are large; XSP is 1/10 SPX
PROXY_BARS = {"XSP": "SPY"}                 # Alpaca has no index bars; XSP signals use SPY's tape (same index)
FLAT_BY = dtime(15, 25)                     # Alpaca rejects 0DTE ETF orders after 15:30 and liquidates from 15:45
FILL_WAIT_S = 60
CID_PREFIX = "omb-"                           # client_order_id prefix: marks orders this bot placed
# Terminal order statuses. "done_for_day" is dead for the session; leaving it on the position
# used to wedge the exit (the bot kept waiting, and the 15:25 flatten skipped anything it was tracking).
DEAD_ORDER = frozenset({"canceled", "cancelled", "expired", "rejected", "done_for_day", "suspended", "replaced"})


def filled_qty(order: dict | None) -> int:
    try:
        return int(float((order or {}).get("filled_qty") or 0))
    except (TypeError, ValueError):
        return 0


def order_avg(order: dict | None) -> float | None:
    raw = (order or {}).get("filled_avg_price")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(NY)


class Log:
    def __init__(self, log_dir: Path | str = LOG_DIR, clock: Callable[[], datetime] = now_et):
        self.dir = Path(log_dir); self.dir.mkdir(parents=True, exist_ok=True); self.clock = clock
    def __call__(self, kind: str, /, **kw: Any) -> dict:
        t = self.clock()
        rec = dict(ts=t.isoformat(), event=kind, **kw)
        try:
            with open(self.dir / f"paper_{t:%Y-%m-%d}.jsonl", "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError as e:                        # a full disk must never kill the trading loop
            print(f"log write failed: {e}", file=sys.stderr)
        print(f"[{t:%H:%M:%S} ET] {kind}: " + ", ".join(f"{k}={v}" for k, v in kw.items()), flush=True)
        return rec


class PaperState:
    """Local record of paper trades, shaped like backtest.PaperBroker so guardrails.check() works unchanged.
    Saved atomically (temp file + rename) so a crash mid-write can't corrupt it; a corrupt file is moved aside."""
    def __init__(self, equity: float, path: Path | str = LOG_DIR / "paper_state.json"):
        self.path = Path(path); self.equity = equity; self.trades: list[dict] = []; self.open: list[dict] = []
        self.last_fire: dict[str, str] = {}; self.load_error: str | None = None
        if self.path.exists():
            try:
                s = json.loads(self.path.read_text())
                self.equity = s["equity"]
                self.trades = [self._load(t) for t in s["trades"]]; self.open = [self._load(t) for t in s["open"]]
                self.last_fire = dict(s.get("last_fire") or {})
            except Exception as e:
                bad = self.path.with_suffix(f".corrupt-{int(_time.time())}.json")
                try: self.path.rename(bad)
                except OSError: pass
                self.load_error = f"could not read {self.path.name} ({e}); moved to {bad.name}, starting clean"
                self.trades, self.open, self.last_fire = [], [], {}
    @staticmethod
    def _load(t: dict) -> dict:
        return {**t, **{k: datetime.fromisoformat(t[k]) for k in ("time", "exit_time") if t.get(k)}}
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(dict(equity=self.equity, trades=self.trades, open=self.open, last_fire=self.last_fire), default=str, indent=1)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh: fh.write(data)
            os.replace(tmp, self.path)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise
    def today_trades(self, d: date) -> list[dict]:
        return [t for t in self.trades + self.open if t["time"].date() == d]
    def last_loss_exit(self) -> datetime | None:
        return max((t["exit_time"] for t in self.trades if t["pnl"] < 0), default=None)
    def last_closed(self) -> dict | None:
        return max(self.trades, key=lambda t: t["exit_time"], default=None)

    @staticmethod
    def _qty(t: dict) -> int:
        """Contracts the trade was opened with (ladder trims shrink `contracts` as they sell)."""
        return int(t.get("orig_contracts") or t.get("contracts") or 0)

    def loss_streak(self, d: date) -> list[dict]:
        """Today's closed trades since the last win (newest last). Empty if today's last closed trade won,
        or nothing closed today. Built only from persisted trades, so a restart can't reset it."""
        today = sorted((t for t in self.trades if t.get("exit_time") and t["exit_time"].astimezone(NY).date() == d),
                       key=lambda t: t["exit_time"])
        out: list[dict] = []
        for t in reversed(today):
            if float(t.get("pnl") or 0) >= 0: break
            out.append(t)
        return list(reversed(out))

    def size_cap_after_loss(self, d: date) -> int | None:
        """No size-up after a loss: while today's losing streak lasts (until a win or the next day), the
        next entry can't be bigger than the smallest losing trade in the streak. None = no cap."""
        streak = self.loss_streak(d)
        return min(self._qty(t) for t in streak) if streak else None

    def rebuild_today_from_log(self, log_dir: Path | str, d: date) -> int:
        """After a corrupt state file: recover today's closed trades from logs/paper_YYYY-MM-DD.jsonl so the
        guardrails (loss streak, cooldown, daily loss, entry caps) don't start from zero. Returns trades added."""
        f = Path(log_dir) / f"paper_{d:%Y-%m-%d}.jsonl"
        if not f.exists(): return 0
        qty: dict[str, int] = {}; n = 0
        for line in f.read_text().splitlines():
            try: e = json.loads(line)
            except ValueError: continue
            if e.get("event") == "fill" and e.get("occ") and e["occ"] not in qty:
                qty[e["occ"]] = int(float(e.get("qty") or 1))
            elif e.get("event") == "exit" and e.get("occ"):
                try:
                    et = datetime.fromisoformat(e["ts"])
                    t0 = datetime.fromisoformat(str(e.get("entry_time"))) if e.get("entry_time") else et
                except ValueError:
                    continue
                self.trades.append(dict(time=t0, exit_time=et, symbol=e.get("symbol"), occ=e["occ"],
                                        pnl=float(e.get("pnl") or 0), contracts=qty.get(e["occ"], 1),
                                        exit_reason=e.get("reason"), rebuilt_from_log=True))
                n += 1
        return n


def to_tape_bars(alpaca_bars: list[dict], now: datetime) -> list[list]:
    """Alpaca bars -> the [start, o, h, l, c, v] rows Tape expects, keeping only closed regular-session bars.
    triggers.Tape converts with a fixed UTC-4 offset (EDT). So that live
    times are right after DST ends, each bar is stamped as its New York wall-clock time + 4h, tagged UTC."""
    out = []
    for b in alpaca_bars:
        start = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        if start + timedelta(minutes=5) > now: continue
        wall = start.astimezone(NY)
        if not (dtime(9, 30) <= wall.time() < dtime(16, 0)): continue
        pseudo = wall.replace(tzinfo=timezone.utc) + timedelta(hours=4)
        out.append([pseudo, b["o"], b["h"], b["l"], b["c"], b["v"]])
    return out


def occ_strike(c: dict) -> float: return float(c["strike_price"])


def pick_contract(broker: Any, sym: str, kind: str, px: float, cfg: dict, today: date) -> dict | None:
    """0DTE contract nearest the configured OTM strike target whose ask is inside [premium_min, premium_max]."""
    otm = cfg["contract"]["otm_pct_target"] / 100
    target = px * (1 + otm) if kind == "call" else px * (1 - otm)
    cs = [c for c in broker.option_contracts(sym, today.isoformat(), kind)
          if (occ_strike(c) > px if kind == "call" else occ_strike(c) < px)]
    cs = sorted(cs, key=lambda c: abs(occ_strike(c) - target))[:6]
    if not cs: return None
    q = broker.option_quotes([c["symbol"] for c in cs])
    for c in cs:
        qq = q.get(c["symbol"]) or {}
        bid, ask = float(qq.get("bp") or 0), float(qq.get("ap") or 0)
        if ask <= 0 or bid <= 0: continue
        if cfg["contract"]["premium_min"] <= ask <= cfg["contract"]["premium_max"]:
            return dict(occ=c["symbol"], strike=occ_strike(c), expiry=c["expiration_date"], bid=bid, ask=ask)
    return None


def parity_spot(broker: Any, sym: str, guess: float, today: date, n: int = 6) -> float | None:
    """Index spot from put-call parity on today's chain: S ~= K + C_mid - P_mid at the strike where C ~= P.
    Used for XSP, whose underlying has no bars on Alpaca."""
    calls = {occ_strike(c): c["symbol"] for c in broker.option_contracts(sym, today.isoformat(), "call")}
    puts = {occ_strike(c): c["symbol"] for c in broker.option_contracts(sym, today.isoformat(), "put")}
    ks = sorted(set(calls) & set(puts), key=lambda k: abs(k - guess))[:n]
    if not ks: return None
    q = broker.option_quotes([calls[k] for k in ks] + [puts[k] for k in ks])
    mid = lambda o: (float(o.get("bp") or 0) + float(o.get("ap") or 0)) / 2 if o and float(o.get("ap") or 0) > 0 else None
    best = None
    for k in ks:
        c, p = mid(q.get(calls[k])), mid(q.get(puts[k]))
        if c is None or p is None: continue
        if best is None or abs(c - p) < best[0]: best = (abs(c - p), k + c - p)
    return round(best[1], 2) if best else None


def leg_cid(rec: dict, suffix: str, now: datetime) -> str:
    """client_order_id for a position's stop/exit legs, tied to that entry so a later re-entry into the same
    contract on the same day can't collide with (and be deduped into) an earlier trade's orders."""
    base = str(rec.get("client_order_id") or "")
    if base.endswith("-b"): return f"{base[:-2]}-{suffix}"
    t = rec.get("time"); t = t if isinstance(t, datetime) else now
    return f"{CID_PREFIX}{t:%y%m%d%H%M}-{rec['occ']}-{suffix}"


def sizing_equity(broker, state):
    """Paper account equity from Alpaca when available; else local state."""
    if broker is not None:
        try:
            return float(broker.account()["equity"])
        except Exception:
            pass
    return state.equity


def size_for(state, profile, premium=None, broker=None, base=1, now=None, mult=1.0):
    """Contract count. `active` uses portfolio_fraction of equity on premium outlay; `disciplined` stays at base (1).
    No size-up after a loss: while today's losing streak lasts (until a win or the next ET day) qty is capped at the
    smallest losing trade's size in that streak, for % sizing too. `mult` (e.g. a smaller share for catch-up entries) scales
    the % size before the cap. Without `now` the legacy rule applies (last closed trade of any day)."""
    if profile.portfolio_fraction is not None and premium is not None:
        eq = sizing_equity(broker, state)
        qty = int(profile.portfolio_fraction * eq / (premium * 100)) if premium > 0 else 0
        if qty > 0 and mult != 1.0:
            qty = max(1, int(qty * mult))
    else:
        qty = base
    if profile.no_size_up_after_loss:
        if now is not None:
            cap = state.size_cap_after_loss(now.astimezone(NY).date())
        else:
            last = state.last_closed()
            cap = PaperState._qty(last) if last and last["pnl"] < 0 else None
        if cap is not None:
            qty = min(qty, max(1, cap)) if profile.portfolio_fraction is not None else max(1, min(qty, cap))
    return qty


class PaperTrader:
    def __init__(self, broker: Any, cfg: dict | None = None, log: Log | None = None, state: PaperState | None = None,
                 watchlist: list[str] | None = None, clock: Callable[[], datetime] = now_et,
                 sleep: Callable[[float], None] = _time.sleep, options_ok: bool | None = None, profile: str = "disciplined"):
        self.b = broker; self.cfg = cfg or load_config(); self.prof = load_profile(self.cfg, profile)
        self.mode = profile if profile in T.MODES else "disciplined"
        self.log = log or Log(clock=clock); self.clock = clock; self.sleep = sleep
        wl = watchlist or self.prof.watchlist or WATCHLIST
        self.watch = list(dict.fromkeys(ALIASES.get(x.upper(), x.upper()) for x in wl))
        self.sig_cooldown = self.prof.signal_cooldown_min or T.DEFAULTS["cooldown_min"]
        self.min_score = self.prof.min_score
        self.min_gap = self.prof.min_gap_between_entries_min or 0
        from .rules import validate_stop   # required, no built-in default: set your own risk settings
        self.stop_pct = validate_stop(self.prof.stop_loss_pct if self.prof.stop_loss_pct is not None
                                      else self.cfg.get("exit", {}).get("stop_loss_pct"))
        self._spy_regime = None   # (date, "invested"|"cash"|"unknown", closes)
        self.state = state or PaperState(self.cfg["account"]["paper_starting_equity"])
        if self.state.load_error:
            self.log("state_reset", reason=self.state.load_error)
            try:
                n = self.state.rebuild_today_from_log(self.log.dir, self.clock().date()) if hasattr(self.log, "dir") else 0
            except Exception as e:
                n = 0; self.log("error", where="state rebuild", error=str(e))
            if n:
                self.state.save()
                self.log("state_rebuilt", trades=n, note="today's closed trades recovered from the event log")
        self.reconciled = False
        self.snapshot: dict | None = None
        self.last_heartbeat: datetime | None = None
        self.options_ok = options_ok if options_ok is not None else broker.options_level() >= 2
        if not self.options_ok:
            self.log("fallback_signal_only", reason="paper account has no options approval (level < 2); logging signals, sending nothing")

    # ---------- per-profile exit settings (properties so tests that skip __init__ still work) ----------
    @property
    def profit_target(self) -> float:
        v = getattr(self.prof, "profit_target_pct", None)
        return v if v is not None else self.cfg["exit"]["profit_target_pct"]

    @property
    def time_stop(self) -> int:
        v = getattr(self.prof, "time_stop_min", None)
        return int(v if v is not None else self.cfg["exit"]["time_stop_min"])

    @property
    def ladder(self) -> list[tuple[float, float]] | None:
        """[(gain over entry, cumulative share of the original qty sold by this rung)], or None for the target plan."""
        if getattr(self.prof, "exit_plan", "target") != "ladder" or not self.prof.trim_ladder_gains:
            return None
        gains = [float(g) for g in self.prof.trim_ladder_gains]
        head = [float(x) for x in (self.prof.trim_fractions or [0.5, 0.25])][:len(gains)]
        rest = len(gains) - len(head); left = max(0.0, 1.0 - sum(head))
        cum, c = [], 0.0
        for i in range(len(gains)):
            c += head[i] if i < len(head) else left / rest
            cum.append(min(1.0, c))
        cum[-1] = 1.0
        return list(zip(gains, cum))

    @staticmethod
    def rung_target(orig: int, cum: float, sold: int, last: bool) -> int:
        """Cumulative contracts sold once this rung is done: rounds up, sells at least 1 more, never more than held."""
        if last: return orig
        return min(orig, max(sold + 1, math.ceil(orig * cum - 1e-9)))

    def stop_price(self, p: dict) -> float:
        """Current stop for a position. Before any trim: entry x (1 + stop_pct). Ladder plan after the first trim:
        entry (breakeven). Once the trail is armed: max(that, peak bid x (1 - trail_pct)). Only ratchets up."""
        entry = float(p["premium"])
        base = round(entry * (1 + self.stop_pct), 2)
        if self.ladder is None:
            return base
        s = entry if p.get("trimmed") else base
        if p.get("trail_armed") and p.get("peak_bid"):
            s = max(s, float(p["peak_bid"]) * (1 - float(self.prof.trail_pct or 0)))
        s = max(s, float(p.get("stop_px") or 0))
        return round(s, 2)

    def stop_label(self, p: dict) -> str:
        if self.ladder is None or not (p.get("trimmed") or p.get("trail_armed")):
            return f"auto stop {self.stop_pct:.0%}"
        entry = float(p["premium"])
        if p.get("trail_armed") and self.stop_price(p) > entry + 1e-9:
            return "trailing stop"
        return "breakeven stop" if p.get("trimmed") else f"auto stop {self.stop_pct:.0%}"

    def _update_peak(self, p: dict, bid: float) -> None:
        """Track the peak bid, arm the trail at +trail_arm_pct, and persist the ratcheted stop."""
        if self.ladder is None or not bid:
            return
        changed = False
        if bid > float(p.get("peak_bid") or 0):
            p["peak_bid"] = bid; changed = True
        arm = self.prof.trail_arm_pct
        if arm is not None and not p.get("trail_armed") and float(p.get("peak_bid") or 0) >= float(p["premium"]) * (1 + arm) - 1e-9:
            p["trail_armed"] = True; changed = True
            self.log("trail_armed", occ=p["occ"], peak=p["peak_bid"], trail=f"{self.prof.trail_pct:.0%} below peak")
        new = self.stop_price(p)
        if new != p.get("stop_px"):
            p["stop_px"] = new; changed = True
        if changed:
            self.state.save()

    # ---------- daily loss limit ----------
    def realized_today(self, now: datetime) -> float:
        """Closed trades exited today plus booked partial trims/fills on positions still open."""
        d = now.date(); tot = 0.0
        for t in self.state.trades:
            et = t.get("exit_time")
            if isinstance(et, str):
                try: et = datetime.fromisoformat(et)
                except ValueError: et = None
            if isinstance(et, datetime) and et.astimezone(NY).date() == d:
                tot += float(t.get("pnl") or 0)
        tot += sum(float(p.get("partial_pnl") or 0) for p in self.state.open)
        return round(tot, 2)

    def daily_loss_block(self, now: datetime) -> str | None:
        pct = getattr(self.prof, "daily_loss_limit_pct", None)
        if not pct:
            return None
        r = self.realized_today(now)
        if r >= 0:
            return None
        eq = sizing_equity(self.b, self.state)
        if r <= -pct * eq:
            return (f"daily loss limit: realized ${r:,.2f} today <= -{pct:.0%} of ${eq:,.0f} paper equity; "
                    f"no new entries until tomorrow")
        return None

    def sync_equity(self, now: datetime) -> None:
        """Local equity follows the paper account. [account].paper_starting_equity is only a placeholder
        until the first sync, so realized P&L can't drift the sizing equity away from the real paper balance."""
        try:
            eq = float(self.b.account()["equity"])
        except Exception as e:
            self.log("error", where="equity sync", error=str(e)); return
        if abs(eq - float(self.state.equity)) >= 0.01:
            self.log("equity_synced", old=round(float(self.state.equity), 2), new=round(eq, 2))
            self.state.equity = eq; self.state.save()

    # backwards-compatible view of the persisted per-symbol signal cooldown
    @property
    def last_fire(self) -> dict[str, datetime]:
        return {k: datetime.fromisoformat(v) for k, v in self.state.last_fire.items()}

    # ---------- broker snapshot (positions + open orders), refreshed every tick ----------
    def refresh_snapshot(self) -> bool:
        try:
            pos = self.b.option_positions(); orders = self.b.open_orders()
        except Exception as e:
            self.snapshot = None
            self.log("error", where="snapshot", error=str(e), action="no new entries this tick")
            return False
        self.snapshot = dict(positions=pos, orders=orders)
        return True

    def broker_busy(self, sym: str) -> str | None:
        """Reason the broker already has exposure on `sym` (position or working buy order), else None."""
        if self.snapshot is None:
            return "broker snapshot unavailable"
        for p in self.snapshot["positions"]:
            if occ_root(p.get("symbol", "")) == sym and float(p.get("qty") or 0) != 0:
                return f"broker already holds {p['symbol']}"
        for o in self.snapshot["orders"]:
            s = o.get("symbol", "")
            if o.get("side") == "buy" and _looks_like_occ(s) and occ_root(s) == sym:
                return f"working buy order {o.get('id')} on {s}"
        return None

    # ---------- broker sync: startup reconcile, plus adopt on later ticks ----------
    def _watch_option(self, bp: dict) -> bool:
        sym = str(bp.get("symbol") or "")
        return _looks_like_occ(sym) and occ_root(sym) in self.watch

    def _entry_time_from_fills(self, occ: str, now: datetime) -> datetime:
        """Latest filled buy time so a restart doesn't restart the 15-min time stop."""
        try:
            closed = self.b.closed_orders(occ)
        except Exception:
            return now
        best: datetime | None = None
        for o in closed:
            if o.get("symbol") not in (None, occ) or o.get("side") != "buy" or o.get("status") != "filled":
                continue
            raw = o.get("filled_at")
            if not raw:
                continue
            try:
                t = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(NY)
            except ValueError:
                continue
            if best is None or t > best:
                best = t
        return best or now

    def _should_rest_stop(self, rec: dict, now: datetime) -> bool:
        if now.time() >= FLAT_BY or float(rec.get("premium") or 0) <= 0:
            return False
        t = rec.get("time")
        if self.time_stop and isinstance(t, datetime) and (now - t).total_seconds() >= self.time_stop * 60:
            return False
        return True

    def cancel_stale_buys(self) -> bool:
        """Cancel this bot's still-open buys. Safe every tick: the loop never leaves a buy resting on purpose.
        A buy that fills anyway is picked up by adopt_untracked."""
        try:
            orders = self.b.open_orders()
        except Exception as e:
            self.log("error", where="cancel_stale_buys", error=str(e))
            return False
        for o in orders:
            if o.get("side") == "buy" and str(o.get("client_order_id", "")).startswith(CID_PREFIX):
                try:
                    self.b.cancel(o["id"])
                except Exception as e:
                    self.log("error", where="cancel_stale_buys", order_id=o.get("id"), error=str(e))
                    continue
                self.log("reconcile_cancel_stale_buy", order_id=o["id"], occ=o.get("symbol"))
        return True

    def _adopt_new(self, pos: dict, orders: list, now: datetime) -> None:
        known = {p["occ"] for p in self.state.open}
        for occ, bp in pos.items():
            if occ in known:
                continue
            try:
                qty = int(float(bp.get("qty") or 0))
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            stop = next((o for o in orders if o.get("symbol") == occ and o.get("side") == "sell" and o.get("type") == "stop"), None)
            market = next((o for o in orders if o.get("symbol") == occ and o.get("side") == "sell" and o.get("type") == "market"), None)
            entry = float(bp.get("avg_entry_price") or 0)
            rec = dict(time=self._entry_time_from_fills(occ, now), symbol=occ_root(occ), kind=occ_kind(occ),
                       strike=int(occ[-8:]) / 1000, expiry=None, premium=entry, contracts=qty,
                       earnings_before_expiry=False, thesis="adopted from the paper account", occ=occ,
                       stop_order_id=stop.get("id") if stop else None, adopted=True)
            if market:
                rec["exit_order_id"] = market.get("id")
                rec["exit_reason_pending"] = "exit already working at the broker"
            self.state.open.append(rec)
            self.state.save()
            self.log("reconcile_adopted", occ=occ, qty=qty, entry=entry, stop_order_id=rec["stop_order_id"])
            if rec.get("stop_order_id") or rec.get("exit_order_id"):
                continue
            if self._should_rest_stop(rec, now):
                self._place_stop(rec, now)

    def adopt_untracked(self, now: datetime) -> None:
        """Pick up long option positions the state file doesn't know about. Runs every tick after startup
        so a buy that fills after we thought it was cancelled still gets a stop and a time stop."""
        try:
            raw = self.b.option_positions()
            orders = self.b.open_orders()
        except Exception as e:
            self.log("error", where="adopt", error=str(e), action="retry next tick")
            return
        for bp in raw:
            sym = str(bp.get("symbol") or "")
            if self._watch_option(bp) and float(bp.get("qty") or 0) < 0:
                self.log("error", where="position", occ=sym, qty=bp.get("qty"),
                         action="short option left open; this bot only sells longs")
        pos = {bp["symbol"]: bp for bp in raw if self._watch_option(bp) and float(bp.get("qty") or 0) > 0}
        self._adopt_new(pos, orders, now)

    def reconcile(self, now: datetime) -> bool:
        """Line local state up with the paper account before trading:
        stale bot buy orders are cancelled, positions the bot doesn't know about are adopted (so they get managed
        and flattened), and local positions that no longer exist at the broker are closed out locally.
        Dropping a local position the broker doesn't have is startup-only: doing it every tick could book a
        fake exit on a transient empty positions response."""
        if not self.cancel_stale_buys():
            return False
        try:
            raw = self.b.option_positions()
            orders = self.b.open_orders()
        except Exception as e:
            self.log("error", where="reconcile", error=str(e), action="retry next tick; no entries until reconciled")
            return False
        pos = {bp["symbol"]: bp for bp in raw if self._watch_option(bp) and float(bp.get("qty") or 0) > 0}
        for p in list(self.state.open):
            if p["occ"] in pos:
                continue
            px, why = self._exit_price_after_gap(p)
            self.close(p, px, now, f"closed while the bot was down ({why})")
        self._adopt_new(pos, orders, now)
        self.log("reconciled", open=[p["occ"] for p in self.state.open], entries_today=len(self.state.today_trades(now.date())))
        return True

    def _exit_price_after_gap(self, p: dict) -> tuple[float, str]:
        if p.get("stop_order_id"):
            try:
                so = self.b.get_order(p["stop_order_id"])
                if so.get("status") == "filled": return float(so["filled_avg_price"]), "stop filled"
            except Exception: pass
        if p.get("exit_order_id"):
            try:
                eo = self.b.get_order(p["exit_order_id"])
                if eo.get("status") == "filled": return float(eo["filled_avg_price"]), "exit order filled"
            except Exception: pass
        try:   # e.g. Alpaca's own 0DTE liquidation, or a manual sell
            for o in self.b.closed_orders(p["occ"]):
                if o.get("side") == "sell" and o.get("status") == "filled" and o.get("filled_avg_price"):
                    return float(o["filled_avg_price"]), f"sell {o.get('id')} filled"
        except Exception: pass
        return 0.0, "no fill found; booked at $0 (expired or liquidated)"

    def _scoreboard(self, sym, trigger=None, thesis=None):
        """Annotation only: never changes entries, stops or size."""
        days = set()
        for t in list(self.state.trades) + list(self.state.open):
            tm = t.get("time") if isinstance(t, dict) else None
            if isinstance(tm, str):
                try: tm = datetime.fromisoformat(tm)
                except ValueError: tm = None
            if isinstance(tm, datetime): days.add(tm.date())
        days.add(self.clock().date())
        try:
            _, closes = self.spy_regime(self.clock())
            return SL.annotate(symbol=sym, trigger=trigger, thesis=thesis, session_count=len(days), spy_daily_closes=closes)
        except Exception as e:
            return {"line": f"scoreboard lens unavailable: {e}"}

    # ---------- entries ----------
    def signal(self, sym: str, now: datetime) -> dict | None:
        start = (now - timedelta(days=4)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        src = PROXY_BARS.get(sym, sym)
        bars = to_tape_bars(self.b.stock_bars(src, start), now)
        if len(bars) < 7: return None
        tape = T.Tape(bars); f = tape.features(len(bars) - 1)
        if not f or f["t"].date() != now.date(): return None
        s = T.evaluate(sym, f, T.DEFAULTS, self.mode, news=None)
        if not s: return None
        last = self.last_fire.get(sym)
        if last and now - last < timedelta(minutes=self.sig_cooldown): return None
        self.state.last_fire[sym] = now.isoformat(); self.state.save()
        px = f["px"]
        if src != sym:
            px = parity_spot(self.b, sym, px, now.date())
            if not px:
                self.log("blocked", symbol=sym, trigger=s[0], reasons=[f"no {sym} spot from put-call parity (thin chain)"]); return None
        sc = R.score_signal(sym, s[0], s[1], s[2], f)
        out = dict(trigger=s[0], kind=s[1], why=s[2], px=px, score=sc.score, score_parts=sc.parts, bar=f["t"].strftime("%H%M"))
        if src != sym: out["tape"] = src
        return out

    def _outside_entry_window(self, now: datetime) -> str | None:
        """Profile entry window. Checked before a signal is recorded so a pre-window bar doesn't burn the cooldown."""
        start, end = self.prof.entry_start_et, self.prof.entry_end_et
        if start and now.time() < dtime.fromisoformat(start):
            return f"no entries before {start} ET"
        if not start and not self.prof.allow_opening_30min and now.time() < dtime(10, 0):
            return "no entries before 10:00 ET"
        if end and now.time() >= dtime.fromisoformat(end):
            return f"no new entries after {end} ET"
        if not end and not self.prof.allow_last_hour and now.time() >= dtime(15, 0):
            return "no entries after 15:00 ET"
        return None

    def entry_blockers(self, sym: str, now: datetime) -> list[str]:
        """Cheap checks that don't need a signal: already open locally / at the broker, entry window, flat time."""
        why = []
        if any(p["symbol"] == sym for p in self.state.open): why.append(f"already open {sym}")
        busy = self.broker_busy(sym) if self.options_ok else None
        if busy: why.append(busy)
        window = self._outside_entry_window(now)
        if window: why.append(window)
        if now.time() >= FLAT_BY: why.append("too close to the 0DTE cutoff")
        return why

    def spy_regime(self, now: datetime) -> tuple[str, list[float] | None]:
        """Scoreboard 200-day SPY cash rule from Alpaca daily bars, cached per day. Any failure is 'unknown'."""
        if self._spy_regime and self._spy_regime[0] == now.date():
            return self._spy_regime[1], self._spy_regime[2]
        closes = None
        try:
            start = (now - timedelta(days=330)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            bars = self.b.stock_bars("SPY", start, timeframe="1Day")
            closes = [float(b["c"]) for b in bars if b.get("c") is not None]
        except Exception as e:
            self.log("error", where="spy 200-day", error=str(e))
        regime = SL.regime_from_spy_closes(closes)
        self._spy_regime = (now.date(), regime, closes)
        return regime, closes

    def hourly_catchup(self, now: datetime) -> bool:
        """True when the profile wants >=1 entry per hour, none happened yet this ET hour, and we're past :NN."""
        if not self.prof.min_entries_per_hour: return False
        until = getattr(self.prof, "hourly_catchup_until_et", None)
        if until and now.time() >= dtime.fromisoformat(until): return False
        # never earlier than the profile's first entry time (the entry window check also blocks it)
        if self._outside_entry_window(now): return False
        if now.minute < self.prof.hourly_catchup_after_min: return False
        n = sum(1 for t in self.state.today_trades(now.date()) if t["time"].hour == now.hour)
        return n < self.prof.min_entries_per_hour

    def gap_block(self, now: datetime) -> str | None:
        if not self.min_gap: return None
        last = max((t["time"] for t in self.state.today_trades(now.date())), default=None)
        if last and now - last < timedelta(minutes=self.min_gap):
            return f"{self.min_gap}-min gap between entries (last entry {last:%H:%M})"
        return None

    def scan(self, now: datetime) -> list[tuple[str, dict]]:
        """Signals across the watchlist this tick, best score first."""
        cands = []
        for sym in self.watch:
            try:
                if self.entry_blockers(sym, now): continue
                sig = self.signal(sym, now)
            except Exception as e:
                self.log("error", where=f"signal {sym}", error=str(e)); continue
            if sig: cands.append((sym, sig))
        cands.sort(key=lambda x: -x[1]["score"])
        return cands

    def try_entry(self, sym: str, now: datetime, sig: dict | None = None) -> None:
        if self.entry_blockers(sym, now): return
        dl = self.daily_loss_block(now)
        if dl:
            self.log("blocked", symbol=sym, reasons=[dl]); return
        sig = sig or self.signal(sym, now)
        if not sig: return
        lens = self._scoreboard(sym, trigger=sig.get("trigger"), thesis=sig.get("why"))
        self.log("signal", symbol=sym, scoreboard=lens["line"], **sig)
        if sig.get("trigger") == "momentum" and self.prof.momentum_needs_spy_above_200d:
            regime, _ = self.spy_regime(now)
            if regime != "invested":
                self.log("skipped", symbol=sym, trigger="momentum", score=sig["score"],
                         reasons=[f"scoreboard cash rule: SPY regime={regime}, momentum entries need SPY above its 200-day"]); return
        catchup = self.hourly_catchup(now)
        catchup_entry = False
        if catchup and self.min_score is not None and sig["score"] < self.min_score:
            floor = getattr(self.prof, "hourly_catchup_min_score", None)
            if floor is not None and sig["score"] < floor:
                self.log("skipped", symbol=sym, trigger=sig["trigger"], score=sig["score"],
                         reasons=[f"catch-up needs setup score >= {floor:.2f} (got {sig['score']:.2f})"]); return
            if not getattr(self.prof, "hourly_catchup_after_loss", True) and self.state.loss_streak(now.date()):
                self.log("skipped", symbol=sym, trigger=sig["trigger"], score=sig["score"],
                         reasons=["no catch-up entries after a loss until the next win"]); return
            catchup_entry = True
            self.log("hourly_catchup", symbol=sym, score=sig["score"], min_score=self.min_score,
                     note="no entry yet this hour; taking the best signal under the score floor")
        elif self.min_score is not None and sig["score"] < self.min_score:
            self.log("skipped", symbol=sym, trigger=sig["trigger"], score=sig["score"],
                     reasons=[f"setup score {sig['score']:.2f} < {self.min_score:.2f}"]); return
        gap = self.gap_block(now)
        if gap:
            self.log("skipped", symbol=sym, trigger=sig["trigger"], score=sig["score"], reasons=[gap]); return
        c = pick_contract(self.b, sym, sig["kind"], sig["px"], self.cfg, now.date())
        if not c:
            self.log("blocked", symbol=sym, scoreboard=lens["line"],
                     reasons=["no 0DTE contract with an ask inside the premium band"]); return
        mult = float(getattr(self.prof, "hourly_catchup_size_mult", 1.0) or 1.0) if catchup_entry else 1.0
        qty = size_for(self.state, self.prof, premium=c["ask"], broker=self.b, now=now, mult=mult)
        cap = self.state.size_cap_after_loss(now.date()) if self.prof.no_size_up_after_loss else None
        if cap is not None:
            self.log("size_capped_after_loss", symbol=sym, qty=qty, cap=cap)
        if qty == 0:
            eq = sizing_equity(self.b, self.state)
            frac = self.prof.portfolio_fraction
            self.log("blocked", symbol=sym, occ=c["occ"], ask=c["ask"], scoreboard=lens["line"],
                     reasons=[f"0 contracts: ${c['ask']:.2f} ask (${c['ask'] * 100:.0f}/contract) exceeds "
                              f"{frac:.0%} of ${eq:,.0f} paper equity"])
            return
        prop = dict(time=now, symbol=sym, kind=sig["kind"], strike=c["strike"], expiry=c["expiry"], premium=c["ask"],
                    contracts=qty, earnings_before_expiry=False, thesis=f"{sig['trigger']}: {sig['why']}", score=sig["score"])
        if catchup_entry: prop["catchup"] = True
        why = check(prop, self.state, self.prof, self.cfg)
        if now.time() >= FLAT_BY: why.append("too close to the 0DTE cutoff")
        if why:
            self.log("blocked", symbol=sym, occ=c["occ"], ask=c["ask"], scoreboard=lens["line"], reasons=why); return
        if not self.options_ok:
            self.log("would_buy", symbol=sym, occ=c["occ"], qty=qty, limit=c["ask"], thesis=prop["thesis"],
                     scoreboard=lens["line"]); return
        cid = f"{CID_PREFIX}{now:%y%m%d}-{sym}-{sig['bar']}-b"
        o = self.b.buy_to_open(c["occ"], qty, c["ask"], client_order_id=cid)
        self.log("order_submitted", side="buy", occ=c["occ"], qty=qty, limit=c["ask"], order_id=o.get("id"),
                 client_order_id=cid, thesis=prop["thesis"], score=sig["score"])
        if o.get("status") in DEAD_ORDER and filled_qty(o) == 0:
            self.log("order_rejected", order_id=o.get("id"), status=o.get("status"), client_order_id=cid)
            return
        fill = self.wait_fill(o["id"])
        if not fill:
            self.b.cancel(o["id"])
            fill = self._filled_after_cancel(o["id"])
            if not fill:
                self.log("order_cancelled", order_id=o["id"], reason=f"not filled in {FILL_WAIT_S}s"); return
        px = float(fill["filled_avg_price"]); qty = int(float(fill.get("filled_qty") or qty)) or qty
        rec = dict(prop, occ=c["occ"], premium=px, contracts=qty, time=now, stop_order_id=None, client_order_id=cid)
        if self.ladder is not None:
            rec.update(orig_contracts=qty, rungs_hit=0, trimmed=False, peak_bid=px, trail_armed=False)
        self.state.open.append(rec); self.state.save()          # record the position before anything else can fail
        self.log("fill", occ=c["occ"], price=px, qty=qty)
        self._place_stop(rec, now)

    def _place_stop(self, rec: dict, now: datetime) -> None:
        """Rest the current stop (profile stop, breakeven, or trail) for the qty still held. A rejected or cancelled id
        is not reused: Alpaca keeps client_order_id forever, so every attempt (and every move) gets a new suffix."""
        n = int(rec.get("stop_attempts") or 0) + 1
        suffix = "s" if n == 1 else f"s{n}"
        stop_px = self.stop_price(rec)
        label = self.stop_label(rec)
        if self.ladder is not None:
            rec["stop_px"] = stop_px
        try:
            so = self.b.place_stop(rec["occ"], int(rec["contracts"]), stop_px, client_order_id=leg_cid(rec, suffix, now))
        except Exception as e:   # paper sometimes rejects stops on contracts with no trade yet
            rec["stop_attempts"] = n
            rec["stop_order_id"] = None; rec["stop_px_resting"] = None
            self.state.save()
            self.log("stop_failed", occ=rec["occ"], stop=stop_px, error=str(e), fallback=f"soft {label} checked every tick")
            return
        rec["stop_attempts"] = n
        if self._consume_fill(rec, so, now, label):
            return
        if so.get("status") in DEAD_ORDER and filled_qty(so) == 0:
            rec["stop_order_id"] = None; rec["stop_px_resting"] = None
            self.state.save()
            self.log("stop_failed", occ=rec["occ"], stop=stop_px, error=str(so.get("status")),
                     fallback=f"soft {label} checked every tick")
            return
        rec["stop_order_id"] = so.get("id"); rec["stop_px_resting"] = stop_px
        self.state.save()
        self.log("stop_placed", occ=rec["occ"], stop=stop_px, qty=int(rec["contracts"]), order_id=so.get("id"), kind=label)

    def _filled_after_cancel(self, order_id: str) -> dict | None:
        for _ in range(3):
            try: o = self._get_order(order_id)
            except Exception: return None
            if o.get("status") == "filled" or (o.get("status") in DEAD_ORDER and filled_qty(o) > 0 and order_avg(o) is not None):
                return o
            if o.get("status") in DEAD_ORDER: return None
            self.sleep(1)
        return None

    def wait_fill(self, order_id: str, wait_s: int = FILL_WAIT_S) -> dict | None:
        for _ in range(max(1, wait_s // 5)):
            try: o = self._get_order(order_id)
            except Exception as e:
                self.log("error", where="wait_fill", order_id=order_id, error=str(e)); self.sleep(5); continue
            if o.get("status") == "filled": return o
            if o.get("status") in DEAD_ORDER: return None
            self.sleep(5)
        return None

    # ---------- exits ----------
    def close(self, p: dict, px: float, now: datetime, reason: str) -> None:
        pnl = round((float(px) - p["premium"]) * 100 * int(p["contracts"]) + float(p.get("partial_pnl") or 0), 2)
        p.update(exit_time=now, exit_premium=px, exit_reason=reason, pnl=pnl)
        self.state.equity += p["pnl"]
        if p in self.state.open: self.state.open.remove(p)
        self.state.trades.append(p); self.state.save()
        self.log("exit", symbol=p["symbol"], occ=p["occ"], reason=reason, entry=p["premium"], exit=px, pnl=p["pnl"],
                 entry_time=p["time"], thesis=p.get("thesis"))

    def exit_reason(self, p: dict, now: datetime, bid: float) -> str | None:
        held = (now - p["time"]).total_seconds() / 60
        if now.time() >= FLAT_BY: return "flat before the 0DTE cutoff"
        if self.ladder is None and bid and bid >= p["premium"] * (1 + self.profit_target):
            return f"target +{self.profit_target:.0%}"
        stop = self.stop_price(p)
        # soft stop: no resting stop, or (ladder) the resting one lags the ratcheted stop
        lagging = self.ladder is not None and float(p.get("stop_px_resting") or 0) < stop - 1e-9
        if (not p.get("stop_order_id") or lagging) and bid and bid <= stop:
            return "soft " + self.stop_label(p).replace("auto stop", "stop")
        if self.time_stop and held >= self.time_stop: return f"time stop {self.time_stop} min"
        return None

    def _get_order(self, order_id: str) -> dict:
        """404 means the id is gone. Treat it as cancelled so a stale id can't wedge the position
        (manage would otherwise throw every tick, and the 15:25 flatten skips anything we're tracking)."""
        try:
            return self.b.get_order(order_id) or {}
        except AlpacaAPIError as e:
            if e.status == 404:
                return {"id": order_id, "status": "canceled"}
            raise

    def _wait_done(self, order_id: str, tries: int = 5) -> dict:
        o: dict = {}
        for _ in range(tries):
            o = self._get_order(order_id)
            if o.get("status") == "filled" or o.get("status") in DEAD_ORDER:
                return o
            self.sleep(1)
        return o

    def _book_partial(self, p: dict, qty: int, px: float) -> None:
        qty = min(int(qty), int(p["contracts"]))
        if qty <= 0:
            return
        p["partial_pnl"] = round(float(p.get("partial_pnl") or 0) + (float(px) - p["premium"]) * 100 * qty, 2)
        p["contracts"] = int(p["contracts"]) - qty

    def _consume_fill(self, p: dict, order: dict, now: datetime, reason: str) -> bool:
        """Apply a fill. True means the position is closed and the caller must not sell again.
        A cancelled stop that already filled the whole qty is a fill, not a chance to sell the contracts twice.
        A partial fill shrinks the position; the caller sells only what is left."""
        if p in self.state.trades:
            return True   # already booked; closing again would add the pnl twice
        avg = order_avg(order)
        fq = filled_qty(order)
        contracts = int(p["contracts"])
        if avg is None or contracts <= 0:
            return False
        # A cancelled partial is terminal. Seeing it again must not look like a full fill of whatever qty is left.
        mark = (str(order.get("id") or ""), fq, avg)
        if mark in p.get("booked_fills", []):
            return False
        if order.get("status") == "filled" or (order.get("status") in DEAD_ORDER and fq >= contracts > 0):
            self.close(p, avg, now, reason)
            return True
        if order.get("status") in DEAD_ORDER and 0 < fq < contracts:
            p.setdefault("booked_fills", []).append(mark)
            self._book_partial(p, fq, avg)
        return False

    def _broker_long_qty(self, occ: str) -> int | None:
        try:
            for bp in self.b.option_positions():
                if bp.get("symbol") == occ:
                    return max(0, int(float(bp.get("qty") or 0)))
            return 0
        except Exception:
            return None

    def _release_stop(self, p: dict, now: datetime) -> str:
        """Cancel every resting stop on this contract and wait until each is terminal.
        Returns 'filled' (position closed), 'released' (safe to sell), or 'pending' (do not sell yet).
        Also cancels a stop we lost the id for, so a market sell can't run into it and get rejected or go short."""
        ids: list[str] = []
        if p.get("stop_order_id"):
            ids.append(p["stop_order_id"])
        try:
            extra = self.b.open_orders()
        except Exception as e:
            self.log("error", where="cancel_stop", occ=p.get("occ"), error=str(e))
            if not ids:
                self.log("exit_pending", occ=p.get("occ"), note="could not list orders to confirm the stop is gone")
                return "pending"
            extra = []
        for o in extra:
            if o.get("symbol") == p.get("occ") and o.get("side") == "sell" and o.get("type") == "stop" and o.get("id") not in ids:
                ids.append(o["id"])
        if not ids:
            p["stop_order_id"] = None
            return "released"
        pending = False
        for oid in ids:
            try:
                self.b.cancel(oid)
            except Exception as e:
                self.log("error", where="cancel_stop", occ=p.get("occ"), order_id=oid, error=str(e))
            so = self._wait_done(oid)
            if self._consume_fill(p, so, now, self.stop_label(p)):
                return "filled"
            if so.get("status") not in DEAD_ORDER:
                pending = True
        if pending or p not in self.state.open:
            if p in self.state.open:
                self.log("exit_pending", occ=p["occ"], note="stop cancel not confirmed yet; retry next tick; not selling into a live stop")
            return "filled" if p not in self.state.open else "pending"
        p["stop_order_id"] = None; p["stop_px_resting"] = None
        self.state.save()
        return "released"

    def _market_sell(self, p: dict, now: datetime, reason: str) -> None:
        held = self._broker_long_qty(p["occ"])
        if held == 0:
            px, why = self._exit_price_after_gap(p)
            self.close(p, px, now, f"{reason}; broker already flat ({why})")
            return
        qty = int(p["contracts"])
        if held is not None and 0 < held < qty:
            self.log("error", where="exit_qty", occ=p["occ"], tracked=qty, broker=held, action="selling the broker qty only")
            p["contracts"] = held
            qty = held
            self.state.save()
        if qty <= 0:
            self.close(p, float(p.get("premium") or 0), now, reason)
            return
        for o in self.b.open_orders():
            if o.get("symbol") == p["occ"] and o.get("side") == "sell" and o.get("type") == "market":
                p["exit_order_id"] = o.get("id")
                p["exit_reason_pending"] = reason
                self.state.save()
                self.log("exit_pending", occ=p["occ"], order_id=o.get("id"), reason=reason, note="sell already working")
                return
        n = int(p.get("exit_attempts") or 0) + 1
        p["exit_attempts"] = n
        o = self.b.sell_to_close(p["occ"], qty, client_order_id=leg_cid(p, f"x{n}", now))
        self.log("order_submitted", side="sell", occ=p["occ"], qty=qty, order_id=o.get("id"), reason=reason)
        if self._consume_fill(p, o, now, reason):
            return
        if o.get("status") in DEAD_ORDER and filled_qty(o) == 0:
            self.state.save()  # keep exit_attempts so the next try uses a fresh client_order_id
            self.log("exit_rejected", occ=p["occ"], order_id=o.get("id"), status=o.get("status"), reason=reason)
            return
        f = self.wait_fill(o["id"], wait_s=30)
        if f and self._consume_fill(p, f, now, reason):
            return
        if p in self.state.open:
            p["exit_order_id"] = o.get("id")
            p["exit_reason_pending"] = reason
            self.state.save()
            self.log("exit_pending", occ=p["occ"], order_id=o.get("id"), reason=reason,
                     note="sell not filled yet; checking every tick")

    def manage_one(self, p: dict, now: datetime) -> None:
        # 0) a ladder trim from an earlier tick still working?
        if p.get("trim_order_id"):
            to = self._get_order(p["trim_order_id"])
            if self._settle_trim(p, to, now) == "pending":
                if now.time() < FLAT_BY:
                    return
                try: self.b.cancel(p["trim_order_id"])      # flatten time: cancel the trim, then sell what's left
                except Exception as e: self.log("error", where="cancel_trim", occ=p.get("occ"), error=str(e))
                if self._settle_trim(p, self._wait_done(p["trim_order_id"]), now) == "pending":
                    return
            if p not in self.state.open:
                return
        # 1) an exit order from an earlier tick still working?
        if p.get("exit_order_id"):
            eo = self._get_order(p["exit_order_id"])
            if self._consume_fill(p, eo, now, p.get("exit_reason_pending", "exit")):
                return
            if eo.get("status") in DEAD_ORDER:
                p.pop("exit_order_id", None)
                self.state.save()
            else:
                # keep the resting stop from locking the shares (or filling on top of this sell)
                if p.get("stop_order_id") and self._release_stop(p, now) == "filled":
                    return
                return
        # 2) resting stop filled, or gone (404 / rejected) so the soft stop can take over?
        elif p.get("stop_order_id"):
            so = self._get_order(p["stop_order_id"])
            if self._consume_fill(p, so, now, self.stop_label(p)):
                return
            if so.get("status") in DEAD_ORDER:
                p["stop_order_id"] = None; p["stop_px_resting"] = None
                self.state.save()
        if p not in self.state.open:
            return
        q = (self.b.option_quotes([p["occ"]]) or {}).get(p["occ"]) or {}
        bid = float(q.get("bp") or 0)
        self._update_peak(p, bid)
        reason = self.exit_reason(p, now, bid)
        if reason:
            if self._release_stop(p, now) != "released":
                return
            if p not in self.state.open:
                return
            self._market_sell(p, now, reason)
            return
        if self.ladder is not None:
            if self._ladder_step(p, now, bid):
                return
            self._maybe_move_stop(p, now, bid)
            if p not in self.state.open:
                return
        # 3) holding, and the hard stop is missing (rejected, or the process died before it was saved)
        if not p.get("stop_order_id") and not p.get("exit_order_id") and not p.get("trim_order_id") and self._should_rest_stop(p, now):
            self._place_stop(p, now)

    # ---------- trim ladder ----------
    def _ladder_step(self, p: dict, now: datetime, bid: float) -> bool:
        """Sell the next rung(s) the bid has reached. True means this tick did something (or is waiting on it)."""
        lad = self.ladder
        if not lad or not bid or now.time() >= FLAT_BY:
            return False
        entry = float(p["premium"]); contracts = int(p["contracts"])
        if contracts <= 0 or entry <= 0:
            return False
        if not p.get("orig_contracts"):
            p["orig_contracts"] = contracts + int(p.get("trimmed_qty") or 0)
        orig = int(p["orig_contracts"]); hit = int(p.get("rungs_hit") or 0)
        k = None
        for i in range(hit, len(lad)):
            if bid >= entry * (1 + lad[i][0]) - 1e-9:
                k = i
        if k is None:
            return False
        sold = max(0, orig - contracts)
        target = self.rung_target(orig, lad[k][1], sold, last=(k == len(lad) - 1))
        qty = min(contracts, target - sold)
        label = f"trim +{lad[k][0]:.0%}"
        if qty <= 0:
            p["rungs_hit"] = k + 1; self.state.save()
            return False
        if qty >= contracts:
            if self._release_stop(p, now) != "released" or p not in self.state.open:
                return True
            p["rungs_hit"] = k + 1
            self._market_sell(p, now, label + " (last contracts)")
            return True
        if self._release_stop(p, now) != "released" or p not in self.state.open:
            return True
        held = self._broker_long_qty(p["occ"])
        if held is not None and held < contracts:
            self.log("error", where="trim_qty", occ=p["occ"], tracked=contracts, broker=held, action="skip trim this tick")
            return True
        n = int(p.get("trim_attempts") or 0) + 1
        p["trim_attempts"] = n
        try:
            o = self.b.sell_to_close(p["occ"], qty, client_order_id=leg_cid(p, f"t{n}", now))
        except Exception as e:
            self.state.save()
            self.log("trim_rejected", occ=p["occ"], rung=label, qty=qty, error=str(e), note="stop re-rests this tick")
            return False
        p.update(trim_order_id=o.get("id"), trim_rung=k, trim_qty=qty)
        self.state.save()
        self.log("order_submitted", side="sell", occ=p["occ"], qty=qty, order_id=o.get("id"), reason=label)
        if self._settle_trim(p, o, now) == "pending":
            f = self.wait_fill(o["id"], wait_s=30)
            self._settle_trim(p, f or self._get_order(o["id"]), now)
        return True

    def _settle_trim(self, p: dict, o: dict, now: datetime) -> str:
        """Book a trim fill (partial P&L, smaller qty, breakeven) and re-rest the stop for what is left.
        Returns 'done' or 'pending'. A fill seen twice is booked once."""
        if p not in self.state.open:
            p.pop("trim_order_id", None); return "done"
        st = o.get("status"); fq = filled_qty(o); avg = order_avg(o)
        lad = self.ladder or []
        k = int(p.get("trim_rung") or 0); label = f"trim +{lad[k][0]:.0%}" if k < len(lad) else "trim"
        if st != "filled" and st not in DEAD_ORDER:
            return "pending"
        if fq > 0 and avg is not None:
            mark = f"{o.get('id')}:{fq}:{avg}"
            if mark not in p.get("booked_trims", []):
                p.setdefault("booked_trims", []).append(mark)
                fq = min(fq, int(p["contracts"]))
                self._book_partial(p, fq, avg)
                p["trimmed_qty"] = int(p.get("trimmed_qty") or 0) + fq
                p["trimmed"] = True
                p.setdefault("trims", []).append(dict(rung=label, qty=fq, price=avg, at=now.isoformat()))
                if fq >= int(p.get("trim_qty") or fq):
                    p["rungs_hit"] = max(int(p.get("rungs_hit") or 0), k + 1)
                p["stop_px"] = self.stop_price(p)
                self.log("trim", occ=p["occ"], rung=label, qty=fq, price=avg, left=int(p["contracts"]),
                         realized=p.get("partial_pnl"), new_stop=p["stop_px"], stop_kind=self.stop_label(p))
        elif st in DEAD_ORDER:
            self.log("trim_rejected", occ=p["occ"], order_id=o.get("id"), status=st, rung=label)
        for key in ("trim_order_id", "trim_qty", "trim_rung"):
            p.pop(key, None)
        self.state.save()
        if int(p["contracts"]) <= 0:
            self.close(p, avg if avg is not None else float(p["premium"]), now, label)
            return "done"
        if not p.get("stop_order_id") and not p.get("exit_order_id") and self._should_rest_stop(p, now):
            self._place_stop(p, now)
        return "done"

    def _maybe_move_stop(self, p: dict, now: datetime, bid: float) -> None:
        """Ratchet the resting stop up (breakeven / trail). Cancel and re-rest only on a meaningful move."""
        if not p.get("stop_order_id") or p.get("exit_order_id"):
            return
        new = self.stop_price(p); rest = float(p.get("stop_px_resting") or 0)
        if new <= rest + max(0.02, 0.03 * rest) or not bid or bid <= new:
            return
        if self._release_stop(p, now) != "released" or p not in self.state.open:
            return
        self.log("stop_moved", occ=p["occ"], old=rest, new=new, kind=self.stop_label(p))
        if self._should_rest_stop(p, now):
            self._place_stop(p, now)

    def manage(self, now: datetime) -> None:
        for p in list(self.state.open):
            try:
                self.manage_one(p, now)
            except Exception as e:            # one bad position never blocks the others
                self.log("error", where=f"manage {p.get('occ')}", error=str(e))

    def _sell_flatten(self, occ: str, qty: int, now: datetime) -> dict:
        """One flatten sell per contract per day, unless that id is already spent and the contracts are still there.
        The old id changed every minute, so a working sell was followed by a second sell (and a short) an hour later."""
        base = f"{CID_PREFIX}{now:%y%m%d}-{occ}-f"
        o = self.b.sell_to_close(occ, qty, client_order_id=base)
        spent = o.get("status") in DEAD_ORDER and filled_qty(o) == 0
        still_long = o.get("status") == "filled" and (self._broker_long_qty(occ) or 0) > 0
        if spent or still_long:
            o = self.b.sell_to_close(occ, qty, client_order_id=f"{CID_PREFIX}{now:%y%m%d%H%M%S}-{occ}-f")
        return o

    def _flatten_one(self, occ: str, qty: int, now: datetime, orders: list) -> None:
        sells = [o for o in orders if o.get("symbol") == occ and o.get("side") == "sell"]
        if any(o.get("type") == "market" for o in sells):
            self.log("flatten_pending", occ=occ, note="market sell already working")
            return
        for o in sells:   # resting stops hold the contracts; cancel and confirm before the market sell
            before = qty
            self.b.cancel(o["id"])
            done = self._wait_done(o["id"])
            fq = filled_qty(done)
            if done.get("status") == "filled" or (done.get("status") in DEAD_ORDER and fq >= before and order_avg(done) is not None):
                self.log("flatten_stop_filled", occ=occ, order_id=o.get("id"))
                return
            if done.get("status") not in DEAD_ORDER:
                self.log("flatten_pending", occ=occ, order_id=o.get("id"), note="stop cancel not confirmed")
                return
            qty -= fq
        if qty <= 0:
            return
        sold = self._sell_flatten(occ, qty, now)
        self.log("flatten_untracked", occ=occ, qty=qty, order_id=sold.get("id"))

    def flatten_untracked(self, now: datetime) -> None:
        """At/after 15:25 ET: anything the account still holds on the watchlist and isn't in state gets sold,
        and working bot buy orders are cancelled. Tracked positions are exited by manage() on the same tick.
        Runs every tick until the account is flat. A working flatten sell is left alone (same client_order_id)."""
        try:
            orders = self.b.open_orders()
            for o in orders:
                if o.get("side") == "buy" and str(o.get("client_order_id", "")).startswith(CID_PREFIX):
                    self.b.cancel(o["id"])
                    self.log("flatten_cancel_buy", order_id=o["id"], occ=o.get("symbol"))
            tracked = {p["occ"] for p in self.state.open}
            for bp in self.b.option_positions():
                occ = str(bp.get("symbol") or "")
                if not self._watch_option(bp):
                    continue
                qty = int(float(bp.get("qty") or 0))
                if qty < 0:
                    self.log("error", where="flatten", occ=occ, qty=bp.get("qty"), action="short option; not buying to cover")
                    continue
                if qty == 0 or occ in tracked:
                    continue
                self._flatten_one(occ, qty, now, orders)
        except Exception as e:
            self.log("error", where="flatten", error=str(e), action="retry next tick")

    # ---------- loop ----------
    def tick(self) -> None:
        now = self.clock()
        try:
            if self.options_ok and not self.reconciled:
                self.reconciled = self.reconcile(now)
                if self.reconciled: self.sync_equity(now)
            elif self.options_ok:
                self.cancel_stale_buys()
                self.adopt_untracked(now)
            self.manage(now)
            if now.time() >= FLAT_BY:
                if self.options_ok: self.flatten_untracked(now)
                return
            if self.options_ok and (not self.reconciled or not self.refresh_snapshot()):
                return
            dl = self.daily_loss_block(now)
            if dl and getattr(self, "_dll_logged", None) != now.date():
                self._dll_logged = now.date()
                self.log("blocked", reasons=[dl])
            for sym, sig in ([] if dl else self.scan(now)):
                try:
                    self.try_entry(sym, now, sig)
                except Exception as e:
                    self.log("error", where=f"entry {sym}", error=str(e))
                if self.options_ok: self.refresh_snapshot()
            if self.options_ok:
                self.adopt_untracked(now)   # a buy that filled after we gave up on it still needs a stop
                for p in list(self.state.open):
                    if (not p.get("stop_order_id") and not p.get("exit_order_id") and not p.get("trim_order_id")
                            and self._should_rest_stop(p, now)):
                        self._place_stop(p, now)
            self.heartbeat(now)
        except Exception as e:
            self.log("error", where="tick", error=str(e))

    def heartbeat(self, now: datetime, every_min: int = 15) -> None:
        if self.last_heartbeat and now - self.last_heartbeat < timedelta(minutes=every_min): return
        self.last_heartbeat = now
        if self.options_ok: self.sync_equity(now)
        self.log("heartbeat", open=[p["occ"] for p in self.state.open], entries_today=len(self.state.today_trades(now.date())),
                 equity=round(self.state.equity, 2))

    def run(self, once: bool = False, poll_s: int = 30) -> None:
        self.log("start", base_url=self.b.base, watchlist=self.watch, profile=self.prof.name, options_orders=self.options_ok,
                 min_score=self.min_score, min_gap_min=self.min_gap)
        try:
            self._run(once, poll_s)
        except KeyboardInterrupt:
            self.log("stopped", reason="SIGINT (manual restart, code reload or end of day)")
            raise

    def _run(self, once: bool, poll_s: int) -> None:
        while True:
            try:
                c = self.b.clock()
            except Exception as e:                      # network blip: log, back off, keep going
                self.log("error", where="clock", error=str(e))
                if once: return
                self.sleep(60); continue
            if c.get("is_open"):
                self.tick()
                if once: return
                self.sleep(poll_s)
            else:
                self.log("market_closed", next_open=c.get("next_open"), orders_sent=0)
                if once: return
                self.sleep(self._until_open(c))

    def _until_open(self, c: dict, cap_s: int = 15 * 60) -> float:
        try:
            nxt = datetime.fromisoformat(str(c.get("next_open")).replace("Z", "+00:00"))
            return max(30, min(cap_s, (nxt - datetime.now(timezone.utc)).total_seconds() + 5))
        except Exception:
            return cap_s
