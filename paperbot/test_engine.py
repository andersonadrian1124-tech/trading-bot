"""Offline tests for the risk engine and paper fills (no network, no API key).
Run:  python test_engine.py"""
import os, tempfile, unittest
import bot


def make():
    d = tempfile.mkdtemp()
    cfg = bot.Cfg()
    cfg.db_path = os.path.join(d, "t.db")
    cfg.fee_pct = 0.0
    cfg.slippage_pct = 0.0
    bot.notify = lambda *_: None
    return cfg, bot.Engine(cfg, bot.DB(cfg.db_path))


def setup(ticker="BTC", mode="swing", entry=100, stop=97, t1=106, t2=112):
    return {"ticker": ticker, "mode": mode, "entry": entry, "stop": stop, "t1": t1, "t2": t2, "thesis": "t"}


class T(unittest.TestCase):
    def test_sizing_risks_one_percent_and_caps_position(self):
        cfg, e = make()
        ok, why, usd = e.evaluate(setup(), 100)
        self.assertTrue(ok, why)
        self.assertAlmostEqual(usd, 1250)          # 1% risk would be $1,667; 25% position cap wins
        ok, why, usd = e.evaluate(setup(stop=90, t1=120), 100)   # 10% stop -> $50 / 0.10 = $500
        self.assertAlmostEqual(usd, 500)

    def test_stop_then_trim_then_breakeven_stop(self):
        cfg, e = make()
        pid, why = e.open_position(setup(), 100)
        self.assertIsNotNone(pid, why)
        e.tick({"BTC": {"p": 106.5}})                # T1: sell half, stop to entry
        p = e.db.one("SELECT * FROM positions WHERE id=?", pid)
        self.assertEqual(p["trimmed"], 1)
        self.assertAlmostEqual(p["stop"], 100)
        self.assertGreater(p["realized"], 0)
        e.tick({"BTC": {"p": 99}})                   # falls back through entry
        p = e.db.one("SELECT * FROM positions WHERE id=?", pid)
        self.assertEqual(p["status"], "closed")
        self.assertEqual(p["exit_reason"], "stop at entry")

    def test_full_stop_loss_is_about_one_percent_of_balance(self):
        cfg, e = make()
        pid, _ = e.open_position(setup(stop=90, t1=120, t2=130), 100)    # risk $50
        e.tick({"BTC": {"p": 89}})
        loss = e.db.one("SELECT realized FROM positions WHERE id=?", pid)["realized"]
        self.assertLess(loss, -50)                   # gap through the stop costs a bit more, still ~1%
        self.assertGreater(loss, -60)

    def test_rejections(self):
        cfg, e = make()
        self.assertFalse(e.evaluate(setup(t1=101), 100)[0])                 # reward:risk too low
        self.assertFalse(e.evaluate(setup(stop=100), 100)[0])               # no real stop
        self.assertFalse(e.evaluate(setup(stop=50, t1=300), 100)[0])        # stop too far
        self.assertFalse(e.evaluate(setup(mode="scalp", stop=99, t1=101), 100)[0])  # scalp T1 < 2%

    def test_alt_swing_limit_and_duplicates(self):
        cfg, e = make()
        for t in ("SOL", "LINK"):
            self.assertIsNotNone(e.open_position(setup(ticker=t), 100)[0])
        ok, why, _ = e.evaluate(setup(ticker="NEAR"), 100)
        self.assertFalse(ok); self.assertIn("alt swing", why)
        self.assertFalse(e.evaluate(setup(ticker="SOL"), 100)[0])           # already holding

    def test_daily_loss_cap_blocks_new_entries(self):
        cfg, e = make()
        for t in ("BTC", "ETH"):
            pid, _ = e.open_position(setup(ticker=t, stop=90, t1=120, t2=130), 100)
            e.tick({t: {"p": 89}})
        ok, why, _ = e.evaluate(setup(ticker="XRP"), 100)
        self.assertFalse(ok); self.assertIn("daily loss cap", why)

    def test_pause_file(self):
        cfg, e = make()
        open(bot.PAUSE_FILE, "w").close()
        try:
            self.assertFalse(e.evaluate(setup(), 100)[0])
        finally:
            os.remove(bot.PAUSE_FILE)

    def test_pending_limit_fills_and_invalidates(self):
        cfg, e = make()
        e.db.x("INSERT INTO pending(ticker,mode,limit_price,stop,t1,t2,thesis,created_at,expires_at) VALUES"
               "('SOL','swing',95,92,102,108,'x',?,?)", bot.now_iso(), "2999-01-01T00:00:00+00:00")
        e.tick({"SOL": {"p": 99}})
        self.assertEqual(len(e.open_positions()), 0)                        # price not at limit yet
        e.tick({"SOL": {"p": 95}})
        self.assertEqual(len(e.open_positions()), 1)
        e.db.x("INSERT INTO pending(ticker,mode,limit_price,stop,t1,t2,thesis,created_at,expires_at) VALUES"
               "('NEAR','swing',50,45,60,70,'x',?,?)", bot.now_iso(), "2999-01-01T00:00:00+00:00")
        e.tick({"NEAR": {"p": 44}})                                         # blew through the stop first
        self.assertEqual(e.db.one("SELECT status FROM pending WHERE ticker='NEAR'")["status"], "cancelled")

    def test_drawdown_halves_risk(self):
        cfg, e = make()
        e.db.x("INSERT INTO positions(ticker,mode,status,realized,closed_at) VALUES('X','swing','closed',-600,?)", bot.now_iso())
        e.db.x("INSERT INTO events(ts,kind,ticker,mode,pnl) VALUES(?,?,?,?,?)", bot.now_iso(), "exit", "X", "swing", -600)
        rs = e.risk_state()
        self.assertTrue(rs["half_risk"])
        self.assertAlmostEqual(rs["unit_risk"], 4400 * 0.01 * 0.5)


