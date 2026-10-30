"""Brokers. PaperBroker is the only one that 'executes'. LiveOrderTicket NEVER sends anything:
it prints an order ticket and requires a human to type the exact confirmation phrase, then the
HUMAN places the order at their own broker themselves. No real-money order placement exists in this codebase; the only
orders sent through an API are Alpaca PAPER orders (broker_alpaca_paper.py, hard-wired to paper-api.alpaca.markets)."""
from datetime import datetime

class PaperBroker:
    def __init__(self, equity):
        self.equity = equity; self.trades = []; self.open = []
    def today_trades(self, d):
        return [t for t in self.trades + self.open if t["time"].date() == d]
    def last_loss_exit(self):
        losses = [t for t in self.trades if t["pnl"] < 0]
        return max((t["exit_time"] for t in losses), default=None)
    def buy(self, p):
        self.open.append({**p}); return p
    def sell(self, p, px, when, reason):
        p.update(exit_time=when, exit_premium=round(px, 2), exit_reason=reason,
                 pnl=round((px - p["premium"]) * 100 * p["contracts"], 2))
        p["pnl_pct"] = (px - p["premium"]) / p["premium"]
        self.equity += p["pnl"]; self.open.remove(p); self.trades.append(p)

class LiveOrderTicket:
    """Suggest-only. You place the order manually; nothing here talks to a broker."""
    PHRASE = "I PLACE THIS MYSELF"
    def suggest(self, p, stop_pct, target_pct, scoreboard_line=None):
        extra = f"  scoreboard: {scoreboard_line}\n" if scoreboard_line else ""
        ticket = (f"\n=== SUGGESTION (not an order) ===\nBUY {p['contracts']}x {p['symbol']} {p['strike']} {p['kind'].upper()} "
                  f"exp {p['expiry']} limit ${p['premium']:.2f}\n  thesis: {p['thesis']}\n{extra}"
                  f"  stop: ${p['premium']*(1+stop_pct):.2f} ({stop_pct:.0%})  target: ${p['premium']*(1+target_pct):.2f} (+{target_pct:.0%})\n"
                  f"  affect check: rate how you feel 1-5 and name it; if >=4 or you just took a loss, wait 10 min.\n")
        print(ticket)
        ans = input(f"Type '{self.PHRASE}' to log that you will enter it manually (anything else skips): ")
        return ans.strip() == self.PHRASE
