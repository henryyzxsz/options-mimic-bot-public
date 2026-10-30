#!/usr/bin/env python3
"""CLI. Paper/backtest only. `suggest` prints tickets for you to place manually; nothing is sent to any broker.
`--paper` sends orders to Alpaca's PAPER environment only (no real money).
  python run.py backtest [--profile disciplined|mimic] [--mode fade|mimic_raw]
  python run.py compare
  python run.py suggest --symbol SPY      (uses the latest bar in your own data/underlying_10m.json)
  python run.py suggest --triggers --symbol SPY [--mode mimic|disciplined]   (latest bar in your own data/bars_5m.json)
  python run.py --paper --check            (read-only Alpaca PAPER account check; places nothing)
  python run.py --paper [--once] [--profile active|disciplined] [--symbols SPY,QQQ,IWM,XSP]   (Alpaca PAPER only; see README)
  scripts/run_paper_day.sh                 (all-day unattended paper run with auto-restart)
"""
import argparse, json
from mimic_bot import backtest
from mimic_bot.rules import load_config, load_profile
from mimic_bot.signals import propose
from mimic_bot.guardrails import check
from mimic_bot.broker import PaperBroker, LiveOrderTicket

ap = argparse.ArgumentParser(); ap.add_argument("cmd", nargs="?", choices=["backtest", "compare", "suggest"])
ap.add_argument("--profile", default=None, help="backtest: disciplined|mimic (default disciplined); --paper: active|disciplined (default active)"); ap.add_argument("--mode", default="fade"); ap.add_argument("--symbol", default="SPY")
ap.add_argument("--triggers", action="store_true", help="use mimic_bot.triggers instead of the simple fade rule")
ap.add_argument("--paper", action="store_true", help="run the signal loop on Alpaca PAPER (paper-api.alpaca.markets only)")
ap.add_argument("--check", action="store_true", help="with --paper: read-only account check, no orders")
ap.add_argument("--once", action="store_true", help="with --paper: run one tick and exit")
ap.add_argument("--symbols", default=None, help="with --paper: comma-separated watchlist (default from the profile; SPX maps to XSP)")
a = ap.parse_args()
if a.paper:
    from mimic_bot.env import load_dotenv
    from mimic_bot.broker_alpaca_paper import AlpacaPaperBroker
    load_dotenv()
    try: b = AlpacaPaperBroker()
    except RuntimeError as e: raise SystemExit(f"paper mode: {e}")
    if a.check:
        print(json.dumps(b.check(), indent=1)); raise SystemExit
    from mimic_bot.paper_loop import PaperTrader
    wl = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] if a.symbols else None
    try: PaperTrader(b, watchlist=wl, profile=a.profile or "active").run(once=a.once)
    except KeyboardInterrupt: print("paper loop stopped (SIGINT)")
    raise SystemExit
if not a.cmd:
    ap.error("pick a command (backtest, compare, suggest) or use --paper")
a.profile = a.profile or "disciplined"
if a.cmd == "backtest":
    print(json.dumps(backtest.run(a.profile, a.mode, verbose=True), indent=1, default=str))
elif a.cmd == "compare":
    for prof, mode in [("mimic", "mimic_raw"), ("mimic", "fade"), ("disciplined", "mimic_raw"), ("disciplined", "fade")]:
        print(json.dumps(backtest.run(prof, mode), default=str))
elif a.cmd == "suggest" and a.triggers:
    from mimic_bot import triggers as T
    from mimic_bot import scoreboard_lens as SL
    mode = a.mode if a.mode in T.MODES else "disciplined"
    tp = T.Tape(T.load_bars()[a.symbol]); f = tp.features(len(tp.b) - 1)
    s = T.evaluate(a.symbol, f, T.DEFAULTS, mode)
    if s:
        lens = SL.annotate(symbol=a.symbol, trigger=s[0], thesis=s[2])
        print(f"{a.symbol} {f['t']:%Y-%m-%d %H:%M} ET px {f['px']}: SUGGEST {s[1].upper()} ({s[0]}: {s[2]}) - place manually if you agree; nothing was sent")
        print(lens["line"])
    else:
        print(f"{a.symbol} {f['t']:%Y-%m-%d %H:%M} ET px {f['px']}: no trigger")
elif a.cmd == "suggest":
    from mimic_bot import scoreboard_lens as SL
    cfg = load_config(); prof = load_profile(cfg, a.profile)
    bars = backtest.load_bars_10m()[a.symbol]
    p = propose(a.symbol, bars, len(bars) - 1, cfg)
    if not p: print("No setup on the latest bar."); raise SystemExit
    why = check(p, PaperBroker(cfg["account"]["paper_starting_equity"]), prof, cfg)
    if why: print("Setup found but blocked:", why); raise SystemExit
    lens = SL.annotate(symbol=a.symbol, thesis=p.get("thesis"))
    LiveOrderTicket().suggest(p, cfg["exit"]["stop_loss_pct"], cfg["exit"]["profit_target_pct"], scoreboard_line=lens["line"])