class Scan(unittest.TestCase):
    @staticmethod
    def prod(base, price, chg, vol_usd, quote="USD", **kw):
        return {"product_id": f"{base}-{quote}", "base_currency_id": base, "quote_currency_id": quote,
                "product_type": "SPOT", "status": "online", "price": str(price),
                "price_percentage_change_24h": str(chg), "approximate_quote_24h_volume": str(vol_usd), **kw}

    def fake(self):
        p = self.prod
        return [p("BTC", 100, 1.0, 9e8), p("SOL", 50, 2.0, 3e8), p("AAA", 1, 30.0, 5e6), p("BBB", 2, -20.0, 8e6),
                p("CCC", 3, 40.0, 1e4),                          # too illiquid
                p("USDC", 1, 0.0, 9e9),                          # stablecoin
                p("EEE", 5, 90.0, 9e9, quote="EUR"),             # not a USD pair
                p("FFF", 5, 90.0, 9e9, trading_disabled=True),   # trading disabled
                p("GGG", 5, 50.0, 9e9, status="offline"),
                p("HHH", 7, 12.0, 2e6, volume_24h=None)]

    def test_screen_filters_and_ranks(self):
        cands, st = bot.screen_universe(self.fake(), bot.Cfg())
        names = {c["ticker"] for c in cands}
        self.assertIn("AAA", names)                    # top gainer
        self.assertIn("BBB", names)                    # biggest dip
        self.assertIn("HHH", names)                    # passes the $1M bar
        for bad in ("CCC", "USDC", "EEE", "FFF", "GGG", "BTC"):
            self.assertNotIn(bad, names)               # illiquid / stable / non-USD / disabled / already watched
        self.assertEqual(st["universe"], 9)            # USD pairs only
        self.assertEqual({c["ticker"]: c["why"] for c in cands}["AAA"], "top 24h gainer")

    def test_parse_product_units(self):
        price, ch, vol = bot.parse_product({"price": "100", "price_percentage_change_24h": "2.5",
                                            "volume_24h": "10"})            # no quote-volume field: base volume x price
        self.assertEqual((price, ch, vol), (100.0, 0.025, 1000.0))
        self.assertEqual(bot.parse_product({})[0], 0.0)                     # missing data does not crash

    def test_parse_candles_sorts_oldest_first(self):
        rows = {"candles": [{"start": "200", "low": "1", "high": "3", "open": "2", "close": "2.5"},
                            {"start": "100", "low": "1", "high": "2", "open": "1.5", "close": "2"}]}
        c = bot.parse_candles(rows)
        self.assertEqual([x["t"] for x in c], [100, 200])
        self.assertEqual(c[1]["h"], 3.0)
        self.assertEqual(bot.parse_candles({}), [])

    def test_scanned_coins_risk_half(self):
        cfg, e = make()
        ok, _, core = e.evaluate(setup(ticker="SOL", stop=90, t1=120), 100)       # on the watchlist
        ok2, _, scanned = e.evaluate(setup(ticker="AAA", stop=90, t1=120), 100)   # not on it
        self.assertTrue(ok and ok2)
        self.assertAlmostEqual(scanned, core / 2)

    def test_brain_accepts_scanned_ticker_only_when_picked(self):
        cfg, e = make()
        b = bot.Brain(cfg, e.db, e)
        act = [{"type": "open", "ticker": "AAA", "mode": "swing", "entry": 100, "stop": 95, "t1": 110, "t2": 120}]
        px = {"AAA": {"p": 100, "ch": 0}}
        self.assertTrue(all("opened" not in o for o in b.apply(act, px)))   # not in this cycle's scan list
        b.allowed = {"AAA"}
        self.assertTrue(any("opened" in o for o in b.apply(act, px)))


class Stats(unittest.TestCase):
    def test_stats_and_curve(self):
        cfg, e = make()
        win, _ = e.open_position(setup(ticker="BTC", stop=90, t1=120, t2=130), 100)   # risk $50
        e.tick({"BTC": {"p": 131}})                                                    # T2: full win
        lose, _ = e.open_position(setup(ticker="ETH", stop=90, t1=120, t2=130), 100)
        e.tick({"ETH": {"p": 89}})                                                     # stopped out
        st = e.stats()
        self.assertEqual((st["n"], st["wins"]), (2, 1))
        self.assertAlmostEqual(st["win_rate"], 0.5)
        self.assertGreater(st["avg_win"], 0)
        self.assertLess(st["avg_loss"], 0)
        self.assertIsNotNone(st["profit_factor"])
        self.assertIsNotNone(st["avg_r"])
        self.assertEqual(st["by_mode"]["swing"]["n"], 2)
        curve = e.curve()
        self.assertAlmostEqual(curve[-1]["v"], e.balance())   # curve ends at the real balance
        self.assertAlmostEqual(e.snapshot({})["stats"]["total"], st["total"])

    def test_empty_stats_do_not_crash(self):
        cfg, e = make()
        st = e.stats()
        self.assertEqual(st["n"], 0)
        self.assertIsNone(st["win_rate"])


class Wire(unittest.TestCase):
    """Fake the HTTP layer to check the requests we send and how we read replies."""
    def fake_get(self, calls):
        import unittest.mock as m

        def get(url, params=None, timeout=None):
            calls.append((url, dict(params or {})))
            r = m.Mock(); r.raise_for_status = lambda: None
            if url.endswith("/candles"):
                r.json = lambda: {"candles": [{"start": "20", "low": "1", "high": "2", "open": "1", "close": "2", "volume": "5"},
                                              {"start": "10", "low": "1", "high": "2", "open": "1", "close": "1.5", "volume": "5"}]}
            elif "/products/" in url:
                r.json = lambda: {"product_id": "SOL-USD", "price": "120.5", "price_percentage_change_24h": "-1.5"}
            else:
                r.json = lambda: {"products": [Scan.prod("AAA", 1, 5, 5e6)]}
            return r
        return get

    def test_requests_and_parsing(self):
        import unittest.mock as m
        calls = []
        with m.patch.object(bot.requests, "get", self.fake_get(calls)):
            px = bot.fetch_prices(["SOL"])
            cs = bot.fetch_candles("SOL", "4h", 10)
            prods = bot.fetch_products()
        self.assertEqual(px["SOL"], {"p": 120.5, "ch": -0.015})
        self.assertEqual([c["t"] for c in cs], [10, 20])
        self.assertTrue(any(u.endswith("/products/SOL-USD") for u, _ in calls))
        cu, cp = next(x for x in calls if x[0].endswith("/candles"))
        self.assertEqual(cp["granularity"], "FOUR_HOUR")
        self.assertEqual(int(cp["end"]) - int(cp["start"]), 14400 * 11)
        self.assertEqual(len(prods), 1)

    def test_bad_data_does_not_crash(self):
        import unittest.mock as m
        with m.patch.object(bot.requests, "get", side_effect=RuntimeError("down")), m.patch.object(bot.time, "sleep"):
            self.assertEqual(bot.fetch_prices(["SOL"]), {})
            self.assertEqual(bot.fetch_candles("SOL", "1h", 10), [])


