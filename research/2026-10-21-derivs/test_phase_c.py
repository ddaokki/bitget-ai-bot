"""Phase C checks (no lookahead, identical features, window hygiene).

  python -P /research/test_phase_c.py      (inside the FreqAI image; phase B dir at /research_b)

Synthetic tests always run. Tests marked [data] use the downloaded Bitget/Binance
files and are skipped when they are missing.
"""
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/research_b")
sys.path.insert(0, "/freqtrade/user_data/strategies")
import features_c as fc  # noqa: E402
import walkforward_c as wf  # noqa: E402

H = pd.Timedelta(hours=1)
RESULTS = []


def check(name):
    def wrap(fn):
        try:
            fn()
            RESULTS.append((name, "PASS", ""))
        except SkipTest as exc:
            RESULTS.append((name, "SKIP", str(exc)))
        except Exception:  # noqa: BLE001
            RESULTS.append((name, "FAIL", traceback.format_exc(limit=3)))
        return fn
    return wrap


class SkipTest(Exception):
    pass


def synthetic(end="2022-04-01", seed=1):
    rng = np.random.default_rng(seed)
    t5 = pd.date_range("2022-01-01", end, freq="5min", tz="UTC", inclusive="left")
    n = len(t5)
    metrics = pd.DataFrame({
        "date": t5,
        "sum_open_interest": np.exp(np.cumsum(rng.normal(0, 0.002, n))) * 1000,
        "sum_open_interest_value": 1.0,
        "count_toptrader_long_short_ratio": np.exp(rng.normal(0, 0.2, n)),
        "sum_toptrader_long_short_ratio": np.exp(rng.normal(0, 0.2, n)),
        "count_long_short_ratio": np.exp(rng.normal(0, 0.2, n)),
        "sum_taker_long_short_vol_ratio": np.exp(rng.normal(0, 0.3, n)),
    })
    t8 = pd.date_range("2022-01-01", end, freq="8h", tz="UTC", inclusive="left")
    funding = pd.DataFrame({"date": t8, "funding_rate": rng.normal(1e-4, 1e-4, len(t8))})
    return metrics, funding


@check("positioning features are prefix-invariant (appending future data changes nothing)")
def _():
    metrics, funding = synthetic()
    full = fc.positioning_hourly(metrics, funding, pd.Timestamp("2022-04-01", tz="UTC"))
    cut = pd.Timestamp("2022-03-01 13:00", tz="UTC")
    part = fc.positioning_hourly(metrics.loc[metrics.date < cut], funding.loc[funding.date < cut], cut)
    common = part.index[part.index <= cut]
    pd.testing.assert_frame_equal(full.loc[common], part.loc[common], check_freq=False)
    assert part.loc[common].notna().any().all(), "every feature should have values before the cut"


@check("a snapshot stamped exactly at the candle close is NOT used (strict as_of)")
def _():
    metrics, funding = synthetic()
    grid = fc.hourly_grid(pd.Timestamp("2022-03-01", tz="UTC"))
    base = fc.metrics_hourly(metrics, grid)
    spiked = metrics.copy()
    at = pd.Timestamp("2022-02-10 12:00", tz="UTC")
    spiked.loc[spiked.date == at, "count_long_short_ratio"] = 1e6
    after = fc.metrics_hourly(spiked, grid)
    assert after.loc[at, "ls_global"] == base.loc[at, "ls_global"], "as_of == snapshot time must not see it"
    # One hour later the 12:00 snapshot is no longer the latest one either, so the spike
    # only reaches the 24h change via the shifted value at as_of = 12:05 .. 13:00 -> none.


@check("funding settled exactly at as_of is not visible until the next hour; 4h funding is scaled to 8h")
def _():
    t = pd.to_datetime(["2022-01-01 00:00", "2022-01-01 08:00", "2022-01-01 12:00", "2022-01-01 16:00"], utc=True)
    funding = pd.DataFrame({"date": t, "funding_rate": [1e-4, 2e-4, 3e-4, 4e-4]})
    grid = pd.date_range("2022-01-01 01:00", "2022-01-01 18:00", freq="h", tz="UTC")
    h = fc.funding_hourly(funding, grid)
    assert np.isclose(h.loc[pd.Timestamp("2022-01-01 08:00", tz="UTC"), "fund_last"], 1e-4)
    assert np.isclose(h.loc[pd.Timestamp("2022-01-01 09:00", tz="UTC"), "fund_last"], 2e-4)
    # 12:00 settlement came 4h after the previous one: 3e-4 per 4h = 6e-4 per 8h
    assert np.isclose(h.loc[pd.Timestamp("2022-01-01 13:00", tz="UTC"), "fund_last"], 6e-4)


@check("deriv_features reads the hour that ENDS at the candle close (date + 1h)")
def _():
    metrics, funding = synthetic()
    own = fc.positioning_hourly(metrics, funding, pd.Timestamp("2022-04-01", tz="UTC"))
    dates = pd.Series(pd.date_range("2022-03-01", periods=5, freq="h", tz="UTC"), index=[10, 11, 12, 13, 14])
    feats = fc.deriv_features(dates, own, own)
    assert list(feats.index) == [10, 11, 12, 13, 14]
    assert np.isclose(feats.loc[10, "oi_chg_24h"], own.loc[dates[10] + H, "oi_chg_24h"])
    assert np.isclose(feats.loc[12, "btc_ls_global"], own.loc[dates[12] + H, "ls_global"])


