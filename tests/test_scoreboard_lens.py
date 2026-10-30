"""Unit tests for the scoreboard side lens (no network)."""
import unittest

from mimic_bot import scoreboard_lens as SL


class SleeveMapping(unittest.TestCase):
    def test_momentum_triggers(self):
        self.assertEqual(SL.sleeve_for(trigger="momentum"), "momentum")
        self.assertEqual(SL.sleeve_for(trigger="open_drive"), "momentum")

    def test_balanced_triggers(self):
        self.assertEqual(SL.sleeve_for(trigger="turn"), "balanced")
        self.assertEqual(SL.sleeve_for(trigger="level"), "balanced")

    def test_fade_thesis(self):
        self.assertEqual(SL.sleeve_for(thesis="fade dip: SPY moved -0.12% in 30m"), "balanced")

    def test_chase_thesis(self):
        self.assertEqual(SL.sleeve_for(thesis="momentum: +0.30% then +0.40% (chase)"), "momentum")

    def test_unlabeled(self):
        self.assertEqual(SL.sleeve_for(trigger="news"), "unlabeled")
        self.assertEqual(SL.sleeve_for(trigger="late_lottery"), "unlabeled")


class Regime(unittest.TestCase):
    def test_unknown_without_history(self):
        self.assertEqual(SL.regime_from_spy_closes(None), "unknown")
        self.assertEqual(SL.regime_from_spy_closes([1.0] * 199), "unknown")

    def test_invested_above_200d(self):
        closes = [100.0] * 199 + [110.0]
        self.assertEqual(SL.regime_from_spy_closes(closes), "invested")

    def test_cash_below_200d(self):
        closes = [100.0] * 199 + [90.0]
        self.assertEqual(SL.regime_from_spy_closes(closes), "cash")


class Annotate(unittest.TestCase):
    def test_early_sample_note(self):
        out = SL.annotate(symbol="NVDA", trigger="turn", session_count=5)
        self.assertIn("early", out["sample_note"])
        self.assertIn("126 sessions", out["sample_note"])

    def test_index_red_flags(self):
        out = SL.annotate(symbol="SPY", trigger="turn")
        self.assertIn("do not apply", out["red_flags"])
        self.assertIn("regime=unknown", out["line"])
        self.assertIn(SL.REGIME_UNKNOWN_NOTE, out["line"])

    def test_stock_checklist(self):
        out = SL.annotate(symbol="NVDA", trigger="turn")
        self.assertIn("checklist", out["red_flags"])
        self.assertIn("delisting", out["red_flags"])

    def test_line_includes_sleeve(self):
        out = SL.annotate(symbol="SPY", trigger="level", spy_daily_closes=[500.0] * 200)
        self.assertIn("sleeve=balanced", out["line"])
        self.assertIn("regime=invested", out["line"])


if __name__ == "__main__":
    unittest.main()