class Parse(unittest.TestCase):
    def test_extract_json_handles_fences(self):
        self.assertEqual(bot.extract_json('```json\n{"a": 1}\n```')["a"], 1)

    def test_apply_ignores_bad_actions(self):
        cfg, e = make()
        b = bot.Brain(cfg, e.db, e)
        out = b.apply([{"type": "open", "ticker": "DOGEX", "mode": "swing", "entry": 1, "stop": 0.9, "t1": 1.2},
                       {"type": "adjust_stop", "position_id": 999, "new_stop": 5}], {"BTC": {"p": 100, "ch": 0}})
        self.assertTrue(all("opened" not in o for o in out))


class Remote(unittest.TestCase):
    def test_apply_valid_and_invalid(self):
        cfg, e = make()
        ch = bot.apply_remote(cfg, {"watchlist": ["btc", "index", "up", "orbio"], "min_rr": 2, "paused": True,
                                    "swing_stop_pct": [0.02, 0.1], "dashboard_token": "x", "claude_minutes": "soon",
                                    "max_open_positions": -1})
        self.assertEqual(cfg.watchlist, ["BTC", "INDEX", "UP", "ORBIO"])
        self.assertEqual(cfg.min_rr, 2)
        self.assertTrue(cfg.paused)
        self.assertEqual(cfg.swing_stop_pct, (0.02, 0.1))
        self.assertEqual(cfg.dashboard_token, "")          # not remotely settable
        self.assertEqual(cfg.claude_minutes, 30)            # bad type skipped
        self.assertEqual(cfg.max_open_positions, 6)         # negative skipped
        self.assertTrue(len(ch) >= 4)

    def test_pause_blocks_entries(self):
        cfg, e = make()
        cfg.paused = True
        ok, why, _ = e.evaluate(setup(ticker="SOL", stop=90, t1=120), 100)
        self.assertFalse(ok)
        self.assertIn("paused", why)

    def test_network_failure_keeps_settings(self):
        import unittest.mock as m
        cfg, e = make()
        with m.patch.object(bot.requests, "get", side_effect=RuntimeError("down")):
            bot.sync_remote(cfg, force=True)
        self.assertEqual(cfg.watchlist, bot.Cfg.watchlist)


