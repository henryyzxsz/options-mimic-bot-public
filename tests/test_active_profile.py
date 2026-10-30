"""Mocked tests for the `active` paper profile (default for `python run.py --paper`). No network."""
import sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_alpaca_paper import FakeAlpaca, make, NY, Loop
from mimic_bot import paper_loop as PL
from mimic_bot.guardrails import check
from mimic_bot.rules import load_config, load_profile

CFG = load_config()
ACTIVE = load_profile(CFG, "active")


def prop(t, sym="SPY", premium=1.0):
    return dict(time=t, symbol=sym, kind="call", strike=600, expiry=t.date(), premium=premium, contracts=1,
                earnings_before_expiry=False, thesis="test")


class ActiveGuardrails(unittest.TestCase):
    def setUp(self):
        self.st = PL.PaperState(1000.0, path=Path("/nonexistent/never_saved.json"))
        self.t = datetime(2026, 11, 2, 11, 0, tzinfo=NY)

    def test_settings(self):
        self.assertEqual(ACTIVE.watchlist, ["SPY", "QQQ", "IWM", "XSP"])
        self.assertEqual(ACTIVE.max_trades_per_session, 20); self.assertEqual(ACTIVE.max_open_per_symbol, 1)
        self.assertEqual(ACTIVE.reentry_cooldown_after_loss_min, 5); self.assertTrue(ACTIVE.no_size_up_after_loss)
        self.assertAlmostEqual(ACTIVE.portfolio_fraction, 0.05)
        self.assertFalse(ACTIVE.allow_lottery_premium); self.assertEqual(ACTIVE.max_trades_per_symbol_per_day, {})
        self.assertEqual(CFG["exit"]["stop_loss_pct"], -0.25)   # example value

    def test_entries_from_0945(self):
        self.assertEqual(check(prop(self.t.replace(hour=9, minute=50)), self.st, ACTIVE, CFG), [])
        self.assertTrue(check(prop(self.t.replace(hour=9, minute=40)), self.st, ACTIVE, CFG))

    def test_no_entries_after_1510(self):
        self.assertEqual(check(prop(self.t.replace(hour=15, minute=5)), self.st, ACTIVE, CFG), [])
        self.assertTrue(check(prop(self.t.replace(hour=15, minute=10)), self.st, ACTIVE, CFG))

    def test_multiple_spy_trades_allowed(self):
        for k in range(3):
            self.st.trades.append(dict(time=self.t - timedelta(hours=1, minutes=k), exit_time=self.t - timedelta(minutes=50),
                                       symbol="SPY", pnl=10.0, contracts=1))
        self.assertEqual(check(prop(self.t), self.st, ACTIVE, CFG), [])

    def test_five_min_cooldown(self):
        self.st.trades.append(dict(time=self.t - timedelta(minutes=10), exit_time=self.t - timedelta(minutes=3), symbol="QQQ", pnl=-30.0, contracts=1))
        self.assertTrue(any("cooldown" in r for r in check(prop(self.t), self.st, ACTIVE, CFG)))
        self.assertEqual(check(prop(self.t + timedelta(minutes=3)), self.st, ACTIVE, CFG), [])

    def test_daily_cap_20(self):
        for k in range(20):
            self.st.trades.append(dict(time=self.t - timedelta(minutes=k), exit_time=self.t, symbol="QQQ", pnl=1.0, contracts=1))
        self.assertTrue(any("daily cap" in r for r in check(prop(self.t), self.st, ACTIVE, CFG)))

    def test_one_open_per_symbol(self):
        self.st.open.append(dict(time=self.t, symbol="SPY", contracts=1))
        self.assertTrue(any("open SPY" in r for r in check(prop(self.t), self.st, ACTIVE, CFG)))
        self.assertEqual(check(prop(self.t, sym="IWM"), self.st, ACTIVE, CFG), [])

    def test_cheap_premium_still_blocked(self):
        self.assertTrue(check(prop(self.t, premium=0.40), self.st, ACTIVE, CFG))