@check("walk-forward windows tile 2023-10-01 .. 2026-10-20 in 90-day steps with 365-day training")
def _():
    w = wf.windows()
    assert w[0] == (pd.Timestamp("2022-10-01", tz="UTC"), pd.Timestamp("2023-10-01", tz="UTC"), pd.Timestamp("2023-12-30", tz="UTC"))
    assert w[-1][2] == wf.END
    for (a, b, c), (_, d, _) in zip(w, w[1:]):
        assert c == d and b - a == wf.TRAIN


@check("training rows never overlap the prediction window, label windows included")
def _():
    dates = pd.date_range("2023-01-01", "2024-01-01", freq="h", tz="UTC")
    data = pd.DataFrame({"date": dates, "label": 1.0})
    ts, ws, we = wf.windows()[0]
    tr = data.loc[wf.train_mask(data, ts, ws)]
    pr = data.loc[wf.predict_mask(data, ws, we)]
    assert (tr.date + pd.Timedelta(hours=49) <= ws).all() and tr.date.max() == ws - pd.Timedelta(hours=49)
    assert tr.date.max() < pr.date.min()


@check("calibration holdout is later than the fit part by at least the 49h label span")
def _():
    dates = pd.Series(pd.date_range("2023-01-01", periods=2000, freq="4h", tz="UTC")).repeat(10).reset_index(drop=True)
    fit, hold = wf.holdout_split(dates)
    assert dates[fit].max() + pd.Timedelta(hours=49) <= dates[hold].min()
    assert abs(hold.mean() - 0.25) < 0.01


def load_real():
    from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
    from freqtrade.enums import CandleType
    path = Path("/freqtrade/user_data/data/bitget")
    frame = FeatherDataHandler(path).ohlcv_load("BTC/USDT:USDT", "1h", candle_type=CandleType.FUTURES, fill_missing=True, drop_incomplete=False)
    if frame.empty:
        raise SkipTest("no BTC candles")
    return frame


@check("[data] indicator_features equals SafeTrendAIV0.feature_engineering_standard (events off)")
def _():
    from SafeTrendAIV0 import SafeTrendAIV0
    from SafeTrendV0 import SafeTrendV0
    frame = load_real().iloc[:12000].copy()
    populated = SafeTrendV0({}).populate_indicators(frame, {"pair": "BTC/USDT:USDT"})
    strategy = SafeTrendAIV0.__new__(SafeTrendAIV0)
    strategy.config = {}
    strategy.events_enabled = lambda: False
    ref = SafeTrendAIV0.feature_engineering_standard(strategy, populated.copy(), {})
    mine = fc.indicator_features(populated)
    for name in fc.INDICATOR_FEATURES:
        pd.testing.assert_series_equal(mine[name].astype(float), ref[f"%-{name}"].astype(float), check_names=False)


@check("[data] training labels are identical when the data is cut at the prediction start")
def _():
    from SafeTrendAIV0 import forward_labels
    from SafeTrendV0 import SafeTrendV0
    frame = load_real()
    frame = frame.loc[frame.date < pd.Timestamp("2024-01-01", tz="UTC")].reset_index(drop=True)
    strategy = SafeTrendV0({})
    full = strategy.populate_indicators(frame.copy(), {})
    ts, ws, _ = wf.windows()[0]
    cut = strategy.populate_indicators(frame.loc[frame.date < ws].reset_index(drop=True), {})
    data = pd.DataFrame({"date": full.date, "label": forward_labels(full, rows=full.four_close).map({"yes": 1.0, "no": 0.0})})
    rows = wf.train_mask(data, ts, ws)
    truncated = forward_labels(cut, rows=cut.four_close).map({"yes": 1.0, "no": 0.0})
    assert rows.sum() > 1000
    np.testing.assert_array_equal(data.label[rows].to_numpy(), truncated.iloc[np.flatnonzero(rows)].to_numpy())


@check("[data] real positioning features are prefix-invariant at a training-window boundary")
def _():
    vision = Path("/freqtrade/user_data/data/binance_vision")
    if not (vision / "metrics_BTCUSDT.feather").is_file():
        raise SkipTest("no Binance data")
    metrics = pd.read_feather(vision / "metrics_BTCUSDT.feather")
    funding = pd.read_feather(vision / "funding_BTCUSDT.feather")
    end = pd.Timestamp("2024-03-01", tz="UTC")
    full = fc.positioning_hourly(metrics.loc[metrics.date < end], funding.loc[funding.date < end], end)
    cut = pd.Timestamp("2023-10-01", tz="UTC")
    part = fc.positioning_hourly(metrics.loc[metrics.date < cut], funding.loc[funding.date < cut], cut)
    common = part.index
    pd.testing.assert_frame_equal(full.loc[common], part, check_freq=False)


if __name__ == "__main__":
    for name, status, detail in RESULTS:
        print(f"{status:4} {name}" + (f"\n{detail}" if detail else ""))
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} ok ({sum(r[1] == 'SKIP' for r in RESULTS)} skipped)")
    sys.exit(1 if failed else 0)