class Books(unittest.TestCase):
    def test_meme_list_always_scanned(self):
        cfg, e = make()
        prods = [{"base_currency_id": b, "quote_currency_id": "USD", "price": "1", "price_percentage_change_24h": "0",
                  "approximate_quote_24h_volume": str(v), "status": "online"} for b, v in
                 (("PEPE", 5e6), ("WIF", 9e6), ("BONK", 2e6), ("ZZZ", 1e6))]
        cfg.scan_top_gainers = cfg.scan_top_losers = cfg.scan_top_volume = 0
        cfg.scan_meme_picks = 2
        cands, _ = bot.screen_universe(prods, cfg)
        self.assertEqual(sorted(c["ticker"] for c in cands), ["PEPE", "WIF"])     # the two most-traded memes
        self.assertTrue(all(c["why"] == "meme list" for c in cands))

    def test_section_off_is_rejected(self):
        cfg, e = make()
        cfg.books["scalp"]["enabled"] = False
        ok, why, _ = e.evaluate(setup(mode="scalp", stop=98, t1=104), 100)
        self.assertFalse(ok)
        self.assertIn("off", why)
        self.assertFalse(e.evaluate(setup(mode="hold", stop=80, t1=140), 100)[0])    # hold is off by default

    def test_meme_section_caps_and_stops(self):
        cfg, e = make()
        ok, why, usd = e.evaluate(setup(ticker="PEPE", mode="meme", stop=90, t1=120), 100)
        self.assertTrue(ok, why)
        self.assertLessEqual(usd, 300)
        self.assertFalse(e.evaluate(setup(ticker="PEPE", mode="meme", stop=99.5, t1=101), 100)[0])   # stop under 3%
        self.assertFalse(e.evaluate(setup(ticker="PEPE", mode="meme", stop=70, t1=160), 100)[0])     # stop over 20%

    def test_meme_max_open(self):
        cfg, e = make()
        cfg.books["meme"]["max_open"] = 1
        self.assertIsNotNone(e.open_position(setup(ticker="PEPE", mode="meme", stop=90, t1=120), 100)[0])
        ok, why, _ = e.evaluate(setup(ticker="WIF", mode="meme", stop=90, t1=120), 100)
        self.assertFalse(ok)
        self.assertIn("max open", why)

    def test_hold_allocation_cap(self):
        cfg, e = make()
        cfg.books["hold"]["enabled"] = True
        cfg.books["hold"]["alloc_pct"] = 0.05           # $250 of a $5,000 account
        ok, why, usd = e.evaluate(setup(ticker="ETH", mode="hold", stop=80, t1=150), 100)
        self.assertTrue(ok, why)
        self.assertLessEqual(usd, 250.01)

    def test_time_stop_closes_and_journals(self):
        cfg, e = make()
        cfg.books["meme"]["max_hold_hours"] = 1
        pid, _ = e.open_position(setup(ticker="PEPE", mode="meme", stop=90, t1=120), 100)
        e.db.x("UPDATE positions SET opened_at='2000-01-01T00:00:00+00:00' WHERE id=?", pid)
        e.tick({"PEPE": {"p": 101}})
        p = e.db.one("SELECT * FROM positions WHERE id=?", pid)
        self.assertEqual(p["exit_reason"], "time stop")
        j = e.db.one("SELECT * FROM journal WHERE pos_id=?", pid)
        self.assertEqual(j["book"], "meme")
        self.assertIn("Time stop", j["auto_lesson"])

    def test_journal_records_stop_and_extremes(self):
        cfg, e = make()
        pid, _ = e.open_position(setup(stop=90, t1=120, t2=130), 100)
        e.tick({"BTC": {"p": 112}})
        e.tick({"BTC": {"p": 89}})
        j = e.db.one("SELECT * FROM journal WHERE pos_id=?", pid)
        self.assertAlmostEqual(j["mfe_pct"], 0.12, places=3)
        self.assertLess(j["ret_pct"], 0)
        self.assertIsNotNone(j["r_mult"])
        self.assertIn("Stopped out", j["auto_lesson"])
        snap = e.snapshot({"BTC": {"p": 100}})
        self.assertEqual(snap["journal"][0]["pos_id"], pid)

    def test_claude_lessons_and_meme_reroute(self):
        cfg, e = make()
        b = bot.Brain(cfg, e.db, e)
        b.allowed = {"PEPE"}                          # as if the scanner had picked it
        out = b.apply([{"type": "open", "ticker": "PEPE", "mode": "swing", "entry": 100, "stop": 90, "t1": 120,
                        "thesis": "x"}], {"PEPE": {"p": 100, "ch": 0}})
        self.assertTrue(any("opened" in o for o in out), out)
        self.assertEqual(e.open_positions()[0]["mode"], "meme")      # meme coins always go to the meme section

    def test_benchmark_tracks_btc(self):
        cfg, e = make()
        e.sample_bench({"BTC": {"p": 100, "ch": 0}})
        e.db.put("bench_last", 0)
        e.sample_bench({"BTC": {"p": 110, "ch": 0}})
        bm = e.benchmark()
        self.assertAlmostEqual(bm["btc_hold"], cfg.start_balance * 1.1)
        self.assertAlmostEqual(bm["bot"], cfg.start_balance)

    def test_review_without_trades_makes_no_call(self):
        cfg, e = make()
        b = bot.Brain(cfg, e.db, e)
        b.review()                                    # first run only starts the clock
        e.db.put("review_last", 0)
        b.review()
        r = e.snapshot({})["review"]
        self.assertIn("nothing to grade", r["headline"])

    def test_remote_books_validation(self):
        cfg, e = make()
        ch = bot.apply_remote(cfg, {"books": {"scalp": {"enabled": False}, "meme": {"risk_pct": 0.9, "max_open": 2,
                                                                                    "stop": [0.05, 0.25]},
                                              "bogus": {"enabled": True}, "swing": {"nonsense": 1}}})
        self.assertFalse(cfg.books["scalp"]["enabled"])
        self.assertEqual(cfg.books["meme"]["risk_pct"], 0.005)       # 90% risk refused
        self.assertEqual(cfg.books["meme"]["max_open"], 2)
        self.assertEqual(cfg.books["meme"]["stop"], [0.05, 0.25])
        self.assertNotIn("nonsense", cfg.books["swing"])
        self.assertEqual(bot.Cfg.books["scalp"]["enabled"], True)    # class defaults untouched
        self.assertTrue(ch)


class Chat(unittest.TestCase):
    def _server(self, chat):
        import socket, json, threading, urllib.request
        cfg, e = make()
        cfg.dashboard_port = socket.socket().getsockname()[1] if False else 0
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        cfg.dashboard_port = port
        cfg.state_path = os.path.join(os.path.dirname(cfg.db_path), "state.json")
        with open(cfg.state_path, "w") as f:
            json.dump(e.snapshot({}), f, default=str)
        srv = bot.start_dashboard(cfg, chat=chat)
        return cfg, srv, port

    def _post(self, port, path, body, headers=None):
        import json, urllib.request, urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers=headers or {})
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, None

    def test_post_needs_header_and_returns_reply(self):
        cfg, srv, port = self._server(lambda m, h: "echo: " + m)
        try:
            self.assertEqual(self._post(port, "/chat", {"message": "hi"})[0], 403)          # no X-PB header
            code, j = self._post(port, "/chat", {"message": "hi"}, {"X-PB": "1"})
            self.assertEqual((code, j["reply"]), (200, "echo: hi"))
            self.assertEqual(self._post(port, "/chat", {"message": "  "}, {"X-PB": "1"})[0], 400)
        finally:
            srv.shutdown()

    def test_pause_button_creates_and_removes_file(self):
        cfg, srv, port = self._server(None)
        try:
            os.chdir(os.path.dirname(cfg.db_path))
            self.assertEqual(self._post(port, "/pause", {"on": True}, {"X-PB": "1"})[1], {"paused": True})
            self.assertTrue(os.path.exists(bot.PAUSE_FILE))
            self.assertEqual(self._post(port, "/pause", {"on": False}, {"X-PB": "1"})[1], {"paused": False})
            self.assertFalse(os.path.exists(bot.PAUSE_FILE))
        finally:
            srv.shutdown()

    def test_chat_uses_state_history_and_daily_limit(self):
        import json, sys, types
        cfg, e = make()
        cfg.state_path = os.path.join(os.path.dirname(cfg.db_path), "state.json")
        with open(cfg.state_path, "w") as f:
            json.dump(e.snapshot({}), f, default=str)
        seen = []

        class Msgs:
            def create(self, **kw):
                seen.append(kw)
                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="answer")])
        fake = types.ModuleType("anthropic"); fake.Anthropic = lambda: types.SimpleNamespace(messages=Msgs())
        old = sys.modules.get("anthropic"); sys.modules["anthropic"] = fake
        try:
            cfg.max_chat_messages_per_day = 2
            c = bot.make_chat(cfg)
            hist = [{"role": "assistant", "text": "stray"}, {"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}]
            self.assertEqual(c("how are we doing?", hist), "answer")
            msgs = seen[0]["messages"]
            self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])     # starts with a user turn
            self.assertIn("BOT STATE", msgs[-1]["content"])
            self.assertIn("how are we doing?", msgs[-1]["content"])
            c("two", [])
            with self.assertRaises(RuntimeError):
                c("three", [])
        finally:
            if old is None: sys.modules.pop("anthropic", None)
            else: sys.modules["anthropic"] = old


