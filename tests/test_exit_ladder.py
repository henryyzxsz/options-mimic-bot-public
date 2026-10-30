"""Active profile exit plan: trim ladder, breakeven after the first trim, trailing stop, daily loss limit.
Mocked: every HTTP call hits FakeAlpaca. Disciplined keeps its +20% target and 15-min time stop."""
import sys, unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_alpaca_paper import FakeAlpaca, NY
from test_engineering_pass import Base
from mimic_bot import paper_loop as PL
from mimic_bot.rules import load_config, load_profile

CFG = load_config()


def stops(f): return [b for b in f.posted() if b["type"] == "stop"]
def sells(f): return [b for b in f.posted() if b["type"] == "market"]


class LadderMath(unittest.TestCase):
    def test_profile_fields(self):
        a, d = load_profile(CFG, "active"), load_profile(CFG, "disciplined")
        self.assertEqual(a.exit_plan, "ladder"); self.assertEqual(a.time_stop_min, 0)
        self.assertEqual(a.trim_ladder_gains, [0.25, 0.6, 0.75, 1.0, 1.5, 2.0, 5.0, 10.0, 20.0])
        self.assertAlmostEqual(a.trail_arm_pct, 0.75); self.assertAlmostEqual(a.trail_pct, 0.60)
        self.assertAlmostEqual(a.daily_loss_limit_pct, 0.10)
        self.assertEqual(d.exit_plan, "target"); self.assertIsNone(d.daily_loss_limit_pct); self.assertIsNone(d.time_stop_min)

    def test_rung_targets(self):
        t = PL.PaperTrader.__new__(PL.PaperTrader); t.prof = load_profile(CFG, "active"); t.cfg = CFG
        lad = t.ladder
        self.assertEqual([g for g, _ in lad], [0.25, 0.6, 0.75, 1.0, 1.5, 2.0, 5.0, 10.0, 20.0])
        self.assertAlmostEqual(lad[0][1], 0.5); self.assertAlmostEqual(lad[1][1], 0.75); self.assertEqual(lad[-1][1], 1.0)
        def walk(n):
            sold, out = 0, []
            for i, (_, cum) in enumerate(lad):
                if sold >= n: break
                tgt = PL.PaperTrader.rung_target(n, cum, sold, i == len(lad) - 1)
                out.append(tgt - sold); sold = tgt
            return out
        self.assertEqual(walk(50), [25, 13, 2, 2, 1, 2, 2, 2, 1])
        self.assertEqual(sum(walk(50)), 50)
        self.assertEqual(walk(1), [1]); self.assertEqual(walk(2), [1, 1]); self.assertEqual(walk(4), [2, 1, 1])
        self.assertTrue(all(q >= 1 for n in range(1, 60) for q in walk(n)))
        self.assertTrue(all(sum(walk(n)) == n for n in range(1, 60)))

    def test_stop_price_rules(self):
        t = PL.PaperTrader.__new__(PL.PaperTrader); t.prof = load_profile(CFG, "active"); t.cfg = CFG; t.stop_pct = -0.25
        p = dict(premium=1.0)
        self.assertEqual(t.stop_price(p), 0.75)                       # before any trim: profile stop
        p["trimmed"] = True; self.assertEqual(t.stop_price(p), 1.00)  # breakeven
        p.update(trail_armed=True, peak_bid=3.0); self.assertEqual(t.stop_price(p), 1.20)   # 60% below the peak
        p["stop_px"] = 1.20; p["peak_bid"] = 2.0
        self.assertEqual(t.stop_price(p), 1.20)                       # ratchets: a lower peak never lowers it
        d = PL.PaperTrader.__new__(PL.PaperTrader); d.prof = load_profile(CFG, "disciplined"); d.cfg = CFG; d.stop_pct = -0.30
        self.assertIsNone(d.ladder); self.assertEqual(d.stop_price(dict(premium=1.0, trimmed=True)), 0.70)


