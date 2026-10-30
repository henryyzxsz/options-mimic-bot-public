"""Mocked tests for the reliability + ranking work: retries, idempotent restarts, reconcile, flatten, setup scoring.
No network: every HTTP call hits FakeAlpaca or a scripted transport."""
import json, sys, tempfile, unittest, urllib.error
from datetime import datetime, timedelta, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_alpaca_paper import FakeAlpaca, make, NY, ENV
from mimic_bot import paper_loop as PL, ranking as R
from mimic_bot.broker_alpaca_paper import AlpacaPaperBroker, AlpacaAPIError, occ_root, occ_kind, _looks_like_occ
from mimic_bot.rules import load_config, load_profile

CFG = load_config()


class Scripted:
    """Transport that returns queued (status, payload) items or raises queued exceptions."""
    def __init__(self, *items): self.items = list(items); self.calls = []
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body, dict(headers)))
        x = self.items.pop(0)
        if isinstance(x, Exception): raise x
        return x


def scripted_broker(*items, **kw):
    t = Scripted(*items); sleeps = []
    return AlpacaPaperBroker(transport=t, env=ENV, sleep=sleeps.append, **kw), t, sleeps


class Retries(unittest.TestCase):
    def test_retries_5xx_then_succeeds(self):
        b, t, sleeps = scripted_broker((503, {"message": "busy"}), (502, None), (200, {"is_open": True}))
        self.assertEqual(b.clock(), {"is_open": True}); self.assertEqual(len(t.calls), 3); self.assertEqual(sleeps, [1.0, 2.0])

    def test_retries_network_errors(self):
        b, t, _ = scripted_broker(urllib.error.URLError("dns"), TimeoutError("slow"), (200, {"status": "ACTIVE"}))
        self.assertEqual(b.account()["status"], "ACTIVE")

    def test_4xx_not_retried(self):
        b, t, _ = scripted_broker((403, {"message": "forbidden"}))
        with self.assertRaises(AlpacaAPIError) as cm: b.account()
        self.assertEqual(cm.exception.status, 403); self.assertEqual(len(t.calls), 1)

    def test_gives_up_after_max_retries(self):
        b, t, _ = scripted_broker(*[(500, {})] * 3, max_retries=2)
        with self.assertRaises(AlpacaAPIError): b.clock()
        self.assertEqual(len(t.calls), 3)

    def test_post_without_client_id_is_refused(self):
        b, t, _ = scripted_broker((503, {}))
        with self.assertRaises(ValueError): b.sell_to_close("SPY261102C00601000", 1)
        self.assertEqual(t.calls, [])   # a POST with no id is not sent, so it can't be retried into a second order

    def test_422_that_mentions_client_id_but_is_not_a_duplicate_is_raised(self):
        # code 40010001 is also "invalid time_in_force"; matching any 422 that echoes the field would
        # treat a rejected order as the existing one and the bot would think the sell went out.
        b, t, _ = scripted_broker((422, {"code": 40010001, "message": "insufficient qty", "client_order_id": "omb-x"}))
        with self.assertRaises(AlpacaAPIError): b.sell_to_close("SPY261102C00601000", 1, client_order_id="omb-x")
        self.assertEqual([c[0] for c in t.calls], ["POST"])

    def test_post_retry_with_duplicate_client_id_returns_existing_order(self):
        existing = {"id": "o9", "status": "filled", "client_order_id": "omb-x"}
        b, t, _ = scripted_broker(urllib.error.URLError("reset"), (422, {"message": "client_order_id must be unique"}), (200, existing))
        self.assertEqual(b.buy_to_open("SPY261102C00601000", 1, 1.0, client_order_id="omb-x"), existing)
        self.assertEqual([c[0] for c in t.calls], ["POST", "POST", "GET"])
        self.assertIn("by_client_order_id", t.calls[2][1])

    def test_errors_never_contain_secret(self):
        b = AlpacaPaperBroker(transport=Scripted((401, {"message": "unauthorized"})), env={"APCA_API_KEY_ID": "KEYID123", "APCA_API_SECRET_KEY": "SECRET456"})
        with self.assertRaises(AlpacaAPIError) as cm: b.account()
        self.assertNotIn("SECRET456", str(cm.exception)); self.assertNotIn("KEYID123", str(cm.exception))
        self.assertNotIn("SECRET456", repr(b))

    def test_cancel_already_filled_is_quiet(self):
        b, _, _ = scripted_broker((422, {"message": "order is not cancelable"}))
        self.assertIsNone(b.cancel("o1"))

    def test_occ_helpers(self):
        self.assertTrue(_looks_like_occ("XSP261102P00580000")); self.assertFalse(_looks_like_occ("SPY"))
        self.assertEqual(occ_root("XSP261102P00580000"), "XSP"); self.assertEqual(occ_kind("XSP261102P00580000"), "put")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
        self.day = datetime(2026, 11, 2).date(); self.now = datetime(2026, 11, 2, 11, 1, tzinfo=NY)
    def tearDown(self): self.tmp.cleanup()
    def state(self, name="state.json"): return PL.PaperState(1000.0, path=self.dir / name)
    def trader(self, fake, state=None, watchlist=("SPY",), profile="active"):
        clock = lambda: self.now
        return PL.PaperTrader(make(fake), cfg=CFG, log=PL.Log(self.dir, clock=clock), state=state or self.state(),
                              watchlist=list(watchlist), clock=clock, sleep=lambda s: None, profile=profile)
    def events(self, kind=None):
        ev = [json.loads(l) for f in sorted(self.dir.glob("paper_*.jsonl")) for l in f.read_text().splitlines()]
        return [e for e in ev if kind is None or e["event"] == kind]
    def buys(self, fake): return [b for b in fake.posted() if b["side"] == "buy"]