class Fluid(unittest.TestCase):
    def test_cancel_order_and_position_view(self):
        from unittest import mock
        cfg, e = make()
        cfg.scan_universe = False
        b = bot.Brain(cfg, e.db, e)
        e.db.x("INSERT INTO pending(ticker,mode,limit_price,stop,t1,t2,thesis,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
               "HYPE", "swing", 90, 85, 100, None, "old idea", bot.now_iso(), "2999-01-01T00:00:00+00:00")
        e.open_position(setup(ticker="SOL", stop=95, t1=110), 100)
        candles = [{"t": i, "o": 100, "h": 101, "l": 99, "c": 100} for i in range(60)]
        with mock.patch.object(bot, "fetch_candles", return_value=candles):
            ctx = b.build_context({"SOL": {"p": 103, "ch": 0}, "HYPE": {"p": 95, "ch": 0}})
        pos = ctx["open_positions"][0]
        self.assertEqual((pos["ticker"], pos["pnl_pct"]), ("SOL", 3.0))
        self.assertIn("age_hours", pos)
        self.assertEqual(pos["thesis"], "t")
        self.assertEqual(ctx["pending_orders"][0]["now_price"], 95)
        out = b.apply([{"type": "cancel_order", "ticker": "HYPE", "why": "stale"},
                       {"type": "cancel_order", "ticker": "NOPE"}], {})
        self.assertEqual(out, ["HYPE waiting order cancelled", "no waiting order for NOPE"])
        self.assertEqual(e.db.one("SELECT status FROM pending")["status"], "cancelled")

    def test_scalp_and_hold_get_their_timeframes(self):
        from unittest import mock
        cfg, e = make()
        cfg.scan_universe = False
        cfg.books["scalp"]["enabled"] = True
        cfg.books["hold"]["enabled"] = True
        b = bot.Brain(cfg, e.db, e)
        asked = []
        def fake(t, tf, n=60):
            asked.append(tf)
            return [{"t": i, "o": 100, "h": 101, "l": 99, "c": 100} for i in range(max(n, 60))]
        with mock.patch.object(bot, "fetch_candles", side_effect=fake):
            ctx = b.build_context({"BTC": {"p": 100, "ch": 0}})
        m = ctx["markets"]["BTC"]
        self.assertEqual(len(m["candles_15m_ohlc"]), 32)
        self.assertEqual(len(m["candles_1d_ohlc"]), 30)
        self.assertIn("ema50_1d", m)
        cfg.books["scalp"]["enabled"] = cfg.books["hold"]["enabled"] = False
        with mock.patch.object(bot, "fetch_candles", side_effect=fake):
            m = b.build_context({"BTC": {"p": 100, "ch": 0}})["markets"]["BTC"]
        self.assertNotIn("candles_15m_ohlc", m)
        self.assertNotIn("candles_1d_ohlc", m)

    def test_prompt_carries_the_mindset(self):
        self.assertIn("not married to any idea", bot.SYSTEM)
        self.assertIn("cancel_order", bot.SYSTEM)


def _pair(addr, sym, price, liq, vol=1000, chain="robinhood", h24=13.0, name="Memory cow Moo"):
    return {"chainId": chain, "dexId": "uniswap", "pairAddress": "0xpool" + str(liq),
            "baseToken": {"address": addr, "symbol": sym, "name": name}, "quoteToken": {"address": "0xq", "symbol": "WETH"},
            "priceUsd": str(price), "priceChange": {"h24": h24}, "volume": {"h24": vol}, "liquidity": {"usd": liq}}


MOO = "0xD9db30bb0d2b8d2eae3826a1372117e058791e18"