class Ladder(Base):
    def entered(self, f):
        t = self.trader(f); t.tick()
        self.assertEqual(len(t.state.open), 1); self.assertEqual(t.state.open[0]["contracts"], 50)
        return t, t.state.open[0]

    def step(self, t, f, ask, minutes=1):
        f.ask = ask; self.now += timedelta(minutes=minutes); t.manage(self.now)

    def test_trims_at_25_and_60_with_breakeven_stop(self):
        f = FakeAlpaca(self.day, ask=1.00); t, p = self.entered(f)
        self.step(t, f, 1.27)                                       # bid 1.25 = +25%
        self.assertEqual(p["contracts"], 25); self.assertTrue(p["trimmed"]); self.assertEqual(p["rungs_hit"], 1)
        self.assertEqual(sells(f)[-1]["qty"], "25"); self.assertAlmostEqual(p["partial_pnl"], 0.27 * 100 * 25)
        self.assertTrue(f.orders["o1"].get("canceled"))              # the original resting stop was cancelled first
        self.assertEqual(stops(f)[-1]["qty"], "25"); self.assertEqual(stops(f)[-1]["stop_price"], "1.00")   # breakeven
        self.assertEqual(p["stop_order_id"], next(k for k, o in f.orders.items() if o is not None and o.get("client_order_id") == stops(f)[-1]["client_order_id"]))
        self.step(t, f, 1.27)                                       # same rung again: nothing new
        self.assertEqual(len(sells(f)), 1)
        self.step(t, f, 1.62)                                       # bid 1.60 = +60%
        self.assertEqual(sells(f)[-1]["qty"], "13"); self.assertEqual(p["contracts"], 12); self.assertEqual(p["rungs_hit"], 2)
        self.assertEqual(stops(f)[-1]["qty"], "12"); self.assertEqual(stops(f)[-1]["stop_price"], "1.00")
        cids = [b["client_order_id"] for b in f.posted()]
        self.assertEqual(len(cids), len(set(cids)))                 # Alpaca never reuses ids
        self.assertTrue(any(e["rung"] == "trim +25%" for e in self.events("trim")))
        # the breakeven stop fills: the trade closes with the trims' profit, not a loss
        f.stop_fills = True; self.step(t, f, 0.98)
        self.assertEqual(t.state.open, []); tr = t.state.trades[-1]
        self.assertEqual(tr["exit_reason"], "breakeven stop")
        self.assertAlmostEqual(tr["pnl"], 0.27 * 100 * 25 + 0.62 * 100 * 13 + 0.0)

    def test_no_target_or_time_stop_for_active(self):
        f = FakeAlpaca(self.day, ask=1.00); t, p = self.entered(f)
        self.step(t, f, 1.22, minutes=20)                          # bid +20%, 20 min held: old rules would have sold
        self.assertEqual(sells(f), []); self.assertEqual(len(t.state.open), 1)

    def test_trailing_stop_arms_at_75_and_ratchets(self):
        f = FakeAlpaca(self.day, ask=1.00); t, p = self.entered(f)
        self.step(t, f, 1.77)                                       # bid 1.75: rungs +25/+60/+75 at once -> 40 sold
        self.assertTrue(p["trail_armed"]); self.assertEqual(p["contracts"], 10); self.assertEqual(p["rungs_hit"], 3)
        self.assertEqual(stops(f)[-1]["stop_price"], "1.00")        # trail (0.70) is under breakeven, so breakeven wins
        self.step(t, f, 2.62)                                       # bid 2.60: rungs +100/+150 -> sold to 43
        self.assertEqual(p["contracts"], 7)
        self.assertEqual(stops(f)[-1]["stop_price"], "1.04"); self.assertEqual(stops(f)[-1]["qty"], "7")
        self.step(t, f, 2.97)                                       # bid 2.95, no rung: trail ratchets 1.04 -> 1.18
        self.assertEqual(stops(f)[-1]["stop_price"], "1.18"); self.assertTrue(self.events("stop_moved"))
        n = len(stops(f))
        self.step(t, f, 2.02)                                       # pullback: the stop never moves down
        self.assertEqual(len(stops(f)), n); self.assertEqual(p["stop_px"], 1.18)
        f.stop_fills = True; self.step(t, f, 1.20)
        self.assertEqual(t.state.open, []); self.assertEqual(t.state.trades[-1]["exit_reason"], "trailing stop")

    def test_soft_trailing_stop_when_alpaca_rejects_stops(self):
        f = FakeAlpaca(self.day, ask=1.00); orig = f.__call__; block = {"on": False}
        def maybe_reject(m, u, h, b):
            if block["on"] and m == "POST" and b and b.get("type") == "stop": return 422, {"message": "rejected"}
            return orig(m, u, h, b)
        t = self.trader(maybe_reject); t.tick(); p = t.state.open[0]
        block["on"] = True
        self.step(t, f, 1.27)                                       # trim, re-rest rejected -> soft breakeven
        self.assertIsNone(p["stop_order_id"]); self.assertTrue(self.events("stop_failed"))
        self.step(t, f, 1.01)                                       # bid 0.99 <= 1.00
        self.assertEqual(t.state.open, []); self.assertEqual(t.state.trades[-1]["exit_reason"], "soft breakeven stop")

    def test_one_contract_sells_all_at_first_rung(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f)
        t.prof.portfolio_fraction = 0.001; t.tick()                # 0.1% of $100k at $1.00 -> a 1-lot
        p = t.state.open[0]; self.assertEqual(p["contracts"], 1)
        self.step(t, f, 1.27)
        self.assertEqual(t.state.open, []); self.assertEqual(sells(f)[-1]["qty"], "1")
        self.assertIn("trim +25%", t.state.trades[-1]["exit_reason"]); self.assertAlmostEqual(t.state.trades[-1]["pnl"], 27.0)

    def test_restart_resumes_ladder_state(self):
        f = FakeAlpaca(self.day, ask=1.00); t, p = self.entered(f)
        self.step(t, f, 1.27)
        t2 = self.trader(f, self.state())                           # new process, state from disk
        self.now += timedelta(minutes=1); f.ask = 1.27; t2.tick()
        p2 = t2.state.open[0]
        self.assertEqual(p2["rungs_hit"], 1); self.assertTrue(p2["trimmed"]); self.assertEqual(p2["contracts"], 25)
        self.assertEqual(p2["orig_contracts"], 50); self.assertEqual(p2["stop_px"], 1.0)
        self.assertEqual(len(sells(f)), 1)                          # +25% not sold twice
        self.assertFalse(self.events("reconcile_adopted"))
        self.step(t2, f, 1.62)
        self.assertEqual(sells(f)[-1]["qty"], "13"); self.assertEqual(p2["contracts"], 12)

    def test_pending_trim_survives_restart_and_books_once(self):
        f = FakeAlpaca(self.day, ask=1.00); t, p = self.entered(f)
        f.hold_market = True; self.step(t, f, 1.27)
        self.assertTrue(p.get("trim_order_id")); self.assertEqual(p["contracts"], 50)
        t2 = self.trader(f, self.state()); f.hold_market = False
        self.now += timedelta(minutes=1); t2.tick(); p2 = t2.state.open[0]
        self.assertEqual(p2["contracts"], 25); self.assertNotIn("trim_order_id", p2)
        self.assertAlmostEqual(p2["partial_pnl"], 675.0)
        self.now += timedelta(minutes=1); t2.manage(self.now)
        self.assertAlmostEqual(p2["partial_pnl"], 675.0); self.assertEqual(len(sells(f)), 1)
        self.assertEqual(stops(f)[-1]["qty"], "25")

    def test_flatten_still_clears_runners(self):
        f = FakeAlpaca(self.day, ask=1.00); t, p = self.entered(f)
        self.step(t, f, 1.27)
        self.now = self.now.replace(hour=15, minute=25, second=5); t.tick()
        self.assertEqual(t.state.open, []); self.assertEqual(t.state.trades[-1]["exit_reason"], "flat before the 0DTE cutoff")


