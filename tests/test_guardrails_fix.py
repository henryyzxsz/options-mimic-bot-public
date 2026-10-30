"""Regression: an hourly catch-up entry after two 1-lot losses must not be sized up by % sizing.
No size-up after a loss, catch-up floor/size/after-loss rules, restart persistence, equity sync. No network."""
import json, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_alpaca_paper import FakeAlpaca, make, NY
from mimic_bot import paper_loop as PL
from mimic_bot.guardrails import check
from mimic_bot.rules import load_config, load_profile

CFG = load_config()
ACTIVE = load_profile(CFG, "active")
DAY = datetime(2026, 11, 2, tzinfo=NY)


def at(h, m):
    return DAY.replace(hour=h, minute=m)


def trade(entry, exit_, pnl, n=1, sym="QQQ", **kw):
    return dict(time=entry, exit_time=exit_, symbol=sym, pnl=pnl, contracts=n, **kw)


def streak_state(path):
    """Synthetic day: three 1-lot wins, then two 1-lot losses."""
    st = PL.PaperState(100_000, path=path)
    st.trades += [trade(at(10, 5), at(10, 20), 40.0), trade(at(10, 25), at(10, 35), 20.0, sym="IWM"),
                  trade(at(11, 0), at(11, 30), 60.0), trade(at(12, 0), at(12, 15), -30.0),
                  trade(at(12, 45), at(13, 15), -25.0)]
    return st


class NoSizeUpAfterLoss(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_loss_streak_caps_at_one_lot(self):
        st = streak_state(self.dir / "s.json")
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.25, now=at(14, 35)), 1)      # uncapped % size would be 40
        self.assertEqual(len(st.loss_streak(DAY.date())), 2)

    def test_win_ends_the_cap(self):
        st = streak_state(self.dir / "s.json")
        st.trades.append(trade(at(14, 0), at(14, 10), 15.0))
        self.assertIsNone(st.size_cap_after_loss(DAY.date()))
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.25, now=at(14, 35)), 40)

    def test_next_day_resets(self):
        st = streak_state(self.dir / "s.json")
        tomorrow = at(10, 0) + timedelta(days=1)
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.25, now=tomorrow), 40)

    def test_cap_is_smallest_losing_size_and_uses_original_qty(self):
        st = PL.PaperState(100_000, path=self.dir / "s.json")
        st.trades += [trade(at(10, 0), at(10, 10), -100.0, n=4),
                      trade(at(10, 20), at(10, 40), -45.0, n=0, orig_contracts=10)]   # ladder trims shrank `contracts`
        self.assertEqual(st.size_cap_after_loss(DAY.date()), 4)
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.0, now=at(11, 0)), 4)

    def test_too_expensive_still_zero(self):
        st = streak_state(self.dir / "s.json"); st.equity = 1000
        self.assertEqual(PL.size_for(st, ACTIVE, premium=2.0), 0)

    def test_catchup_smaller_size(self):
        st = PL.PaperState(100_000, path=self.dir / "s.json")
        self.assertEqual(PL.size_for(st, ACTIVE, premium=1.25, now=at(14, 35), mult=0.6), 24)

    def test_cap_survives_restart(self):
        p = self.dir / "state.json"
        st = streak_state(p); st.save()
        again = PL.PaperState(1000.0, path=p)
        self.assertEqual(again.size_cap_after_loss(DAY.date()), 1)
        self.assertEqual(again.last_loss_exit(), at(13, 15))
        self.assertEqual(again.equity, 100_000)

    def test_cooldown_applies_to_catchup_proposals(self):
        st = streak_state(self.dir / "s.json")
        prop = dict(time=at(13, 18), symbol="SPY", kind="call", strike=600, expiry=DAY.date(), premium=1.0,
                    contracts=1, earnings_before_expiry=False, thesis="t", catchup=True)
        self.assertTrue(any("cooldown" in r for r in check(prop, st, ACTIVE, CFG)))