class Dex(unittest.TestCase):
    def setUp(self):
        self.cfg, self.e = make()
        self.cfg.dex_tokens = [{"ticker": "MOO", "address": MOO, "note": ""}]
        bot.set_dex(self.cfg)
        bot._DEX["meta"].clear()
        bot._DEX["last_tick"].clear()

    def tearDown(self):
        bot._DEX["map"] = {}

    def test_best_pool_and_quote_side_ignored(self):
        import unittest.mock as m
        resp = {"pairs": [_pair(MOO.lower(), "MOO", 0.0067, 800000), _pair(MOO.lower(), "MOO", 0.0090, 500),
                          _pair("0xother", "AI", 1.0, 9999999)]}
        with m.patch.object(bot, "ds_get", return_value=resp):
            px = bot.fetch_dex_prices(self.cfg.dex_map())
        self.assertEqual(list(px), ["MOO"])
        self.assertAlmostEqual(px["MOO"]["p"], 0.0067)
        self.assertAlmostEqual(px["MOO"]["ch"], 0.13)
        self.assertEqual(bot._DEX["meta"]["MOO"]["liq"], 800000)

    def test_fetch_prices_mixes_coinbase_and_dex(self):
        import unittest.mock as m
        cb = {"price": "100", "price_percentage_change_24h": "2", "approximate_quote_24h_volume": "5"}
        with m.patch.object(bot, "cb_get", return_value=cb), m.patch.object(bot, "ds_get", return_value={"pairs": [_pair(MOO.lower(), "MOO", 0.0067, 800000)]}):
            px = bot.fetch_prices(["BTC", "MOO"])
        self.assertEqual(sorted(px), ["BTC", "MOO"])
        self.assertAlmostEqual(px["BTC"]["p"], 100)

    def test_ticks_become_candles(self):
        import time
        bot.record_ticks({"MOO": {"p": 1.0, "ch": 0}})
        bot._DEX["last_tick"].clear()
        bot.record_ticks({"MOO": {"p": 1.2, "ch": 0}})
        bot._DEX["last_tick"].clear()
        bot.record_ticks({"MOO": {"p": 0.9, "ch": 0}})
        bars = bot.fetch_candles("MOO", "15m", 10)
        self.assertEqual(len(bars), 1)
        self.assertEqual((bars[0]["o"], bars[0]["h"], bars[0]["l"], bars[0]["c"]), (1.0, 1.2, 0.9, 0.9))
        bot.record_ticks({"MOO": {"p": 5.0, "ch": 0}})                 # too soon: skipped
        self.assertEqual(bot.fetch_candles("MOO", "15m", 10)[0]["c"], 0.9)

    def test_thin_pool_refused_and_size_capped(self):
        s = setup(ticker="MOO", mode="meme", stop=90, t1=120)
        bot._DEX["meta"]["MOO"] = {"liq": 10000}
        ok, why, _ = self.e.evaluate(s, 100)
        self.assertFalse(ok)
        self.assertIn("liquidity", why)
        bot._DEX["meta"]["MOO"] = {"liq": 100000}
        ok, why, usd = self.e.evaluate(s, 100)
        self.assertTrue(ok, why)
        self.assertLessEqual(usd, 2000.01)                              # 2% of a $100k pool

    def test_dex_costs_apply(self):
        self.cfg.dex_fee_pct, self.cfg.dex_slippage_pct = 0.003, 0.01
        bot._DEX["meta"]["MOO"] = {"liq": 1000000}
        pid, why = self.e.open_position(setup(ticker="MOO", mode="meme", stop=90, t1=120), 100)
        p = self.e.db.one("SELECT * FROM positions WHERE id=?", pid)
        self.assertAlmostEqual(p["entry"], 101.0)                       # 1% slippage in
        self.assertLess(p["realized"], 0)                               # 0.3% fee booked
        self.assertAlmostEqual(self.e.costs("BTC")[1], 0.0)             # Coinbase coins keep Coinbase costs (make() zeroes them)

    def test_dex_token_trades_in_meme_section(self):
        bot._DEX["meta"]["MOO"] = {"liq": 1000000}
        b = bot.Brain(self.cfg, self.e.db, self.e)
        out = b.apply([{"type": "open", "ticker": "MOO", "mode": "swing", "entry": 100, "stop": 90, "t1": 120, "thesis": "x"}],
                      {"MOO": {"p": 100, "ch": 0}})
        self.assertTrue(any("opened" in o for o in out), out)
        self.assertEqual(self.e.open_positions()[0]["mode"], "meme")

    def test_search_both_sources(self):
        import unittest.mock as m
        prods = [{"base_currency_id": "MOO", "base_name": "Moo Deng", "quote_currency_id": "USD", "price": "0.2",
                  "price_percentage_change_24h": "5", "approximate_quote_24h_volume": "900000"},
                 {"base_currency_id": "BTC", "base_name": "Bitcoin", "quote_currency_id": "USD", "price": "1"}]
        pairs = {"pairs": [_pair(MOO.lower(), "MOO", 0.0067, 800000), _pair("0xsmall", "MOO", 0.5, 300, name="Moo Virus")]}
        bot._prod_cache.update(t=0.0, rows=[])
        with m.patch.object(bot, "fetch_products", return_value=prods), m.patch.object(bot, "ds_get", return_value=pairs):
            r = bot.search_coins("moo")
        self.assertEqual([c["ticker"] for c in r["coinbase"]], ["MOO"])
        self.assertEqual(r["dex"][0]["liquidity_usd"], 800000)           # deepest pool first
        self.assertEqual(r["dex"][0]["address"], MOO.lower())
        self.assertEqual(len(r["dex"]), 2)
        with m.patch.object(bot, "fetch_products", side_effect=RuntimeError("down")), m.patch.object(bot, "ds_get", return_value=pairs):
            bot._prod_cache.update(t=0.0)
            r = bot.search_coins("moo")
        self.assertTrue(r["errors"] and r["dex"])                        # one source failing doesn't hide the other

    def test_remote_dex_tokens_validation(self):
        bot.apply_remote(self.cfg, {"dex_tokens": [{"ticker": "cow", "address": MOO}, {"ticker": "BAD!", "address": "x"}]})
        self.assertEqual([d["ticker"] for d in self.cfg.dex_tokens], ["MOO"])         # whole list refused if any entry is bad
        bot.apply_remote(self.cfg, {"dex_tokens": [{"ticker": "cow", "address": MOO}]})
        self.assertEqual(self.cfg.dex_tokens[0]["ticker"], "COW")
        self.assertIn("COW", bot._DEX["map"])
        self.assertIn("COW", self.cfg.core())

    def test_search_endpoint(self):
        import json, socket, urllib.request, unittest.mock as m
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.cfg.dashboard_port = port
        srv = bot.start_dashboard(self.cfg)
        try:
            with m.patch.object(bot, "search_coins", return_value={"query": "moo", "coinbase": [], "dex": [], "errors": []}):
                r = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/search?q=moo", timeout=5).read())
            self.assertEqual(r["query"], "moo")
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/search?q=%3Cscript%3E", timeout=5)
        finally:
            srv.shutdown()



class Ntfy(unittest.TestCase):
    def test_topic_validation_and_send(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot_fresh", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"))
        B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)
        c = B.Cfg()
        self.assertEqual(B.apply_remote(c, {"ntfy_topic": "bad"}), [])
        self.assertEqual(c.ntfy_topic, "")
        B.apply_remote(c, {"ntfy_topic": "adrian-paperbot-test1"})
        self.assertEqual(c.ntfy_topic, "adrian-paperbot-test1")
        sent = []
        orig = B.requests.post
        B.requests.post = lambda url, **k: sent.append((url, k))
        try:
            B.notify(c, "hi")
        finally:
            B.requests.post = orig
        self.assertEqual(sent[0][0], "https://ntfy.sh/adrian-paperbot-test1")