class Restart(Base):
    def test_relaunch_with_same_state_does_not_double_enter(self):
        f = FakeAlpaca(self.day); st = self.state()
        self.trader(f, st).tick(); self.assertEqual(len(self.buys(f)), 1)
        self.now += timedelta(minutes=1)
        self.trader(f, self.state()).tick()            # new process, state reloaded from disk
        self.assertEqual(len(self.buys(f)), 1)

    def test_relaunch_with_lost_state_adopts_position_instead_of_buying(self):
        f = FakeAlpaca(self.day); self.trader(f, self.state("a.json")).tick()
        self.now += timedelta(minutes=1)
        t2 = self.trader(f, self.state("b.json")); t2.tick()   # fresh, empty state file
        self.assertEqual(len(self.buys(f)), 1)
        self.assertEqual(len(t2.state.open), 1); self.assertTrue(t2.state.open[0]["adopted"])
        self.assertTrue(self.events("reconcile_adopted"))

    def test_adopted_position_without_stop_gets_one(self):
        f = FakeAlpaca(self.day)
        f.orders["o0"] = dict(symbol="SPY261102C00601000", qty="1", side="buy", type="limit", limit_price="1.00", id="o0")
        t = self.trader(f); t.reconcile(self.now)
        self.assertEqual(f.posted()[-1]["type"], "stop"); self.assertEqual(f.posted()[-1]["stop_price"], "0.75")

    def test_local_position_gone_at_broker_is_closed_locally(self):
        st = self.state(); st.open.append(dict(time=self.now - timedelta(hours=20), symbol="SPY", kind="call", occ="SPY261102C00600000",
                                              premium=1.0, contracts=1, stop_order_id=None))
        t = self.trader(FakeAlpaca(self.day), st); t.reconcile(self.now)
        self.assertEqual(st.open, []); self.assertIn("closed while the bot was down", st.trades[-1]["exit_reason"])

    def test_stale_bot_buy_orders_cancelled_on_start(self):
        f = FakeAlpaca(self.day)
        f.orders["o0"] = dict(symbol="SPY261102C00601000", qty="1", side="buy", type="stop", stop_price="1.0", id="o0", client_order_id="omb-old-b")
        self.trader(f).reconcile(self.now)
        self.assertTrue(f.orders["o0"].get("canceled")); self.assertTrue(self.events("reconcile_cancel_stale_buy"))

    def test_signal_cooldown_survives_restart(self):
        f = FakeAlpaca(self.day); f.empty_chain = True        # signal fires but nothing to buy
        self.trader(f).tick(); self.assertEqual(len(self.events("signal")), 1)
        self.now += timedelta(minutes=2); self.trader(f).tick()
        self.assertEqual(len(self.events("signal")), 1)       # still inside the 10-min per-symbol cooldown

    def test_corrupt_state_is_moved_aside(self):
        (self.dir / "state.json").write_text("{not json")
        st = self.state(); self.assertEqual(st.trades, []); self.assertIsNotNone(st.load_error)
        self.trader(FakeAlpaca(self.day), st)
        self.assertTrue(self.events("state_reset")); self.assertTrue(list(self.dir.glob("state.corrupt-*.json")))

    def test_state_save_is_atomic_roundtrip(self):
        st = self.state(); st.last_fire["SPY"] = self.now.isoformat(); st.save()
        self.assertEqual(self.state().last_fire, {"SPY": self.now.isoformat()})
        self.assertEqual(list(self.dir.glob(".state-*")), [])


