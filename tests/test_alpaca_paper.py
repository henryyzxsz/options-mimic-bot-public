"""Mocked tests for the Alpaca paper broker and paper loop. No network: every HTTP call hits a fake."""
import json, sys, tempfile, unittest, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mimic_bot.broker_alpaca_paper import AlpacaPaperBroker, LiveTradingRefused, PAPER_BASE_URL, DATA_BASE_URL
from mimic_bot import paper_loop as PL
from mimic_bot.rules import load_config

NY = ZoneInfo("America/New_York")
ENV = {"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"}


class FakeAlpaca:
    """Records calls; serves bars that form a dip-and-turn on SPY so the 'turn' trigger fires."""
    def __init__(self, day, options_level=3, bars=None, ask=1.00, stop_fills=False):
        self.calls = []; self.day = day; self.level = options_level; self.ask = ask; self.stop_fills = stop_fills
        self.bars = bars if bars is not None else self._dip_and_turn()
        self.orders = {}; self.empty_chain = False; self.hold_market = False
    def _dip_and_turn(self):
        out = []; t = datetime(self.day.year, self.day.month, self.day.day, 9, 30, tzinfo=NY)
        px = [600.0] * 12 + [599.6, 599.2, 598.8, 599.2, 599.6, 600.0] + [600.0] * 6
        for k, c in enumerate(px):
            out.append(dict(t=(t + timedelta(minutes=5 * k)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            o=c, h=c + .05, l=c - .05, c=c, v=1000))
        return out
    def _status(self, o):
        if o.get("canceled"): return "canceled"
        if o.get("type") == "market" and self.hold_market: return "new"
        filled = o["type"] != "stop" or self.stop_fills
        return "filled" if filled else "new"
    def _view(self, o):
        st = self._status(o)
        px = float(o.get("limit_price") or o.get("stop_price") or self.ask)
        return dict({k: v for k, v in o.items() if k != "canceled"}, status=st,
                    filled_avg_price=str(px) if st == "filled" else None, filled_qty=o["qty"] if st == "filled" else "0")
    def _positions(self):
        held = {}
        for o in self.orders.values():
            if self._status(o) != "filled": continue
            held[o["symbol"]] = held.get(o["symbol"], 0) + (int(o["qty"]) if o["side"] == "buy" else -int(o["qty"]))
        return [dict(symbol=s, qty=str(q), asset_class="us_option", avg_entry_price=str(self.ask)) for s, q in held.items() if q]
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        assert url.startswith(PAPER_BASE_URL) or url.startswith(DATA_BASE_URL), url
        path = url.split(".markets", 1)[1].split("?")[0]
        if path == "/v2/account": return 200, {"status": "ACTIVE", "equity": "100000", "buying_power": "200000", "options_trading_level": self.level}
        if path == "/v2/clock": return 200, {"is_open": True, "next_open": "x"}
        if path == "/v2/positions": return 200, self._positions()
        if path.startswith("/v2/stocks/"): return 200, {"bars": self.bars, "next_page_token": None}
        if path == "/v2/options/contracts":
            kind = "call" if "type=call" in url else "put"; cp = "C" if kind == "call" else "P"
            root = urllib.parse.parse_qs(url.split("?", 1)[1]).get("underlying_symbols", ["SPY"])[0]
            if self.empty_chain: return 200, {"option_contracts": []}
            return 200, {"option_contracts": [dict(symbol=f"{root}{self.day:%y%m%d}{cp}{int(k*1000):08d}", strike_price=str(k),
                                                   expiration_date=self.day.isoformat(), type=kind) for k in range(595, 606)]}
        if path == "/v1beta1/options/quotes/latest":
            syms = url.split("symbols=")[1].split("&")[0].replace("%2C", ",").split(",")
            return 200, {"quotes": {s: {"bp": self.ask - .02, "ap": self.ask} for s in syms}}
        if path == "/v2/orders" and method == "POST":
            cid = body.get("client_order_id")
            if cid and any(o.get("client_order_id") == cid for o in self.orders.values()):
                return 422, {"code": 40010001, "message": "client_order_id must be unique"}
            oid = f"o{len(self.orders)}"; self.orders[oid] = dict(body, id=oid); return 200, {"id": oid, "status": "new"}
        if path == "/v2/orders" and method == "GET":
            if "status=closed" in url:
                return 200, [self._view(o) for o in self.orders.values() if self._status(o) != "new"][::-1]
            return 200, [self._view(o) for o in self.orders.values() if self._status(o) == "new"]
        if path == "/v2/orders:by_client_order_id":
            cid = urllib.parse.parse_qs(url.split("?", 1)[1])["client_order_id"][0]
            o = next((o for o in self.orders.values() if o.get("client_order_id") == cid), None)
            return (200, self._view(o)) if o else (404, {"message": "order not found"})
        if path.startswith("/v2/orders/") and method == "GET":
            oid = path.rsplit("/", 1)[1]
            if oid not in self.orders: return 404, {"message": "order not found"}
            return 200, self._view(self.orders[oid])
        if path.startswith("/v2/orders/") and method == "DELETE":
            o = self.orders.get(path.rsplit("/", 1)[1])
            if o is None or self._status(o) == "filled": return 422, {"message": "order is not cancelable"}
            o["canceled"] = True; return 204, None
        return 404, {"message": "not mocked " + path}
    def posted(self): return [b for m, u, b in self.calls if m == "POST"]


def make(fake, **kw):
    return AlpacaPaperBroker(transport=fake, env=ENV, **kw)


class BrokerSafety(unittest.TestCase):
    def test_refuses_live_url(self):
        with self.assertRaises(LiveTradingRefused): make(FakeAlpaca(datetime(2026, 11, 2).date()), base_url="https://api.alpaca.markets")
    def test_refuses_live_url_from_env(self):
        with self.assertRaises(LiveTradingRefused):
            AlpacaPaperBroker(transport=FakeAlpaca(datetime(2026, 11, 2).date()), env={**ENV, "APCA_API_BASE_URL": "https://api.alpaca.markets"})
    def test_paper_url_from_env_ok(self):
        b = AlpacaPaperBroker(transport=FakeAlpaca(datetime(2026, 11, 2).date()), env={**ENV, "APCA_API_BASE_URL": PAPER_BASE_URL + "/v2"})
        self.assertEqual(b.base, PAPER_BASE_URL)
    def test_needs_keys(self):
        with self.assertRaises(RuntimeError): AlpacaPaperBroker(transport=lambda *a: (200, {}), env={})
    def test_data_host_is_read_only(self):
        b = make(FakeAlpaca(datetime(2026, 11, 2).date()))
        with self.assertRaises(LiveTradingRefused): b._req("POST", DATA_BASE_URL, "/v2/orders", body={})
    def test_check_is_read_only(self):
        f = FakeAlpaca(datetime(2026, 11, 2).date()); r = make(f).check()
        self.assertEqual(r["options_trading_level"], 3); self.assertEqual(f.posted(), [])
        self.assertTrue(all(m == "GET" for m, _, _ in f.calls))


class Loop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
        self.day = datetime(2026, 11, 2).date()   # first Monday after DST ends (EST)
        self.now = datetime(2026, 11, 2, 11, 1, tzinfo=NY)
    def tearDown(self): self.tmp.cleanup()
    def trader(self, fake, state=None):
        clock = lambda: self.now
        log = PL.Log(self.dir, clock=clock)
        st = state or PL.PaperState(2000.0, path=self.dir / "state.json")
        return PL.PaperTrader(make(fake), cfg=load_config(), log=log, state=st, watchlist=["SPY"], clock=clock, sleep=lambda s: None)
    def events(self):
        return [json.loads(l) for f in sorted(self.dir.glob("paper_*.jsonl")) for l in f.read_text().splitlines()]

    def test_dst_safe_bar_times(self):
        bars = PL.to_tape_bars(FakeAlpaca(self.day)._dip_and_turn(), self.now)
        from mimic_bot import triggers as T
        f = T.Tape(bars).features(len(bars) - 1)
        self.assertEqual(f["t"].strftime("%H:%M"), "11:00")

    def test_signal_buys_and_places_stop(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f); t.tick()
        posts = f.posted(); kinds = [e["event"] for e in self.events()]
        self.assertIn("signal", kinds); self.assertIn("fill", kinds); self.assertIn("stop_placed", kinds)
        sig = next(e for e in self.events() if e["event"] == "signal")
        self.assertIn("scoreboard", sig); self.assertIn("sleeve=balanced", sig["scoreboard"])
        self.assertEqual(posts[0]["side"], "buy"); self.assertEqual(posts[0]["type"], "limit"); self.assertEqual(posts[0]["qty"], "1")
        self.assertEqual(posts[1]["type"], "stop"); self.assertEqual(posts[1]["stop_price"], "0.75")
        self.assertEqual(len(t.state.open), 1)

    def test_cheap_premium_blocked(self):
        f = FakeAlpaca(self.day, ask=0.40); self.trader(f).tick()
        self.assertEqual(f.posted(), [])
        self.assertIn("blocked", [e["event"] for e in self.events()])

    def test_no_entries_before_10(self):
        self.now = datetime(2026, 11, 2, 9, 56, tzinfo=NY)
        f = FakeAlpaca(self.day); self.trader(f).tick(); self.assertEqual(f.posted(), [])

    def test_cooldown_after_loss(self):
        st = PL.PaperState(2000.0, path=self.dir / "state.json")
        st.trades.append(dict(time=self.now - timedelta(minutes=20), exit_time=self.now - timedelta(minutes=5), symbol="QQQ", pnl=-30.0, contracts=1))
        f = FakeAlpaca(self.day); self.trader(f, st).tick()
        self.assertEqual(f.posted(), [])
        blocked = [e for e in self.events() if e["event"] == "blocked"]
        self.assertTrue(any("cooldown" in r for e in blocked for r in e["reasons"]))

    def test_one_spy_per_day(self):
        st = PL.PaperState(2000.0, path=self.dir / "state.json")
        st.trades.append(dict(time=self.now - timedelta(hours=1), exit_time=self.now - timedelta(minutes=50), symbol="SPY", pnl=10.0, contracts=1))
        f = FakeAlpaca(self.day); self.trader(f, st).tick(); self.assertEqual(f.posted(), [])

    def test_no_size_up_after_loss(self):
        from mimic_bot.rules import load_profile
        st = PL.PaperState(2000.0, path=self.dir / "state.json")
        st.trades.append(dict(time=self.now, exit_time=self.now, symbol="QQQ", pnl=-5.0, contracts=1))
        self.assertEqual(PL.size_for(st, load_profile(load_config(), "disciplined"), base=3), 1)

    def test_fallback_without_options(self):
        f = FakeAlpaca(self.day, options_level=0); self.trader(f).tick()
        self.assertEqual(f.posted(), [])
        kinds = [e["event"] for e in self.events()]
        self.assertIn("fallback_signal_only", kinds); self.assertIn("would_buy", kinds)

    def test_stop_fill_recorded_as_loss(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f); t.tick()
        f.stop_fills = True; self.now += timedelta(minutes=2); t.manage(self.now)
        self.assertEqual(t.state.trades[-1]["exit_reason"], "auto stop -25%")
        self.assertAlmostEqual(t.state.trades[-1]["pnl"], -25.0)

    def test_time_stop_sells(self):
        f = FakeAlpaca(self.day, ask=1.00); t = self.trader(f); t.tick()
        self.now += timedelta(minutes=16); t.manage(self.now)
        self.assertEqual(f.posted()[-1]["side"], "sell"); self.assertEqual(f.posted()[-1]["type"], "market")
        self.assertTrue(any(m == "DELETE" for m, _, _ in f.calls))   # resting stop cancelled first

    def test_every_order_logged(self):
        f = FakeAlpaca(self.day); t = self.trader(f); t.tick(); self.now += timedelta(minutes=16); t.manage(self.now)
        self.assertEqual(len(f.posted()) - 1, len([e for e in self.events() if e["event"] == "order_submitted"]))  # stop logged as stop_placed


if __name__ == "__main__":
    unittest.main()