class CatchupAndEquity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
        self.now = at(10, 35)
    def tearDown(self): self.tmp.cleanup()

    def trader(self, fake, st):
        clock = lambda: self.now
        t = PL.PaperTrader(make(fake), cfg=CFG, log=PL.Log(self.dir, clock=clock), state=st, watchlist=["SPY"],
                           clock=clock, sleep=lambda s: None, profile="active")
        t.refresh_snapshot()
        return t

    def events(self):
        return [json.loads(l) for f in sorted(self.dir.glob("paper_*.jsonl")) for l in f.read_text().splitlines()]

    def sig(self, score):
        return dict(trigger="turn", kind="call", why="test", px=600.0, score=score, score_parts={}, bar="1035")

    def test_settings(self):
        self.assertEqual(ACTIVE.hourly_catchup_min_score, 0.48)
        self.assertEqual(ACTIVE.hourly_catchup_size_mult, 0.6)
        self.assertFalse(ACTIVE.hourly_catchup_after_loss)

    def test_catchup_window_ends_at_1100(self):
        t = self.trader(FakeAlpaca(self.day()), PL.PaperState(100_000, path=self.dir / "s.json"))
        self.assertEqual(ACTIVE.hourly_catchup_until_et, "11:00")
        self.assertTrue(t.hourly_catchup(at(10, 30)))
        self.assertTrue(t.hourly_catchup(at(10, 59)))
        self.assertTrue(t.hourly_catchup(at(9, 50)))        # first hour: from the 09:45 entry start
        self.assertFalse(t.hourly_catchup(at(9, 40)))       # before the earliest entry
        for h, m in [(11, 0), (11, 35), (12, 30), (14, 35), (15, 5)]:
            self.assertFalse(t.hourly_catchup(at(h, m)), f"{h}:{m:02d}")

    def test_no_catchup_entry_after_1100(self):
        self.now = at(11, 35)
        f = FakeAlpaca(self.day(), ask=1.00); t = self.trader(f, PL.PaperState(100_000, path=self.dir / "s.json"))
        t.try_entry("SPY", self.now, self.sig(0.52))
        self.assertEqual(f.posted(), [])
        self.assertNotIn("hourly_catchup", [e["event"] for e in self.events()])
        self.assertTrue(any("setup score 0.52 < 0.56" in r for e in self.events() if e["event"] == "skipped" for r in e["reasons"]))

    def test_catchup_entry_in_10_to_11_window(self):
        self.now = at(10, 40)
        f = FakeAlpaca(self.day(), ask=1.00); t = self.trader(f, PL.PaperState(100_000, path=self.dir / "s.json"))
        t.try_entry("SPY", self.now, self.sig(0.52))
        self.assertIn("hourly_catchup", [e["event"] for e in self.events()])
        self.assertEqual(f.posted()[0]["qty"], "30")

    def test_normal_entries_after_1100_unchanged(self):
        self.now = at(13, 40)
        f = FakeAlpaca(self.day(), ask=1.00); t = self.trader(f, PL.PaperState(100_000, path=self.dir / "s.json"))
        t.try_entry("SPY", self.now, self.sig(0.60))
        self.assertEqual(f.posted()[0]["qty"], "50")

    def test_catchup_below_floor_skipped(self):
        f = FakeAlpaca(self.day(), ask=1.00); t = self.trader(f, PL.PaperState(100_000, path=self.dir / "s.json"))
        t.try_entry("SPY", self.now, self.sig(0.42))
        self.assertEqual(f.posted(), [])
        self.assertTrue(any("catch-up needs setup score" in r for e in self.events() if e["event"] == "skipped" for r in e["reasons"]))

    def test_catchup_blocked_in_loss_streak(self):
        st = PL.PaperState(100_000, path=self.dir / "s.json")
        st.trades.append(trade(at(9, 46), at(9, 58), -30.0))      # morning loss, cooldown long over at 10:35
        f = FakeAlpaca(self.day(), ask=1.00); t = self.trader(f, st)
        t.try_entry("SPY", self.now, self.sig(0.52))
        self.assertEqual(f.posted(), [])
        self.assertTrue(any("after a loss" in r for e in self.events() if e["event"] == "skipped" for r in e["reasons"]))

    def test_catchup_ok_is_smaller_size(self):
        f = FakeAlpaca(self.day(), ask=1.00); t = self.trader(f, PL.PaperState(100_000, path=self.dir / "s.json"))
        t.try_entry("SPY", self.now, self.sig(0.52))
        self.assertEqual(f.posted()[0]["qty"], "30")              # 5% of $100k at $1.00 = 50, x0.6
        self.assertTrue(t.state.open[0].get("catchup"))

    def test_full_score_after_loss_capped(self):
        self.now = at(14, 35)
        f = FakeAlpaca(self.day(), ask=1.00); t = self.trader(f, streak_state(self.dir / "s.json"))
        t.try_entry("SPY", self.now, self.sig(0.60))
        self.assertEqual(f.posted()[0]["qty"], "1")
        self.assertIn("size_capped_after_loss", [e["event"] for e in self.events()])

    def test_equity_synced_from_alpaca(self):
        f = FakeAlpaca(self.day(), ask=1.00)
        st = PL.PaperState(1000.0, path=self.dir / "s.json"); st.equity = -321.0
        t = self.trader(f, st); t.tick()
        self.assertEqual(t.state.equity, 100_000.0)
        self.assertIn("equity_synced", [e["event"] for e in self.events()])
        self.assertEqual(json.loads((self.dir / "s.json").read_text())["equity"], 100_000.0)

    def test_corrupt_state_rebuilt_from_event_log(self):
        log = PL.Log(self.dir, clock=lambda: self.now)
        log("fill", occ="QQQ261102C00600000", price=1.15, qty=3)
        log("exit", symbol="QQQ", occ="QQQ261102C00600000", reason="auto stop", entry=1.15, exit=0.6, pnl=-165.0,
            entry_time=at(12, 0).isoformat())
        (self.dir / "s.json").write_text("{not json")
        t = self.trader(FakeAlpaca(self.day()), PL.PaperState(1000.0, path=self.dir / "s.json"))
        self.assertEqual(t.state.size_cap_after_loss(DAY.date()), 3)
        self.assertIsNotNone(t.state.last_loss_exit())
        self.assertIn("state_rebuilt", [e["event"] for e in self.events()])

    def test_sigint_is_logged(self):
        t = self.trader(FakeAlpaca(self.day()), PL.PaperState(100_000, path=self.dir / "s.json"))
        def boom(*a, **k): raise KeyboardInterrupt
        t._run = boom
        with self.assertRaises(KeyboardInterrupt): t.run()
        self.assertIn("stopped", [e["event"] for e in self.events()])

    def day(self):
        return DAY.date()


if __name__ == "__main__":
    unittest.main()