class Failures(Base):
    def test_snapshot_failure_blocks_entries(self):
        f = FakeAlpaca(self.day); orig = f.__call__
        t = self.trader(f); t.reconciled = True
        def broken(m, u, h, b):
            if "/v2/positions" in u: return 503, {"message": "down"}
            return orig(m, u, h, b)
        t.b.transport = broken; t.b.max_retries = 0; t.tick()
        self.assertEqual(f.posted(), []); self.assertTrue(any(e.get("where") == "snapshot" for e in self.events("error")))

    def test_no_contracts_in_chain_logged_not_crashed(self):
        f = FakeAlpaca(self.day); f.empty_chain = True; self.trader(f).tick()
        self.assertEqual(f.posted(), [])
        self.assertTrue(any("premium band" in r for e in self.events("blocked") for r in e["reasons"]))

    def test_stop_rejected_falls_back_to_soft_stop(self):
        f = FakeAlpaca(self.day); orig = f.__call__
        def no_stops(m, u, h, b):
            if m == "POST" and b and b.get("type") == "stop": return 422, {"message": "trade not found"}
            return orig(m, u, h, b)
        t = self.trader(no_stops); t.tick()
        self.assertTrue(self.events("stop_failed")); self.assertIsNone(t.state.open[0]["stop_order_id"])
        f.ask = 0.45; self.now += timedelta(minutes=2); t.manage(self.now)   # bid 0.43 <= 0.75
        self.assertEqual(t.state.trades[-1]["exit_reason"], "soft stop -25%")

    def test_unfilled_exit_stays_open_until_filled(self):
        f = FakeAlpaca(self.day); t = self.trader(f, profile="disciplined"); t.tick()
        orig_status = f._status; pending = {"on": True}
        f._status = lambda o: "new" if (pending["on"] and o["type"] == "market") else orig_status(o)
        self.now += timedelta(minutes=16); t.manage(self.now)
        self.assertEqual(len(t.state.open), 1); self.assertTrue(self.events("exit_pending"))
        pending["on"] = False; self.now += timedelta(seconds=30); t.manage(self.now)
        self.assertEqual(t.state.open, []); self.assertEqual(t.state.trades[-1]["exit_reason"], "time stop 15 min")
        self.assertEqual(len([b for b in f.posted() if b["type"] == "market"]), 1)   # no second sell

    def test_log_write_failure_does_not_raise(self):
        log = PL.Log(self.dir, clock=lambda: self.now); log.dir = self.dir / "missing" / "nested"
        log("heartbeat", open=[])   # must not raise

    def test_missing_stop_order_does_not_stick_the_position(self):
        f = FakeAlpaca(self.day); t = self.trader(f, profile="disciplined"); t.tick()
        t.state.open[0]["stop_order_id"] = "gone"
        t.state.open[0]["time"] = self.now - timedelta(minutes=16)
        self.now += timedelta(minutes=1); t.manage(self.now)
        self.assertEqual(t.state.open, [])
        self.assertIn("time stop", t.state.trades[-1]["exit_reason"])
        self.assertTrue(f.orders["o1"].get("canceled"))   # the real stop, whose id we lost, is still cancelled

    def test_canceled_stop_that_filled_does_not_sell_again(self):
        t = self.trader(FakeAlpaca(self.day))
        p = dict(time=self.now, symbol="SPY", kind="call", occ="SPY261102C00601000", premium=1.0, contracts=1, thesis="t")
        t.state.open.append(p)
        order = {"status": "canceled", "filled_qty": "1", "filled_avg_price": "0.70"}
        self.assertTrue(t._consume_fill(p, order, self.now, "auto stop -30%"))
        self.assertEqual(t.state.open, []); self.assertEqual(t.state.trades[-1]["pnl"], -30.0)
        self.assertEqual(t.b.transport.posted(), [])

    def test_partial_stop_fill_shrinks_the_position(self):
        t = self.trader(FakeAlpaca(self.day))
        p = dict(time=self.now, symbol="SPY", kind="call", occ="SPY261102C00601000", premium=1.0, contracts=2, thesis="t")
        t.state.open.append(p)
        partial = {"id": "stp", "status": "canceled", "filled_qty": "1", "filled_avg_price": "0.70"}
        self.assertFalse(t._consume_fill(p, partial, self.now, "auto stop -30%"))
        self.assertFalse(t._consume_fill(p, partial, self.now, "auto stop -30%"))  # same fill must not close the rest
        self.assertEqual(p["contracts"], 1); self.assertEqual(p["partial_pnl"], -30.0)
        self.assertEqual(len(t.state.open), 1)

    def test_pre_window_signal_does_not_burn_cooldown(self):
        self.now = datetime(2026, 11, 2, 9, 40, tzinfo=NY)
        t = self.trader(FakeAlpaca(self.day)); called = []
        t.signal = lambda sym, now: called.append(sym) or {"trigger": "turn", "kind": "call", "why": "x", "px": 1,
                                                          "score": 0.9, "score_parts": {}, "bar": "0935"}
        t.tick()
        self.assertEqual(called, []); self.assertEqual(t.state.last_fire, {})

    def test_later_tick_adopts_a_position_and_stops_it(self):
        from datetime import timezone
        f = FakeAlpaca(self.day); f.bars = []
        t = self.trader(f); t.reconciled = True
        occ = "SPY261102C00601000"
        f.orders["b"] = dict(symbol=occ, qty="1", side="buy", type="limit", limit_price="1.00", id="b",
                             filled_at=self.now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        t.tick()
        self.assertEqual(len(t.state.open), 1); self.assertTrue(t.state.open[0]["adopted"])
        self.assertEqual(self.buys(f), []); self.assertEqual(f.posted()[-1]["type"], "stop")
        self.assertEqual(t.state.open[0]["time"], self.now)

    def test_adopted_fill_time_still_honors_the_time_stop(self):
        from datetime import timezone
        f = FakeAlpaca(self.day); f.bars = []
        t = self.trader(f, profile="disciplined"); t.reconciled = True
        filled = (self.now - timedelta(minutes=20)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        f.orders["b"] = dict(symbol="SPY261102C00601000", qty="1", side="buy", type="limit", limit_price="1.00",
                             id="b", filled_at=filled)
        t.tick()
        self.assertEqual(t.state.open, []); self.assertIn("time stop", t.state.trades[-1]["exit_reason"])


class Flatten(Base):
    def test_flat_by_1525_including_untracked(self):
        f = FakeAlpaca(self.day); t = self.trader(f); t.tick(); self.assertEqual(len(t.state.open), 1)
        f.orders["ox"] = dict(symbol="QQQ261102P00500000", qty="1", side="buy", type="limit", limit_price="1.00", id="ox")
        t.watch.append("QQQ")
        self.now = datetime(2026, 11, 2, 15, 25, 5, tzinfo=NY); t.tick()
        self.assertEqual(t.state.open, []); self.assertEqual(t.state.trades[-1]["exit_reason"], "flat before the 0DTE cutoff")
        self.assertTrue(any(e.get("occ", "").startswith("QQQ") for e in self.events("reconcile_adopted")))
        self.assertTrue(any(b and str(b.get("symbol", "")).startswith("QQQ") and b.get("side") == "sell" for b in f.posted()))
        self.assertEqual(f._positions(), [])

    def test_untracked_flatten_cancels_stop_and_does_not_double_sell(self):
        f = FakeAlpaca(self.day); f.hold_market = True
        occ = "QQQ261102P00500000"
        f.orders["buy"] = dict(symbol=occ, qty="1", side="buy", type="limit", limit_price="1.00", id="buy")
        f.orders["stp"] = dict(symbol=occ, qty="1", side="sell", type="stop", stop_price="0.70", id="stp")
        t = self.trader(f, watchlist=("QQQ",)); t.reconciled = True
        self.now = datetime(2026, 11, 2, 15, 25, tzinfo=NY)
        t.tick(); t.tick()
        markets = [b for b in f.posted() if b and b.get("type") == "market"]
        self.assertEqual(len(markets), 1)
        self.assertTrue(f.orders["stp"].get("canceled"))
        seq = [(m, (b or {}).get("type")) for m, _, b in f.calls if m in ("DELETE", "POST")]
        self.assertLess(seq.index(("DELETE", None)), seq.index(("POST", "market")))

    def test_flatten_backup_sells_once_while_the_market_order_is_working(self):
        f = FakeAlpaca(self.day); f.hold_market = True
        occ = "QQQ261102P00500000"
        f.orders["buy"] = dict(symbol=occ, qty="1", side="buy", type="limit", limit_price="1.00", id="buy")
        f.orders["stp"] = dict(symbol=occ, qty="1", side="sell", type="stop", stop_price="0.70", id="stp")
        t = self.trader(f, watchlist=("QQQ",))
        self.now = datetime(2026, 11, 2, 15, 26, tzinfo=NY)
        t.flatten_untracked(self.now); t.flatten_untracked(self.now)
        self.assertEqual(len([b for b in f.posted() if b and b.get("type") == "market"]), 1)
        self.assertTrue(f.orders["stp"].get("canceled"))
        self.assertLessEqual(len(f.posted()[-1]["client_order_id"]), 48)

    def test_no_entries_at_or_after_flat_time(self):
        f = FakeAlpaca(self.day); self.now = datetime(2026, 11, 2, 15, 26, tzinfo=NY); self.trader(f).tick()
        self.assertEqual(self.buys(f), [])


class Ranking(Base):
    def f(self, hm="11:00", from_open=-0.2):
        h, m = map(int, hm.split(":"))
        return dict(t=datetime(2026, 11, 2, h, m, tzinfo=NY), from_open=from_open)

    def test_best_triggers_outrank_chases(self):
        s = lambda trig, why="": R.score_signal("SPY", trig, "call", why, self.f()).score
        self.assertGreater(s("news"), s("momentum")); self.assertGreater(s("turn"), s("momentum"))
        self.assertGreater(s("level", "at vwap (+0.01%) after -0.10%/15m"), s("momentum"))
        self.assertGreater(s("momentum"), s("late_lottery")); self.assertGreater(s("late_lottery"), s("open_drive"))

    def test_vwap_beats_round_and_prior_day_levels(self):
        s = lambda k: R.score_signal("SPY", "level", "call", f"at {k} (+0.01%) after -0.1%/15m", self.f()).score
        self.assertGreater(s("vwap"), s("rnd")); self.assertGreater(s("rnd"), s("pdh"))

    def test_time_of_day_and_fade_context(self):
        sc = lambda **kw: R.score_signal("QQQ", "turn", "call", "", self.f(**kw)).score
        self.assertGreater(sc(hm="10:30"), sc(hm="15:05")); self.assertGreater(sc(hm="10:30"), sc(hm="09:50"))
        self.assertGreater(sc(from_open=-0.3), sc(from_open=+0.3))    # buying calls on a down day = fading the day

    def test_shrinkage_and_aliases(self):
        self.assertAlmostEqual(R.shrink(4, 5), 0.6); self.assertEqual(R.symbol_win("XSP"), R.symbol_win("SPY"))
        self.assertEqual(R.tod_win(time(9, 0)), 0.5)

    def test_scores_in_unit_interval(self):
        for trig in R.TRIGGER_WIN:
            v = R.score_signal("IWM", trig, "put", "", self.f()).score; self.assertTrue(0 <= v <= 1)

    def test_active_profile_has_ranking_settings(self):
        p = load_profile(CFG, "active"); self.assertEqual(p.min_score, 0.56); self.assertEqual(p.min_gap_between_entries_min, 10)
        self.assertIsNone(load_profile(CFG, "disciplined").min_score)

    def test_low_score_skipped(self):
        f = FakeAlpaca(self.day); t = self.trader(f); t.min_score = 0.99; t.tick()
        self.assertEqual(f.posted(), []); self.assertTrue(any("setup score" in r for e in self.events("skipped") for r in e["reasons"]))

    def test_highest_score_first_and_global_gap(self):
        f = FakeAlpaca(self.day); t = self.trader(f, watchlist=("SPY", "QQQ")); t.tick()
        buys = self.buys(f); self.assertEqual(len(buys), 1)
        self.assertTrue(buys[0]["symbol"].startswith("QQQ"))     # QQQ (higher example prior) outranks SPY on the same setup
        self.assertTrue(any("gap between entries" in r for e in self.events("skipped") for r in e["reasons"]))

    def test_signal_logs_score(self):
        f = FakeAlpaca(self.day); self.trader(f).tick()
        sig = self.events("signal")[0]; self.assertIn("score", sig); self.assertIn("score_parts", sig)


class PaperOnly(unittest.TestCase):
    def test_assert_paper_base_rejects_lookalikes_without_echoing_them(self):
        from mimic_bot.env import assert_paper_base, PAPER_BASE_URL
        assert_paper_base({})
        assert_paper_base({"APCA_API_BASE_URL": PAPER_BASE_URL + "/v2"})
        evil = "https://user:secret@paper-api.alpaca.markets.evil.com"
        with self.assertRaises(RuntimeError) as cm:
            assert_paper_base({"APCA_API_BASE_URL": evil})
        self.assertNotIn("secret", str(cm.exception)); self.assertNotIn("evil.com", str(cm.exception))
        with self.assertRaises(RuntimeError):
            assert_paper_base({"APCA_API_BASE_URL": "https://api.alpaca.markets"})

    def test_launcher_paper_guard_and_backoff(self):
        import subprocess
        root = Path(__file__).resolve().parent.parent
        subprocess.check_call(["bash", "-n", str(root / "scripts" / "run_paper_day.sh")])
        text = (root / "scripts" / "run_paper_day.sh").read_text()
        self.assertIn("assert_paper_base", text)
        self.assertNotIn("https://paper-api.alpaca.markets*", text)
        self.assertNotIn("${wait_s:-15} * 2", text)
        self.assertIn("wait_s=15", text)


if __name__ == "__main__":
    unittest.main()
