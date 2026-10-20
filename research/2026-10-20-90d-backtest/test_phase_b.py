"""Phase B offline checks; run inside the FreqAI image:

docker compose -p bitget-spike -f freqtrade/docker-compose.yml \
  -f research/2026-10-20-90d-backtest/compose.freqai.yml run --rm \
  -v <this dir>:/research --entrypoint python freqai -P /research/test_phase_b.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, "/freqtrade/user_data/strategies")
sys.path.insert(0, str(Path(__file__).resolve().parent))

HEADER = "ts_utc,time_known,region,category,scheduled,importance,title_en,title_ko,source_url\n"


def write(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    return Path(handle.name)


class FakeTrade:
    def __init__(self, open_rate, is_short=False, leverage=2.0, tag="safe_long", amount=1.0, open_date="2026-01-01", fee=0.0006, stop=None, trade_id=1):
        self.open_rate, self.is_short, self.leverage, self.enter_tag = open_rate, is_short, leverage, tag
        self.amount, self.fee_open, self.fee_close, self.id = amount, fee, fee, trade_id
        self.open_date_utc = pd.Timestamp(open_date, tz="UTC")
        self.stop_loss = stop
        self.data = {}

    def get_custom_data(self, key):
        return self.data.get(key)

    def set_custom_data(self, key, value):
        self.data[key] = value


class CalendarTests(unittest.TestCase):
    def test_availability_rules(self):
        from event_impact import load_calendar
        cal = load_calendar(write(HEADER
            + "2026-01-02T00:00:00Z,false,CRYPTO,EXCHANGE_INCIDENT,false,high,a,a,u\n"
            + "2026-01-02T13:30:00Z,true,US,CPI,true,high,b,b,u\n"
            + "2026-01-05T00:00:00Z,false,JP,CENTRAL_BANK,true,medium,c,c,u\n"))
        hack = cal.loc[cal.group == "CRYPTO:EXCHANGE_INCIDENT"].iloc[0]
        self.assertEqual(hack.available, pd.Timestamp("2026-01-03T00:00:00Z"))
        self.assertEqual(hack.known_from, hack.available)
        cpi = cal.loc[cal.group == "US:CPI"].iloc[0]
        self.assertEqual(cpi.available, pd.Timestamp("2026-01-02T13:30:00Z"))
        self.assertEqual(cpi.known_from, pd.Timestamp("2025-12-19T13:30:00Z"))
        boj = cal.loc[cal.group == "JP:CENTRAL_BANK"].iloc[0]
        self.assertEqual(boj.nominal, pd.Timestamp("2026-01-05T00:00:00Z"))
        self.assertEqual(boj.available, pd.Timestamp("2026-01-06T00:00:00Z"))

    def test_legacy_loader_reads_boolean_time_known_without_lookahead(self):
        from event_features import load_events
        ev = load_events(write(HEADER + "2026-01-02T05:00:00Z,false,CRYPTO,EXCHANGE_INCIDENT,false,high,a,a,u\n"))
        self.assertEqual(ev.ts_utc.iloc[0], pd.Timestamp("2026-01-03T00:00:00Z"))
        self.assertEqual(ev.time_known.iloc[0], pd.Timestamp("2026-01-03T00:00:00Z"))


class WeightedFeatureTests(unittest.TestCase):
    def calendar(self, extra=""):
        from event_impact import load_calendar
        return load_calendar(write(HEADER
            + "2026-01-02T05:00:00Z,true,CRYPTO,EXCHANGE_INCIDENT,false,high,a,a,u\n"
            + "2026-01-03T13:30:00Z,true,US,CPI,true,high,b,b,u\n"
            + "2026-01-03T00:00:00Z,false,KR,REG_KR,false,high,c,c,u\n" + extra))

    def test_point_in_time(self):
        from event_impact import weighted_event_features
        weights = {"CRYPTO:EXCHANGE_INCIDENT": 2.0, "US:CPI": 1.5, "KR:REG_KR": 1.2}
        dates = pd.Series(pd.date_range("2026-01-01", "2026-01-05", freq="h", tz="UTC"))
        f = weighted_event_features(dates, self.calendar(), weights)
        at = lambda ts: f.loc[dates == pd.Timestamp(ts, tz="UTC")].iloc[0]  # noqa: E731
        # Hack at 05:00 is invisible on the 03:00 candle (closes 04:00), visible on the 04:00 candle.
        self.assertEqual(at("2026-01-02 03:00").ev_w_recent6h, 0.0)
        self.assertEqual(at("2026-01-02 04:00").ev_w_recent6h, 2.0)
        self.assertEqual(at("2026-01-02 10:00").ev_w_recent6h, 0.0)    # as_of 11:00, event 6h old -> out
        self.assertEqual(at("2026-01-02 10:00").ev_w_last24h, 2.0)
        # Date-only KR event appears only at 2026-01-04 00:00 (date + 24h).
        self.assertAlmostEqual(at("2026-01-03 22:00").ev_w_last24h, 1.5)        # only the CPI so far
        self.assertAlmostEqual(at("2026-01-03 23:00").ev_w_recent6h, 1.2)
        # Scheduled CPI is visible ahead of time.
        self.assertAlmostEqual(at("2026-01-03 12:00").ev_hours_to_next_kept, 0.5)
        self.assertAlmostEqual(at("2026-01-02 14:00").ev_w_next24h, 1.5)
        self.assertEqual(at("2026-01-02 12:00").ev_w_next24h, 0.0)       # 24.5h ahead

    def test_future_unscheduled_events_do_not_change_past(self):
        from event_impact import weighted_event_features
        weights = {"CRYPTO:EXCHANGE_INCIDENT": 2.0, "US:CPI": 1.5}
        dates = pd.Series(pd.date_range("2026-01-01", "2026-01-04", freq="h", tz="UTC"))
        a = weighted_event_features(dates, self.calendar(), weights)
        b = weighted_event_features(dates, self.calendar("2026-01-04T10:00:00Z,true,CRYPTO,EXCHANGE_INCIDENT,false,high,d,d,u\n"), weights)
        pd.testing.assert_frame_equal(a, b)

    def test_dropped_groups_contribute_nothing(self):
        from event_impact import CAP_HOURS, weighted_event_features
        dates = pd.Series(pd.date_range("2026-01-01", "2026-01-04", freq="h", tz="UTC"))
        f = weighted_event_features(dates, self.calendar(), {})
        self.assertEqual(float(f.drop(columns="ev_hours_to_next_kept").abs().sum().sum()), 0.0)
        self.assertTrue((f.ev_hours_to_next_kept == CAP_HOURS).all())


def synthetic_prices(hours=24 * 7 * 30, start="2024-01-01", seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=hours, freq="h", tz="UTC")
    rets = rng.normal(0, 0.004, hours)
    return idx, rets


class ImpactTableTests(unittest.TestCase):
    def build(self, loud_group="US:CPI"):
        idx, rets = synthetic_prices()
        lines, loud_times = [], []
        for k in range(12):
            loud = idx[500 + k * 300]
            quiet = idx[650 + k * 300]
            loud_times.append(loud)
            lines.append(f"{loud.isoformat()},true,US,CPI,true,high,x,x,u\n")
            lines.append(f"{quiet.isoformat()},true,JP,CENTRAL_BANK,true,high,y,y,u\n")
        for k in range(5):
            lines.append(f"{idx[560 + k * 300].isoformat()},true,MIDEAST,GEOPOLITICAL,false,high,z,z,u\n")
        for t in loud_times:
            pos = idx.get_loc(t)
            rets[pos:pos + 6] *= 3.0
        for k in range(5):
            pos = 560 + k * 300
            rets[pos:pos + 6] *= 3.0
        close = pd.Series(100 * np.exp(np.cumsum(rets)), index=idx)
        from event_impact import load_calendar
        return load_calendar(write(HEADER + "".join(lines))), {"BTC/USDT:USDT": close, "ETH/USDT:USDT": close * 0.05}

    def test_loud_group_kept_quiet_and_small_groups_dropped(self):
        from event_impact import impact_table, weights_from_table
        cal, prices = self.build()
        table = impact_table(cal, prices, bootstrap=200).set_index("group")
        self.assertTrue(table.loc["US:CPI", "kept"])
        self.assertGreater(table.loc["US:CPI", "ratio"], 2.0)
        self.assertLess(table.loc["US:CPI", "ci_lo"], table.loc["US:CPI", "ratio"])
        self.assertFalse(table.loc["JP:CENTRAL_BANK", "kept"])           # ratio ~1
        self.assertFalse(table.loc["MIDEAST:GEOPOLITICAL", "kept"])      # loud but n=5 < 8
        self.assertEqual(set(weights_from_table(table.reset_index())), {"US:CPI"})

    def test_window_uses_only_its_own_prices(self):
        from event_impact import impact_table
        cal, prices = self.build()
        end = prices["BTC/USDT:USDT"].index[2400]
        a = impact_table(cal, prices, end=end)
        changed = {k: v.copy() for k, v in prices.items()}
        for v in changed.values():
            v.iloc[2400:] = v.iloc[2400:] * np.linspace(1, 3, len(v) - 2400)
        b = impact_table(cal, changed, end=end)
        pd.testing.assert_frame_equal(a, b)
        self.assertLess(int(a.n.max()), 12)


class BlackoutTests(unittest.TestCase):
    def test_blackout_window(self):
        from event_impact import blackout_mask, load_calendar
        cal = load_calendar(write(HEADER + "2026-01-02T13:30:00Z,true,US,CPI,true,high,b,b,u\n2026-01-03T12:00:00Z,true,KR,REG_KR,true,high,c,c,u\n"))
        times = pd.Series(pd.to_datetime(["2026-01-02T12:00Z", "2026-01-02T15:30Z", "2026-01-02T16:00Z", "2026-01-03T12:00Z"]))
        self.assertEqual(list(blackout_mask(times, cal)), [True, True, False, False])


class CalibrationTests(unittest.TestCase):
    def test_purged_split_is_time_ordered(self):
        from spike_calibration import purged_holdout_split
        fit, hold = purged_holdout_split(100, 0.25, 12)
        self.assertEqual(list(hold), list(range(75, 100)))
        self.assertEqual(fit[-1], 62)

    def test_platt_fixes_overconfidence(self):
        from spike_calibration import CalibratedModel, PlattMap
        rng = np.random.default_rng(3)
        true_p = rng.uniform(0.2, 0.45, 4000)
        hit = rng.uniform(size=4000) < true_p
        raw = np.clip(true_p * 2 - 0.1, 0.01, 0.99)       # overconfident scores
        platt = PlattMap.fit(raw, hit)
        self.assertTrue(platt.fitted)
        q = platt(raw)
        self.assertLess(abs(q.mean() - hit.mean()), 0.02)
        self.assertLess(abs(q[raw > 0.7].mean() - hit[raw > 0.7].mean()), 0.08)
        self.assertLess(q[raw > 0.7].mean(), raw[raw > 0.7].mean() - 0.2)
        base = SimpleNamespace(classes_=np.array(["no", "yes"]), predict_proba=lambda X: np.column_stack([1 - X[:, 0], X[:, 0]]))
        model = CalibratedModel(base, platt)
        proba = model.predict_proba(np.array([[0.8], [0.2]]))
        np.testing.assert_allclose(proba.sum(axis=1), 1.0)
        self.assertLess(proba[0, 1], 0.8)
        self.assertIn(model.predict(np.array([[0.99]]))[0], ["no", "yes"])

    def test_single_class_holdout_is_identity(self):
        from spike_calibration import PlattMap
        platt = PlattMap.fit(np.full(50, 0.3), np.zeros(50))
        self.assertFalse(platt.fitted)
        self.assertAlmostEqual(float(platt(np.array([0.3]))[0]), 0.3, places=6)


class StrategyTests(unittest.TestCase):
    def test_exchange_minimum_from_market_limits(self):
        from SafeTrendV0 import SafeTrendV0
        market = {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}, "contractSize": 1}
        self.assertAlmostEqual(SafeTrendV0.exchange_min_margin(market, 100000, 2), 5.0)
        self.assertAlmostEqual(SafeTrendV0.exchange_min_margin(market, 20000, 2), 2.5)
        # target margin 1.05 / (0.04 * 2) = 13.125. A real minimum of 18 (1.37x) is accepted;
        # Freqtrade's reserve-padded minimum (18 * 1.105 = 19.9, 1.52x) would have skipped it.
        self.assertAlmostEqual(SafeTrendV0.risk_stake(105, 100, 2, 2, 18, 100), 18)
        self.assertEqual(SafeTrendV0.risk_stake(105, 100, 2, 2, 18 * 1.105, 100), 0)

    def test_low_tier_matches_spec(self):
        from SafeTrendAITiered import TIER_PROFILES, SafeTrendAITiered
        low = TIER_PROFILES["low"]
        self.assertEqual((low["risk"], low["leverage"], low["stop_atr"], low["trail_atr"], low["tp_atr"], low["donchian_exit"]), (0.005, 2.0, 1.5, None, 2.0, False))
        strategy = SafeTrendAITiered({})
        trade = FakeTrade(100.0, tag="safe_long|tier=low|p=0.5")
        self.assertFalse(strategy.confirm_trade_exit("X", trade, "market", 1, 1, "GTC", "opposite_donchian_10", None))

    def history(self, highs):
        dates = pd.date_range("2026-01-01", periods=len(highs), freq="h", tz="UTC")
        return pd.DataFrame({"date": dates, "high": highs, "low": np.array(highs) - 1, "atr_4h": 2.0})

    def test_stop_anchored_ratchet_and_breakeven(self):
        from SafeTrendPhaseB import SafeTrendTieredV1Plus
        from SafeTrendV0 import SafeTrendV0
        base = SafeTrendV0({})
        trade = FakeTrade(100.0, tag="safe_long")
        hist = self.history([100.5, 101.0, 101.5])
        self.assertAlmostEqual(base.stop_price(trade, hist, 2.0), 96.0)        # 100 - 2 x 2
        trade.open_rate = 103.0                                                # a later fill moves the average
        self.assertAlmostEqual(base.stop_price(trade, hist, 2.0), 96.0)        # stop stays anchored
        plus = SafeTrendTieredV1Plus({})
        t2 = FakeTrade(100.0, tag="safe_long|tier=high|p=0.7")
        self.assertAlmostEqual(plus.stop_price(t2, self.history([100.5, 101.5]), 2.0), 96.0)
        be = plus.stop_price(t2, self.history([100.5, 101.5, 102.2]), 2.0)      # +2.2 >= 1 ATR
        self.assertAlmostEqual(be, 100.0 * (1 + 0.0012))
        self.assertAlmostEqual(plus.stop_price(t2, self.history([100.5]), 2.0), be)  # ratchet never loosens

    def test_portfolio_caps(self):
        from SafeTrendPhaseB import SafeTrendTieredV1Plus
        s = SafeTrendTieredV1Plus({})
        t = lambda short, risk_stop, i: FakeTrade(100.0, is_short=short, amount=1.0, stop=risk_stop, trade_id=i)  # noqa: E731
        two_long = [t(False, 99.0, 1), t(False, 99.0, 2)]
        self.assertTrue(s.portfolio_allows(two_long[:1], False, "mid", 1.0, 105))
        self.assertFalse(s.portfolio_allows(two_long, False, "mid", 1.0, 105))   # 3rd only for high
        self.assertTrue(s.portfolio_allows(two_long, False, "high", 1.0, 105))
        three_long = two_long + [t(False, 99.0, 3)]
        self.assertFalse(s.portfolio_allows(three_long, False, "high", 1.0, 105))  # max 3 same side
        self.assertTrue(s.portfolio_allows(three_long, True, "high", 1.0, 105))
        self.assertFalse(s.portfolio_allows(three_long + [t(True, 101.0, 4)], True, "high", 0.1, 105))  # max 4
        # risk: 3 x 1.0 open + 1.3 new = 4.3 > 4.2 (4% of 105)
        self.assertFalse(s.portfolio_allows(three_long, True, "high", 1.3, 105))
        self.assertTrue(s.portfolio_allows(three_long, True, "high", 1.2, 105))
        locked = t(False, 101.0, 5)                                               # stop above entry -> 0 risk
        self.assertEqual(s.trade_risk(locked), 0.0)

    def test_scale_in_size_respects_risk(self):
        from SafeTrendPhaseB import SafeTrendTieredV1Plus
        add = SafeTrendTieredV1Plus.add_amount
        # long 1 @100, stop 101 (locked +1), price 110, budget 1.575 -> (1.575 + 1) / 9 = 0.286
        self.assertAlmostEqual(add(1.0, 100.0, 101.0, 110.0, False, 1.575, 10.0), 2.575 / 9)
        self.assertAlmostEqual(add(1.0, 100.0, 101.0, 110.0, False, 1.575, 0.1), 0.1)    # capped by fraction
        self.assertEqual(add(1.0, 100.0, 101.0, 100.5, False, 1.575, 1.0), 0.0)          # price below stop
        self.assertAlmostEqual(add(1.0, 100.0, 99.0, 90.0, True, 1.0, 10.0), 2.0 / 9)    # short mirror


class AnalysisLibTests(unittest.TestCase):
    def test_funding_crossings_and_both_sides_pay(self):
        from phase_b_lib import funding_crossings, trade_funding
        open_d, close_d = pd.Timestamp("2026-01-01T08:00Z"), pd.Timestamp("2026-01-02T08:00Z")
        crossings = funding_crossings(open_d, close_d)
        self.assertEqual(list(crossings.hour), [16, 0, 8])                      # entry at 08:00 does not pay 08:00
        mark = pd.Series(100.0, index=pd.date_range("2026-01-01", periods=48, freq="h", tz="UTC"))
        fills = [(open_d, 2.0, 100.0)]
        cons_long, signed_long, n = trade_funding(open_d, close_d, False, fills, mark)
        cons_short, signed_short, _ = trade_funding(open_d, close_d, True, fills, mark)
        self.assertEqual(n, 3)
        self.assertAlmostEqual(cons_long, 3 * 0.0001 * 200)
        self.assertAlmostEqual(cons_short, cons_long)                            # conservative: shorts pay too
        self.assertAlmostEqual(signed_short, -signed_long)                       # phase A: shorts receive
        later = [(open_d, 2.0, 100.0), (pd.Timestamp("2026-01-02T00:00Z"), 1.0, 100.0)]
        self.assertAlmostEqual(trade_funding(open_d, close_d, False, later, mark)[0], 0.0001 * (200 + 200 + 300))

    def test_mfe_mae_clipped_at_exit(self):
        from phase_b_lib import mfe_mae_atr
        idx = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
        candles = pd.DataFrame({"open": [100, 101, 102, 99], "high": [101, 103, 103, 104], "low": [99.5, 100.5, 98, 90]}, index=idx)
        # long stopped at 98.5 on the candle opened 02:00 (its low 98 and the later candle are excluded)
        mfe, mae = mfe_mae_atr(candles, idx[0], idx[2], 100.0, 98.5, False, 1.0)
        self.assertAlmostEqual(mfe, 3.0)       # highs 101, 103 and the exit candle clipped to [98.5, 102]
        self.assertAlmostEqual(mae, 1.5)       # not 2.0 (98 after the stop) and not 10 (next candle)

    def test_equity_mark_to_market_and_drawdown(self):
        from phase_b_lib import equity_curve, max_drawdown_pct, period_return_pct
        idx = pd.date_range("2026-01-01", periods=10, freq="h", tz="UTC")
        close = pd.Series([100, 100, 90, 80, 95, 110, 110, 110, 110, 110], index=idx, dtype=float)
        trades = pd.DataFrame([{
            "pair": "X", "open_date": idx[1], "close_date": idx[6], "is_short": False, "fills": [(idx[1], 1.0, 100.0)],
            "fee_open": 0.0, "profit_abs": 10.0, "funding_times": [pd.Timestamp("2026-01-01T04:00Z")], "funding_costs": [1.0],
        }])
        eq = equity_curve(trades, {"X": close}, 100.0, idx[0], idx[-1] + pd.Timedelta(hours=1))
        self.assertAlmostEqual(eq.loc[pd.Timestamp("2026-01-01T04:00Z")], 79.0)  # close 80, 04:00 funding 1 charged
        self.assertAlmostEqual(eq.loc[pd.Timestamp("2026-01-01T05:00Z")], 94.0)  # 95 - funding 1
        self.assertAlmostEqual(eq.iloc[-1], 109.0)
        self.assertAlmostEqual(max_drawdown_pct(eq), 21.0)                       # closed-trade MDD would be 0
        self.assertAlmostEqual(period_return_pct(eq, 100.0, idx[0], eq.index[-1]), 9.0)

    def test_reliability_gap(self):
        from phase_b_lib import reliability
        table, gap = reliability(np.array([0.1, 0.1, 0.7, 0.7]), np.array([0, 1, 1, 1]))
        self.assertAlmostEqual(gap, (2 * 0.4 + 2 * 0.3) / 4)
        self.assertEqual(int(table.rows.sum()), 4)


if __name__ == "__main__":
    unittest.main(verbosity=1)