class ActivePortfolioSizing(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 11, 2, 11, 0, tzinfo=NY)

    def test_five_pct_examples(self):
        st = PL.PaperState(100_000, path=Path("/nonexistent/never_saved.json"))
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.0), 50)
        self.assertEqual(PL.size_for(st, ACTIVE, premium=0.40), 125)

    def test_too_expensive_blocks_zero_qty(self):
        st = PL.PaperState(1000, path=Path("/nonexistent/never_saved.json"))
        self.assertEqual(PL.size_for(st, ACTIVE, premium=2.0), 0)

    def test_after_loss_clamped_to_losing_size(self):
        # No size-up after a loss also holds for % sizing.
        st = PL.PaperState(100_000, path=Path("/nonexistent/never_saved.json"))
        st.trades.append(dict(time=self.t - timedelta(minutes=10), exit_time=self.t - timedelta(minutes=3),
                              symbol="QQQ", pnl=-30.0, contracts=1))
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.0), 1)
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.0, now=self.t), 1)

    def test_equity_from_broker_account(self):
        st = PL.PaperState(1000.0, path=Path("/nonexistent/never_saved.json"))

        class Acct:
            def account(self):
                return {"equity": "100000"}

        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.0, broker=Acct()), 50)

    def test_loop_uses_five_pct_qty(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            d = Path(tmp.name)
            day = datetime(2026, 11, 2).date()
            now = datetime(2026, 11, 2, 11, 1, tzinfo=NY)
            f = FakeAlpaca(day, ask=1.00)
            t = PL.PaperTrader(make(f), cfg=CFG, log=PL.Log(d, clock=lambda: now),
                               state=PL.PaperState(1000.0, path=d / "s.json"), watchlist=["SPY"],
                               clock=lambda: now, sleep=lambda s: None, profile="active")
            t.tick()
            self.assertEqual(f.posted()[0]["qty"], "50")
        finally:
            tmp.cleanup()


class ActiveLoop(Loop):
    """Reuse the mocked loop fixture with the active profile."""
    def trader(self, fake, state=None, watchlist=("SPY",)):
        clock = lambda: self.now
        st = state or PL.PaperState(1000.0, path=self.dir / "state.json")
        return PL.PaperTrader(make(fake), cfg=CFG, log=PL.Log(self.dir, clock=clock), state=st, watchlist=list(watchlist),
                              clock=clock, sleep=lambda s: None, profile="active")

    # tests from Loop that encode disciplined-only rules don't apply to `active`
    def test_signal_buys_and_places_stop(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f); t.tick()
        posts = f.posted(); kinds = [e["event"] for e in self.events()]
        self.assertIn("signal", kinds); self.assertIn("fill", kinds); self.assertIn("stop_placed", kinds)
        self.assertEqual(posts[0]["side"], "buy"); self.assertEqual(posts[0]["type"], "limit"); self.assertEqual(posts[0]["qty"], "50")
        self.assertEqual(posts[1]["type"], "stop"); self.assertEqual(posts[1]["stop_price"], "0.75")
        self.assertEqual(len(t.state.open), 1)

    def test_stop_fill_recorded_as_loss(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f); t.tick()
        f.stop_fills = True; self.now += timedelta(minutes=2); t.manage(self.now)
        self.assertEqual(t.state.trades[-1]["exit_reason"], "auto stop -25%")
        self.assertAlmostEqual(t.state.trades[-1]["pnl"], -1250.0)

    def test_one_spy_per_day(self):
        st = PL.PaperState(1000.0, path=self.dir / "state.json")
        st.trades.append(dict(time=self.now - timedelta(hours=1), exit_time=self.now - timedelta(minutes=50), symbol="SPY", pnl=10.0, contracts=1))
        f = FakeAlpaca(self.day); self.trader(f, st).tick()
        self.assertEqual(f.posted()[0]["side"], "buy")          # second SPY trade of the day is allowed on paper
    def test_no_entries_before_10(self):
        self.now = datetime(2026, 11, 2, 9, 40, tzinfo=NY)
        f = FakeAlpaca(self.day); self.trader(f).tick(); self.assertEqual(f.posted(), [])

    def test_cooldown_after_loss(self):
        st = PL.PaperState(1000.0, path=self.dir / "state.json")
        st.trades.append(dict(time=self.now - timedelta(minutes=10), exit_time=self.now - timedelta(minutes=3), symbol="QQQ", pnl=-30.0, contracts=1))
        f = FakeAlpaca(self.day); self.trader(f, st).tick(); self.assertEqual(f.posted(), [])

    def test_spx_maps_to_xsp_on_spy_tape(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f, watchlist=("SPX",))
        self.assertEqual(t.watch, ["XSP"]); t.tick()
        self.assertTrue(any("/v2/stocks/SPY/bars" in u for _, u, _ in f.calls))
        self.assertFalse(any("/v2/stocks/XSP" in u for _, u, _ in f.calls))
        self.assertTrue(any("underlying_symbols=XSP" in u for _, u, _ in f.calls))
        self.assertEqual(f.posted()[0]["side"], "buy")

    def test_closed_market_once_sends_nothing(self):
        f = FakeAlpaca(self.day); orig = f.__call__
        def closed(m, u, h, b):
            if u.endswith("/v2/clock"): return 200, {"is_open": False, "next_open": "2026-11-03T09:30:00-05:00"}
            return orig(m, u, h, b)
        PL.PaperTrader(make(closed), cfg=CFG, log=PL.Log(self.dir, clock=lambda: self.now),
                       state=PL.PaperState(1000.0, path=self.dir / "s.json"), clock=lambda: self.now,
                       sleep=lambda s: None, profile="active").run(once=True)
        self.assertEqual(f.posted(), [])
        self.assertIn("market_closed", [e["event"] for e in self.events()])

    def test_time_stop_sells(self):
        # active: no 15-min time stop; the position is held (stop resting) until the ladder, stop or 15:25 flatten
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f); t.tick()
        self.now += timedelta(minutes=16); t.manage(self.now)
        self.assertEqual(len(t.state.open), 1); self.assertEqual(f.posted()[-1]["type"], "stop")


del Loop
if __name__ == "__main__":
    unittest.main()