class Gecko(unittest.TestCase):
    def test_search_merges_coingecko(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot_cg", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"))
        B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)
        calls = []

        def fake(path, params=None):
            calls.append(path)
            if path == "/search":
                return {"coins": [{"id": "lighter", "symbol": "lit", "name": "Lighter", "market_cap_rank": 90}]}
            if path == "/simple/price":
                return {"lighter": {"usd": 2.5, "usd_24h_change": 4.0}}
            return {"platforms": {"ethereum": "0x232CE3bd40fCd6f80f3d55A522d03f25Df784Ee2", "": ""}}
        B.cg_get = fake
        rows = B.cg_search("lighter")
        self.assertEqual(rows[0]["ticker"], "LIT")
        self.assertAlmostEqual(rows[0]["change"], 0.04)
        self.assertEqual(rows[0]["chains"][0]["chain"], "ethereum")
        B.cg_get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rate limited"))
        B._prod_cache.update(t=B.time.time(), rows=[])
        B.ds_get = lambda *a, **k: {}
        res = B.search_coins("lighter")
        self.assertEqual(res["coingecko"], [])
        self.assertTrue(any("CoinGecko" in e for e in res["errors"]))



class Discover(unittest.TestCase):
    def test_dex_discover_filters_and_registers(self):
        import importlib.util, time as _t
        spec = importlib.util.spec_from_file_location("bot_dd", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"))
        B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)
        cfg = B.Cfg()
        old = (_t.time() - 5 * 86400) * 1000
        def pair(sym, addr, liq, vol, ch, created=old, chain="solana"):
            return {"chainId": chain, "baseToken": {"symbol": sym, "name": sym, "address": addr}, "priceUsd": "0.5",
                    "liquidity": {"usd": liq}, "volume": {"h24": vol}, "priceChange": {"h1": 1, "h6": 2, "h24": ch},
                    "txns": {"h1": {"buys": 10, "sells": 5}}, "pairCreatedAt": created, "pairAddress": "p" + sym, "dexId": "x"}
        a1, a2, a3, a4, a5 = ["A" * 30, "B" * 30, "C" * 30, "D" * 30, "E" * 30]
        def fake(path, params=None, tries=2):
            if "boosts" in path or "profiles" in path:
                return [{"chainId": "solana", "tokenAddress": a} for a in (a1, a2, a3, a4, a5)]
            return {"pairs": [pair("GOOD", a1, 400000, 900000, 120), pair("THIN", a2, 10000, 900000, 300),
                              pair("QUIET", a3, 400000, 500, 50), pair("NEW", a4, 400000, 900000, 80, created=_t.time() * 1000),
                              pair("BTC", a5, 400000, 900000, 5)]}
        B.ds_get = fake
        out = B.dex_discover(cfg, taken={"BTC"})
        self.assertEqual([r["ticker"] for r in out], ["GOOD"])
        self.assertEqual(B._DEX["meta"]["GOOD"]["liq"], 400000)
        cfg.dex_scan = {"GOOD": {"ticker": "GOOD", "address": a1, "note": "scanned"}}
        self.assertIn("GOOD", cfg.dex_map())



class News(unittest.TestCase):
    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot_news", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"))
        B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)
        return B

    def test_parse_rss_and_atom(self):
        B = self.load()
        rss = ("<rss><channel><item><title>Zcash ETF tops $1 billion</title><link>https://x.test/a</link>"
               "<pubDate>Wed, 07 Oct 2026 18:37:17 +0000</pubDate><description>&lt;p&gt;ZEC rallies&lt;/p&gt;</description></item></channel></rss>")
        atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Solana outage</title><link href="https://x.test/b"/>'
                "<updated>2026-10-07T10:00:00Z</updated><summary>SOL halted</summary></entry></feed>")
        a, b = B.parse_feed(rss, "x.test"), B.parse_feed(atom, "y.test")
        self.assertEqual(a[0]["title"], "Zcash ETF tops $1 billion")
        self.assertEqual(a[0]["summary"], "ZEC rallies")
        self.assertGreater(a[0]["ts"], 0)
        self.assertEqual(b[0]["link"], "https://x.test/b")
        self.assertGreater(b[0]["ts"], 0)

    def test_matching_avoids_false_hits(self):
        B = self.load()
        h = lambda t: {"title": t, "summary": ""}
        self.assertTrue(B.headline_matches(h("Zcash ETF tops $1 billion"), "ZEC", ["zcash"]))
        self.assertTrue(B.headline_matches(h("Why $UP is moving"), "UP", []))
        self.assertFalse(B.headline_matches(h("Prices go up as markets open"), "UP", []))
        self.assertFalse(B.headline_matches(h("Artificial intelligence boom"), "AI", []))
        self.assertTrue(B.headline_matches(h("Hackers drain LINK bridge"), "LINK", ["chainlink"]))
        self.assertFalse(B.headline_matches(h("A new link between rates and bitcoin"), "LINK", ["chainlink"]))

    def test_context_and_cache(self):
        B = self.load()
        import time as _t
        cfg = B.Cfg()
        now = _t.time()
        items = [{"title": "Zcash ETF tops $1 billion", "summary": "", "link": "", "ts": now - 3600, "source": "a"},
                 {"title": "Fed holds rates", "summary": "", "link": "", "ts": now - 7200, "source": "b"}]
        per, gen = B.news_context(cfg, {"ZEC": ["zcash"], "BTC": ["bitcoin"]}, items)
        self.assertEqual(list(per), ["ZEC"])
        self.assertEqual(len(gen), 2)
        self.assertEqual(per["ZEC"][0]["age_hours"], 1.0)



class Reentry(unittest.TestCase):
    def test_blocked_after_stop(self):
        cfg, eng = make()
        cfg.reentry_cooldown_hours = 6
        s = {"ticker": "SOL", "mode": "swing", "entry": 100.0, "stop": 95.0, "t1": 110.0, "t2": 120.0, "thesis": "x"}
        ok, why, usd = eng.evaluate(s, 100.0)
        self.assertTrue(ok, why)
        eng.open_position(s, 100.0)
        p = eng.open_positions()[0]
        eng.close_position(p, 95.0, "stop hit")
        ok, why, _ = eng.evaluate(s, 100.0)
        self.assertFalse(ok)
        self.assertIn("stopped out", why)
        cfg.reentry_cooldown_hours = 0
        self.assertTrue(eng.evaluate(s, 100.0)[0])