class DailyLossLimit(Base):
    def loss(self, pnl, st=None):
        st = st or self.state()
        st.trades.append(dict(time=self.now - timedelta(hours=1), exit_time=self.now - timedelta(minutes=30), symbol="QQQ",
                              pnl=pnl, contracts=10, occ="QQQ261102C00500000", premium=1.0))
        st.save(); return st

    def test_blocks_entries_after_limit(self):
        f = FakeAlpaca(self.day); t = self.trader(f, self.loss(-10000.0)); t.tick()   # 10% of $100,000
        self.assertEqual(self.buys(f), [])
        self.assertTrue(any("daily loss limit" in r for e in self.events("blocked") for r in e["reasons"]))
        self.now += timedelta(minutes=1); t.tick()
        self.assertEqual(len([e for e in self.events("blocked") if "daily loss limit" in e["reasons"][0]]), 1)   # logged once

    def test_under_limit_still_trades(self):
        f = FakeAlpaca(self.day); self.trader(f, self.loss(-5000.0)).tick()
        self.assertEqual(len(self.buys(f)), 1)

    def test_yesterdays_loss_does_not_count(self):
        st = self.state()
        st.trades.append(dict(time=self.now - timedelta(days=1), exit_time=self.now - timedelta(days=1), symbol="QQQ",
                              pnl=-40000.0, contracts=10))
        f = FakeAlpaca(self.day); self.trader(f, st).tick()
        self.assertEqual(len(self.buys(f)), 1)

    def test_enforced_after_restart(self):
        self.loss(-12000.0)
        f = FakeAlpaca(self.day); self.trader(f, self.state()).tick()       # fresh process, same state file
        self.assertEqual(self.buys(f), [])

    def test_partial_trim_losses_count(self):
        st = self.loss(-6000.0)
        st.open.append(dict(time=self.now - timedelta(minutes=5), symbol="QQQ", kind="call", occ="QQQ261102C00500000",
                            premium=1.0, contracts=5, partial_pnl=-4500.0, stop_order_id=None))
        t = self.trader(FakeAlpaca(self.day), st)
        self.assertEqual(t.realized_today(self.now), -10500.0)
        self.assertIsNotNone(t.daily_loss_block(self.now))

    def test_disciplined_has_no_limit(self):
        t = self.trader(FakeAlpaca(self.day), self.loss(-40000.0), profile="disciplined")
        self.assertIsNone(t.daily_loss_block(self.now))


class DisciplinedUnchanged(Base):
    def test_target_20(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f, profile="disciplined"); t.tick()
        self.assertEqual(t.state.open[0]["contracts"], 1); self.assertNotIn("rungs_hit", t.state.open[0])
        self.assertEqual(stops(f)[-1]["stop_price"], "0.75")
        f.ask = 1.22; self.now += timedelta(minutes=2); t.manage(self.now)
        self.assertEqual(t.state.trades[-1]["exit_reason"], "target +20%"); self.assertEqual(sells(f)[-1]["qty"], "1")

    def test_time_stop_15(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f, profile="disciplined"); t.tick()
        self.now += timedelta(minutes=16); t.manage(self.now)
        self.assertEqual(t.state.trades[-1]["exit_reason"], "time stop 15 min")


if __name__ == "__main__":
    unittest.main()
