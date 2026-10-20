"""Deterministic offline checks; run inside the pinned Freqtrade image.

docker compose -p bitget-spike -f freqtrade/docker-compose.yml run --rm \
  -v <this dir>:/research --entrypoint python freqtrade -P /research/test_strategy.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/freqtrade/user_data/strategies")


def candles(count=6000, start="2025-06-01"):
    rng = np.random.default_rng(81)
    close = 100 + np.cumsum(rng.normal(0.005, 0.3, count))
    return pd.DataFrame({"date": pd.date_range(start, periods=count, freq="h", tz="UTC"), "open": close, "high": close + 0.4, "low": close - 0.4, "close": close, "volume": 100.0})


class FakeTrade:
    def __init__(self, open_rate, is_short=False, leverage=2.0, tag="safe_long", fee=0.0006):
        self.open_rate, self.is_short, self.leverage, self.enter_tag, self.fee = open_rate, is_short, leverage, tag, fee
        self.data = {}

    def get_custom_data(self, key):
        return self.data.get(key)

    def set_custom_data(self, key, value):
        self.data[key] = value

    def calc_profit_ratio(self, rate):
        sign = -1 if self.is_short else 1
        return sign * (rate / self.open_rate - 1) * self.leverage - 2 * self.fee * self.leverage


class StrategyTests(unittest.TestCase):
    def strategy(self, config=None):
        from SafeTrendV0 import SafeTrendV0
        return SafeTrendV0(config or {})

    def test_partial_resampled_candles_are_removed(self):
        strategy = self.strategy()
        self.assertEqual(len(strategy.closed_resample(candles(7), 4)), 1)
        self.assertEqual(len(strategy.closed_resample(candles(23), 24)), 0)

    def test_future_candles_do_not_change_existing_indicators_or_signals(self):
        strategy = self.strategy()
        source = candles()
        whole = strategy.populate_entry_trend(strategy.populate_indicators(source.copy(), {}), {})
        prefix = strategy.populate_entry_trend(strategy.populate_indicators(source.iloc[:5501].copy(), {}), {})
        columns = ["atr_4h", "ema50_1d", "ema200_1d", "dc_high_4h", "atr_q90_4h", "atr_rank_4h", "enter_long", "enter_short"]
        pd.testing.assert_frame_equal(whole.loc[:5500, columns], prefix[columns])
        merged = whole.dropna(subset=["date_4h", "date_1d"])
        # A 4h/1d bar is visible only on the 1h row whose close completes it.
        self.assertTrue(((merged.date_4h + pd.Timedelta(hours=4)) <= (merged.date + pd.Timedelta(hours=1))).all())
        self.assertTrue(((merged.date_1d + pd.Timedelta(days=1)) <= (merged.date + pd.Timedelta(hours=1))).all())

    def test_entry_start_suppresses_warmup_entries(self):
        source = candles()
        cutoff = source.date.iloc[4000]
        strategy = self.strategy({"spike": {"entry_start": str(cutoff)}})
        frame = strategy.populate_entry_trend(strategy.populate_indicators(source.copy(), {}), {})
        early = frame.date < cutoff
        self.assertEqual(int(frame.loc[early, ["enter_long", "enter_short"]].to_numpy().sum()), 0)

    def test_size_respects_risk_and_rejects_oversized_exchange_minimum(self):
        strategy = self.strategy()
        # wallet 105, risk 1.05, stop 4%, leverage 2 => margin 13.125.
        self.assertAlmostEqual(strategy.risk_stake(105, 100, 2, 2, 5, 100), 13.125)
        self.assertEqual(strategy.risk_stake(105, 100, 2, 2, 20, 100), 0)
        self.assertAlmostEqual(strategy.risk_stake(105, 100, 2, 2, 15, 100), 15)
        # low tier: 0.5% risk, 1.5xATR stop => 0.525 / (0.03 * 2) = 8.75
        self.assertAlmostEqual(strategy.risk_stake(105, 100, 2, 2, 5, 100, risk=0.005, stop_atr=1.5), 8.75)

    def test_tag_parsing_and_profiles(self):
        from SafeTrendAITiered import SafeTrendAITiered, tier_for
        from SafeTrendV0 import parse_tag
        self.assertEqual(parse_tag("safe_long|tier=mid|p=0.583"), {"name": "safe_long", "tier": "mid", "p": "0.583"})
        p = pd.Series([0.2, 0.45, 0.5499, 0.55, 0.6499, 0.65, 0.9, np.nan])
        self.assertEqual([None if pd.isna(x) else x for x in tier_for(p)], [None, "low", "low", "mid", "mid", "high", "high", None])
        strategy = SafeTrendAITiered({})
        self.assertEqual(strategy.profile("safe_long|tier=high|p=0.7")["leverage"], 3.0)
        self.assertEqual(strategy.profile("safe_long|tier=low|p=0.5")["stop_atr"], 1.5)
        self.assertEqual(strategy.profile("safe_long")["risk"], 0.01)

    def test_low_tier_take_profit_maps_to_target_price(self):
        from SafeTrendAITiered import SafeTrendAITiered
        strategy = SafeTrendAITiered({})
        trade = FakeTrade(100.0, tag="safe_long|tier=low|p=0.5")
        trade.set_custom_data("entry_atr", 2.0)
        roi = strategy.custom_roi("X", trade, None, 60, trade.enter_tag, "long")
        self.assertAlmostEqual(roi, trade.calc_profit_ratio(104.0))
        mid = FakeTrade(100.0, tag="safe_long|tier=mid|p=0.6")
        mid.set_custom_data("entry_atr", 2.0)
        self.assertIsNone(strategy.custom_roi("X", mid, None, 60, mid.enter_tag, "long"))

    def test_high_tier_ignores_donchian_exit(self):
        from SafeTrendAITiered import SafeTrendAITiered
        strategy = SafeTrendAITiered({})
        high = FakeTrade(100.0, tag="safe_long|tier=high|p=0.7")
        mid = FakeTrade(100.0, tag="safe_long|tier=mid|p=0.6")
        self.assertFalse(strategy.confirm_trade_exit("X", high, "market", 1, 1, "GTC", "opposite_donchian_10", None))
        self.assertTrue(strategy.confirm_trade_exit("X", mid, "market", 1, 1, "GTC", "opposite_donchian_10", None))
        self.assertTrue(strategy.confirm_trade_exit("X", high, "market", 1, 1, "GTC", "force_exit", None))

    def test_ai_features_are_past_only_and_exclude_target(self):
        from SafeTrendAIV0 import SafeTrendAIV0
        strategy = SafeTrendAIV0({})
        source = candles()
        whole = strategy.feature_engineering_standard(source.copy(), {})
        prefix = strategy.feature_engineering_standard(source.iloc[:5501].copy(), {})
        features = [c for c in whole if c.startswith("%")]
        self.assertEqual(len(features), 13)
        self.assertFalse(any(c.startswith("&") for c in features))
        pd.testing.assert_frame_equal(whole.loc[:5500, features], prefix[features])

    def test_labels_exclude_incomplete_future_and_treat_intrabar_ties_as_loss(self):
        from SafeTrendAIV0 import forward_labels
        source = candles(100)
        source["close"] = 100.0
        source["open"] = 100.0
        source["high"] = 101.0
        source["low"] = 99.0
        source["atr_4h"] = 1.0
        source["ema50_1d"] = 110.0
        source["ema200_1d"] = 100.0
        source.loc[1, "high"] = 103.0
        labels = forward_labels(source)
        self.assertEqual(labels.iloc[0], "yes")
        self.assertTrue(labels.iloc[-48:].isna().all())
        source.loc[1, "low"] = 97.0
        self.assertEqual(forward_labels(source).iloc[0], "no")
        self.assertTrue(forward_labels(source.iloc[:48]).isna().all())
        rows = pd.Series(False, index=source.index)
        rows.iloc[3] = True
        masked = forward_labels(source, rows=rows)
        self.assertEqual(int(masked.notna().sum()), 1)

    def test_labels_do_not_depend_on_data_after_training_end(self):
        from SafeTrendAIV0 import SafeTrendAIV0
        strategy = SafeTrendAIV0({})
        source = strategy.feature_engineering_standard(candles(), {})
        cut = 5000
        truncated = strategy.set_freqai_targets(source.iloc[:cut].copy(), {})["&-barrier"]
        self.assertTrue(truncated.iloc[-48:].isna().all())
        full = strategy.set_freqai_targets(source.copy(), {})["&-barrier"]
        known = truncated.notna()
        pd.testing.assert_series_equal(truncated[known], full.iloc[:cut][known])


class EventFeatureTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        handle.write(text)
        handle.close()
        return Path(handle.name)

    def test_point_in_time_rules(self):
        from event_features import compute_event_features, load_events
        path = self.write(
            "ts_utc,time_known,region,category,scheduled,importance\n"
            "2026-01-02T12:30:00Z,2025-12-01T00:00:00Z,US,CPI,true,high\n"
            "2026-01-03T05:00:00Z,,CRYPTO,hack,false,high\n"
            "2026-01-03T06:00:00Z,2026-01-03T03:00:00Z,KR,ban,false,3\n"
            "2026-01-04T00:00:00Z,2026-01-03T23:00:00Z,US,FOMC,true,high\n"
            "2026-01-02T00:00:00Z,2025-12-01T00:00:00Z,US,PPI,true,low\n"
        )
        events = load_events(path)
        dates = pd.Series(pd.date_range("2026-01-02T00:00:00Z", "2026-01-04T12:00:00Z", freq="h"))
        feats = compute_event_features(dates, events)
        at = lambda ts: feats.loc[dates == pd.Timestamp(ts)].iloc[0]  # noqa: E731
        # as_of = candle close. 2026-01-02T00 candle closes 01:00 -> CPI at 12:30 is 11.5h away.
        self.assertAlmostEqual(at("2026-01-02T00:00:00Z").ev_hours_to_next_sched_high, 11.5)
        # FOMC becomes known only at 2026-01-03T23:00, so before that the next event is unknown (cap).
        self.assertEqual(at("2026-01-03T12:00:00Z").ev_hours_to_next_sched_high, 24.0 * 14)
        self.assertAlmostEqual(at("2026-01-03T22:00:00Z").ev_hours_to_next_sched_high, 1.0)
        # Unscheduled hack at 05:00 is invisible on the 03:00 candle (closes 04:00), visible on 04:00 (closes 05:00).
        self.assertEqual(at("2026-01-03T03:00:00Z").ev_in_last_24h_crypto_global, 0.0)
        self.assertEqual(at("2026-01-03T04:00:00Z").ev_hours_since_high_crypto_global, 0.0)
        # KR event with an earlier time_known still counts only once it happened.
        self.assertEqual(at("2026-01-03T04:00:00Z").ev_in_last_24h_kr, 0.0)
        self.assertEqual(at("2026-01-03T06:00:00Z").ev_hours_since_high_kr, 1.0)
        # Low-importance PPI is ignored; CPI happened 12:30 -> since on 2026-01-02T13 candle (close 14:00) = 1.5h.
        self.assertAlmostEqual(at("2026-01-02T13:00:00Z").ev_hours_since_high_us, 1.5)
        self.assertEqual(at("2026-01-02T05:00:00Z").ev_in_last_24h_us, 0.0)

    def test_future_events_do_not_change_past_features(self):
        from event_features import compute_event_features, load_events
        base = "ts_utc,time_known,region,category,scheduled,importance\n2026-01-02T12:00:00Z,,GLOBAL,war,false,high\n"
        extra = base + "2026-01-05T12:00:00Z,,US,hack,false,high\n"
        dates = pd.Series(pd.date_range("2026-01-01", "2026-01-05", freq="h", tz="UTC"))
        a = compute_event_features(dates, load_events(self.write(base)))
        b = compute_event_features(dates, load_events(self.write(extra)))
        pd.testing.assert_frame_equal(a, b)

    def test_missing_file_gives_capped_features(self):
        from event_features import FEATURE_COLUMNS, compute_event_features, load_events
        feats = compute_event_features(pd.Series(pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")), load_events("/nonexistent.csv"))
        self.assertEqual(list(feats.columns), FEATURE_COLUMNS)
        self.assertTrue((feats.filter(like="hours").to_numpy() == 24.0 * 14).all())

    def test_event_features_off_by_default_and_on_by_config(self):
        from SafeTrendAIV0 import SafeTrendAIV0
        self.assertFalse(SafeTrendAIV0({}).events_enabled())
        path = self.write("ts_utc,time_known,region,category,scheduled,importance\n2025-06-03T00:00:00Z,2025-05-01T00:00:00Z,US,FOMC,true,high\n")
        strategy = SafeTrendAIV0({"event_features": {"enabled": True, "path": str(path)}})
        self.assertTrue(strategy.events_enabled())
        frame = strategy.feature_engineering_standard(candles(500), {})
        self.assertIn("%-ev_hours_to_next_sched_high", frame)
        self.assertEqual(len([c for c in frame if c.startswith("%")]), 13 + 7)


if __name__ == "__main__":
    unittest.main(verbosity=1)