def cdl(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


class Gold(unittest.TestCase):
    def cfg_eng(self):
        cfg, e = make()
        cfg.ifvg_fee_pct = cfg.ifvg_slip_pct = 0.0
        return cfg, e

    def candles(self, base_t=0):
        # 20 flat candles, then a bearish gap forms (cand 20-22), then a candle closes above it = bullish IFVG
        cs = [cdl(base_t + i * 900, 100, 100.4, 99.6, 100) for i in range(20)]
        cs += [cdl(base_t + 20 * 900, 100, 100.2, 99.0, 99.2),
               cdl(base_t + 21 * 900, 99.2, 99.3, 98.0, 98.2),
               cdl(base_t + 22 * 900, 98.2, 98.7, 97.9, 98.5),   # prev low 99.0 > this high 98.7 -> bear gap 98.7-99.0
               cdl(base_t + 23 * 900, 98.5, 99.4, 98.4, 99.3)]   # closes above 99.0 -> bullish IFVG
        return cs

    def test_detects_first_bullish_ifvg(self):
        cs = self.candles()
        g = bot.first_ifvg(cs, 0, 0.1)
        self.assertEqual(g["side"], "long")
        self.assertEqual(g["j"], 23)

    def test_bearish_ifvg_is_short(self):
        cs = [cdl(i * 900, 100, 100.4, 99.6, 100) for i in range(20)]
        cs += [cdl(20 * 900, 100, 101.0, 99.8, 100.9), cdl(21 * 900, 100.9, 102.0, 100.8, 101.8),
               cdl(22 * 900, 101.8, 102.1, 101.2, 101.4),        # gap 100.? check: c20 high 101.0 < c22 low 101.2
               cdl(23 * 900, 101.4, 101.5, 100.4, 100.6)]        # closes below 101.0 -> bearish IFVG
        g = bot.first_ifvg(cs, 0, 0.1)
        self.assertEqual(g["side"], "short")

    def test_ignores_inversions_before_session(self):
        self.assertIsNone(bot.first_ifvg(self.candles(), 24 * 900, 0.1))

    def test_setup_is_one_to_one_with_breakeven_at_68pct(self):
        cfg, e = self.cfg_eng()
        cs = self.candles()
        plan, why = bot.ifvg_setup(cfg, cs, 99.3, 0)
        self.assertEqual(why, "ok")
        r = plan["risk"]
        self.assertAlmostEqual(plan["tp"] - plan["entry"], r)
        self.assertAlmostEqual(plan["be"] - plan["entry"], 0.68 * r)

    def test_long_flow_breakeven_then_stop_scratch(self):
        cfg, e = self.cfg_eng()
        plan = {"side": "long", "entry": 100, "stop": 99, "sig": {"lo": 99, "hi": 99.5}}
        pid, why = e.open_ifvg(plan, 100)
        self.assertTrue(pid, why)
        p = e.open_positions()[0]
        self.assertAlmostEqual(p["t1"], 101)
        self.assertAlmostEqual(p["be_at"], 100.68)
        self.assertAlmostEqual(p["qty"], 50.0)        # $50 risk / $1
        e.tick({"PAXG": {"p": 100.7}})
        p = e.open_positions()[0]
        self.assertEqual(p["stop"], 100)              # moved to entry
        e.tick({"PAXG": {"p": 99.9}})
        self.assertEqual(len(e.open_positions()), 0)

    def test_short_wins_at_target(self):
        cfg, e = self.cfg_eng()
        plan = {"side": "short", "entry": 100, "stop": 101, "sig": {"lo": 99.5, "hi": 101}}
        pid, why = e.open_ifvg(plan, 100)
        self.assertTrue(pid, why)
        e.tick({"PAXG": {"p": 99.0}})
        self.assertEqual(len(e.open_positions()), 0)
        self.assertAlmostEqual(e.balance(), 5050.0, places=3)
        self.assertGreater(e.stats()["avg_r"], 0.99)

    def test_short_loses_at_stop(self):
        cfg, e = self.cfg_eng()
        e.open_ifvg({"side": "short", "entry": 100, "stop": 101, "sig": {"lo": 1, "hi": 2}}, 100)
        e.tick({"PAXG": {"p": 101.0}})
        self.assertAlmostEqual(e.balance(), 4950.0, places=3)

    def test_one_gold_trade_at_a_time_and_pause(self):
        cfg, e = self.cfg_eng()
        plan = {"side": "long", "entry": 100, "stop": 99, "sig": {"lo": 1, "hi": 2}}
        e.open_ifvg(plan, 100)
        self.assertIsNone(e.open_ifvg(plan, 100)[0])
        cfg.paused = True
        self.assertEqual(e.open_ifvg(plan, 100)[1], "paused")

    def test_session_window(self):
        from datetime import datetime, timezone
        cfg = bot.Cfg()
        mon_9pm = datetime(2026, 10, 12, 21, 0, tzinfo=bot.ET).astimezone(timezone.utc)
        self.assertIsNotNone(bot.session_window(mon_9pm, cfg))
        tue_1am = datetime(2026, 10, 13, 1, 0, tzinfo=bot.ET).astimezone(timezone.utc)
        self.assertEqual(bot.session_window(tue_1am, cfg), bot.session_window(mon_9pm, cfg))
        noon = datetime(2026, 10, 12, 12, 0, tzinfo=bot.ET).astimezone(timezone.utc)
        self.assertIsNone(bot.session_window(noon, cfg))
        fri_9pm = datetime(2026, 10, 16, 21, 0, tzinfo=bot.ET).astimezone(timezone.utc)
        self.assertIsNone(bot.session_window(fri_9pm, cfg))

if __name__ == "__main__":
    unittest.main(verbosity=2)

