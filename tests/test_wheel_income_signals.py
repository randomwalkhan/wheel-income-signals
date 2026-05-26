import datetime as dt
import tempfile
import unittest
from pathlib import Path

import wheel_income_signals as wis


class WheelIncomeSignalTests(unittest.TestCase):
    def test_parse_occ_symbol(self):
        parsed = wis.parse_occ_symbol("O:QQQ260626P00670000")
        self.assertEqual(parsed["root"], "QQQ")
        self.assertEqual(parsed["expiration"], dt.date(2026, 6, 26))
        self.assertEqual(parsed["option_type"], "put")
        self.assertEqual(parsed["strike"], 670.0)
        self.assertEqual(
            wis.occ_symbol("QQQ", dt.date(2026, 6, 26), "put", 670.0),
            "QQQ260626P00670000",
        )

    def test_black_scholes_and_implied_volatility(self):
        price = wis.black_scholes_price("put", 100, 95, 0.04, 0.30, 30 / 365)
        iv = wis.implied_volatility("put", price, 100, 95, 0.04, 30 / 365)
        greeks = wis.black_scholes_greeks("put", 100, 95, 0.04, iv, 30 / 365)
        self.assertAlmostEqual(iv, 0.30, places=3)
        self.assertLess(greeks["delta"], 0)
        self.assertGreater(greeks["prob_itm"], 0)
        self.assertLess(greeks["prob_itm"], 1)

    def test_liquidity_flags(self):
        engine = wis.StrategyEngine(wis.StrategyConfig(max_spread_pct=0.05, min_open_interest=100))
        quote = wis.OptionQuote(
            underlying="SPY",
            contract_symbol="SPY260626P00450000",
            option_type="put",
            expiration=dt.date(2026, 6, 26),
            strike=450,
            bid=1.0,
            ask=1.2,
            open_interest=10,
            volume=0,
        )
        flags = engine.liquidity_flags(quote)
        self.assertIn("wide_spread", flags)
        self.assertIn("low_open_interest", flags)
        self.assertIn("low_volume", flags)

    def test_assignment_cost_basis_accounting(self):
        strike = 100.0
        premium = 2.0
        fee = 0.65
        premium_cash = premium * wis.CONTRACT_SIZE - fee
        cost_basis = strike - premium_cash / wis.CONTRACT_SIZE
        self.assertAlmostEqual(cost_basis, 98.0065)

    def test_cc_selection_never_below_cost_basis(self):
        cfg = wis.StrategyConfig(min_open_interest=0, min_volume=0, min_iv_hv_ratio=0.0)
        engine = wis.StrategyEngine(cfg)
        as_of = dt.date(2026, 5, 26)
        history = [
            wis.DailyBar(as_of - dt.timedelta(days=day), 100, 101, 99, 100 + day * 0.01, 1000000)
            for day in range(100, 0, -1)
        ]
        quotes = []
        for strike in [95, 100, 105, 110]:
            q = wis.OptionQuote(
                underlying="SPY",
                contract_symbol=wis.occ_symbol("SPY", dt.date(2026, 6, 26), "call", strike),
                option_type="call",
                expiration=dt.date(2026, 6, 26),
                strike=strike,
                bid=1.0,
                ask=1.02,
                iv=0.25,
                open_interest=1000,
                volume=100,
            )
            quotes.append(q)
        selected = engine.select_cc("SPY", as_of, 100, 105, quotes, history, strict_liquidity=True)
        self.assertTrue(selected)
        self.assertGreaterEqual(selected[0].quote.strike, 105)

    def test_polygon_snapshot_normalization(self):
        payload = {
            "details": {
                "ticker": "O:QQQ260626P00670000",
                "contract_type": "put",
                "expiration_date": "2026-06-26",
                "strike_price": 670,
            },
            "last_quote": {"bid": 5.0, "ask": 5.2},
            "greeks": {"delta": -0.25, "theta": -0.1},
            "implied_volatility": 0.24,
            "open_interest": 500,
            "day": {"volume": 100, "close": 5.1},
        }
        quote = wis.PolygonProvider.normalize_option_snapshot(payload, "QQQ")
        self.assertEqual(quote.contract_symbol, "QQQ260626P00670000")
        self.assertEqual(quote.mid, 5.1)
        self.assertEqual(quote.delta, -0.25)

    def test_theta_rows_from_array_payload(self):
        payload = {
            "header": {"format": ["date", "open", "high", "low", "close", "volume"]},
            "response": [[20260522, 1, 2, 0.5, 1.5, 1000]],
        }
        rows = wis.ThetaDataProvider.rows_from_theta(payload)
        self.assertEqual(rows[0]["date"], 20260522)
        self.assertEqual(rows[0]["close"], 1.5)

    def test_synthetic_backtest_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = wis.main(
                [
                    "backtest",
                    "--provider",
                    "synthetic",
                    "--symbols",
                    "SPY",
                    "--start",
                    "2022-01-03",
                    "--end",
                    "2022-08-31",
                    "--out-dir",
                    tmp,
                    "--target-annual-yield",
                    "0.01",
                    "--min-iv-hv-ratio",
                    "0.0",
                ]
            )
            self.assertEqual(rc, 0)
            trades = Path(tmp, "trades.csv").read_text()
            equity = Path(tmp, "equity_curve.csv").read_text()
            summary = Path(tmp, "summary.md").read_text()
            self.assertIn("CSP", trades)
            self.assertIn("equity", equity)
            self.assertIn("Wheel Backtest Summary", summary)

    def test_synthetic_signal_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = wis.main(
                [
                    "signal",
                    "--provider",
                    "synthetic",
                    "--symbols",
                    "SPY",
                    "--as-of",
                    "2026-05-26",
                    "--out-dir",
                    tmp,
                    "--target-annual-yield",
                    "0.01",
                    "--min-iv-hv-ratio",
                    "0.0",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(Path(tmp, "signals.csv").exists())

    def test_yahoo_proxy_prices_model_option_from_history(self):
        provider = wis.YahooFinanceProxyProvider()
        as_of = dt.date(2026, 5, 26)
        quote = wis.OptionQuote(
            underlying="SPY",
            contract_symbol=wis.occ_symbol("SPY", dt.date(2026, 6, 26), "put", 600),
            option_type="put",
            expiration=dt.date(2026, 6, 26),
            strike=600,
        )

        def fake_history(symbol, start, end):
            bars = []
            current = start
            while current <= end:
                if current.weekday() < 5:
                    day = (current - start).days
                    close = 620 + day * 0.05
                    bars.append(wis.DailyBar(current, close, close + 1, close - 1, close, 1000000))
                current += dt.timedelta(days=1)
            return bars

        provider.get_price_history = fake_history
        priced = provider.get_option_eod(quote, as_of)
        self.assertIsNotNone(priced)
        self.assertGreater(priced.mid, 0)
        self.assertLess(priced.delta, 0)


if __name__ == "__main__":
    unittest.main()
