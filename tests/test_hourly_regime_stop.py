"""Active profile: per-profile stop, hourly catch-up, scoreboard 200-day gate. No network."""
import sys, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_alpaca_paper import NY
from mimic_bot import paper_loop as PL
from mimic_bot import scoreboard_lens as SL
from mimic_bot.rules import load_config, load_profile

CFG = load_config()


class T(unittest.TestCase):
    def mk(self, profile="active"):
        t = PL.PaperTrader.__new__(PL.PaperTrader)
        t.prof = load_profile(CFG, profile); t.cfg = CFG
        t.state = PL.PaperState(1000.0, path=Path("/tmp/_hrs_state.json"))
        return t

    def test_profile_stop(self):
        self.assertIsNone(load_profile(CFG, "active").stop_loss_pct)      # falls back to [exit].stop_loss_pct
        self.assertIsNone(load_profile(CFG, "disciplined").stop_loss_pct)
        self.assertLess(CFG["exit"]["stop_loss_pct"], 0)

    def test_stop_is_required(self):
        import tempfile, os
        from mimic_bot.rules import ConfigError
        src = Path(__file__).resolve().parent.parent / "config" / "example_profile.toml"
        text = "\n".join(l for l in src.read_text().splitlines() if not l.startswith("stop_loss_pct"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"; p.write_text(text)
            with self.assertRaises(ConfigError): load_config(p)
            p.write_text(text.replace("\n[exit]\n", "\n[exit]\nstop_loss_pct = 0.2\n"))
            with self.assertRaises(ConfigError): load_config(p)

    def test_hourly_catchup(self):
        t = self.mk()
        self.assertFalse(t.hourly_catchup(datetime(2026, 11, 2, 10, 10, tzinfo=NY)))
        self.assertTrue(t.hourly_catchup(datetime(2026, 11, 2, 10, 35, tzinfo=NY)))
        self.assertFalse(t.hourly_catchup(datetime(2026, 11, 2, 11, 35, tzinfo=NY)))   # catch-up ends at 11:00 ET
        t.state.today_trades = lambda d: [dict(time=datetime(2026, 11, 2, 10, 5, tzinfo=NY))]
        self.assertFalse(t.hourly_catchup(datetime(2026, 11, 2, 10, 35, tzinfo=NY)))
        self.assertFalse(self.mk("disciplined").hourly_catchup(datetime(2026, 11, 2, 10, 35, tzinfo=NY)))

    def test_regime(self):
        self.assertEqual(SL.regime_from_spy_closes([100.0] * 199 + [120.0]), "invested")
        self.assertEqual(SL.regime_from_spy_closes([100.0] * 199 + [80.0]), "cash")
        self.assertEqual(SL.regime_from_spy_closes([1.0] * 50), "unknown")


if __name__ == "__main__":
    unittest.main()
