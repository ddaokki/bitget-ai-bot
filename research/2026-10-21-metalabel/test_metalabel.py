"""Phase D checks: trade simulation sees no future, exit-time purge, point-in-time features.

  python -P /research/test_metalabel.py     (FreqAI image; phase C dir at /research_c)

Synthetic tests always run. [data] tests use the downloaded candles. The phase C
feature checks (prefix invariance, strict as_of, identical indicator formulas) are
imported from /research_c/test_phase_c.py and reported here too.
"""
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/research_c")
sys.path.insert(0, "/research_b")
sys.path.insert(0, "/freqtrade/user_data/strategies")
import metalabel_lib as ml  # noqa: E402

H = pd.Timedelta(hours=1)
RESULTS = []


class SkipTest(Exception):
    pass


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


def path(n=200, seed=3, start="2024-01-01 00:00"):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    opn = np.r_[100.0, close[:-1]]
    high = np.maximum(opn, close) * (1 + rng.uniform(0, 0.005, n))
    low = np.minimum(opn, close) * (1 - rng.uniform(0, 0.005, n))
    dates = pd.date_range(start, periods=n, freq="h", tz="UTC").to_numpy()
    return dates, opn, high, low, close, np.full(n, 1.0), np.zeros(n, dtype=bool)


def sim(arrs, i=10, side=1, **kw):
    d, o, h, lo, c, atr, ex = arrs
    return ml.simulate_trade(d, o, h, lo, c, atr, ex, i, side, None, **kw)


@check("trade result is unchanged when every candle AFTER the exit candle is replaced (spec and engine modes)")
def _():
    for mode, side, seed in [(m, s, k) for m in ("spec", "engine") for s in (1, -1) for k in range(5)]:
            arrs = path(seed=seed)
            base = sim(arrs, side=side, mode=mode)
            j = int(pd.DatetimeIndex(arrs[0]).get_loc(base["exit_candle"]))
            assert base["exit_reason"] != "open_at_end"
            d, o, h, lo, c, atr, ex = (a.copy() for a in arrs)
            rng = np.random.default_rng(99)
            o[j + 1:] *= rng.uniform(0.5, 1.5, len(o) - j - 1)
            h[j + 1:] = o[j + 1:] * 2
            lo[j + 1:] = o[j + 1:] * 0.3
            c[j + 1:] = o[j + 1:]
            atr[j + 1:] = 50.0
            ex[j + 1:] = True
            again = ml.simulate_trade(d, o, h, lo, c, atr, ex, 10, side, None, mode=mode)
            for k in ("exit_candle", "exit_price", "exit_reason", "r"):
                assert again[k] == base[k], (mode, side, seed, k, again[k], base[k])


@check("trade result is unchanged when the data is cut right after the exit candle")
def _():
    arrs = path(seed=7)
    base = sim(arrs)
    j = int(pd.DatetimeIndex(arrs[0]).get_loc(base["exit_candle"]))
    cut = [a[: j + 1] for a in arrs]
    again = sim(cut)
    assert again["exit_reason"] == base["exit_reason"] and again["r"] == base["r"]


@check("the trailing stop on candle j ignores candle j's own high (closed data only)")
def _():
    n = 30
    d = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC").to_numpy()
    o = np.full(n, 100.0)
    h = np.full(n, 100.5)
    lo = np.full(n, 99.5)
    c = np.full(n, 100.0)
    # Candle 15 spikes to 120 then trades down to 110. A trail using its own high would put
    # the stop at 120 - 3 = 117 and exit inside candle 15; the engine cannot know that high yet.
    h[15], lo[15], c[15] = 120.0, 110.0, 112.0
    o[16:], h[16:], lo[16:], c[16:] = 112.0, 112.5, 111.5, 112.0
    r =ml.simulate_trade(d, o, h, lo, c, np.full(n, 1.0), np.zeros(n, dtype=bool), 5, 1, None)
    assert r["exit_candle"] == pd.Timestamp(d[16]), r   # first candle after the spike closed: open 112 < stop 117 -> gap fill at open
    assert r["exit_price"] == 112.0 and r["exit_reason"] == "stop"
    # Engine mode: a requested stop above the whole candle is ignored (Freqtrade returns 0),
    # so the trade survives candle 16 with the old 98 stop and keeps the 117 request pending.
    e = ml.simulate_trade(d, o, h, lo, c, np.full(n, 1.0), np.zeros(n, dtype=bool), 5, 1, None, mode="engine")
    assert e["exit_reason"] == "open_at_end", e


@check("initial stop = entry - 2xATR; a gap through the stop fills at the worse open")
def _():
    n = 20
    d = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC").to_numpy()
    o = np.full(n, 100.0)
    h = np.full(n, 100.5)
    lo = np.full(n, 99.5)
    c = np.full(n, 100.0)
    o[8], h[8], lo[8], c[8] = 95.0, 96.0, 94.0, 95.0   # opens below stop 98
    r = ml.simulate_trade(d, o, h, lo, c, np.full(n, 1.0), np.zeros(n, dtype=bool), 2, 1, None, fee=0.0, funding_rate=0.0)
    assert r["exit_price"] == 95.0 and np.isclose(r["r"], -2.5)
    o[8], h[8], lo[8] = 99.0, 99.5, 97.5   # touches 98 inside the candle
    r = ml.simulate_trade(d, o, h, lo, c, np.full(n, 1.0), np.zeros(n, dtype=bool), 2, 1, None, fee=0.0, funding_rate=0.0)
    assert r["exit_price"] == 98.0 and np.isclose(r["r"], -1.0)


@check("opposite Donchian exit on row k fills at the open of row k+1; fees and funding reduce R")
def _():
    n = 40
    d = pd.date_range("2024-01-01 05:00", periods=n, freq="h", tz="UTC").to_numpy()
    o = np.linspace(100, 110, n)
    h = o + 0.2
    lo = o - 0.2
    c = o + 0.1
    ex = np.zeros(n, dtype=bool)
    ex[20] = True
    r = ml.simulate_trade(d, o, h, lo, c, np.full(n, 1.0), ex, 3, 1, None)
    assert r["exit_candle"] == pd.Timestamp(d[21]) and r["exit_price"] == o[21] and r["exit_reason"] == "opposite_donchian_10"
    # entry candle d[4] = 09:00, exit candle d[21] = next day 02:00: crossings 16:00 and 00:00 -> 2
    assert len(ml.funding_crossings(pd.Timestamp(d[4]), pd.Timestamp(d[21]))) == 2
    expected = ((o[21] - o[4]) - 0.0006 * (o[4] + o[21]) - 2 * 0.0001 * o[4]) / 2.0
    assert np.isclose(r["r"], expected), (r["r"], expected)
    assert r["exit_time"] == pd.Timestamp(d[21]) + H


@check("exit-time purge: only trades whose exit is known before the window start train it")
def _():
    ws = pd.Timestamp("2024-01-01", tz="UTC")
    ts = ws - pd.Timedelta(days=365)
    date = pd.Series(pd.to_datetime(["2023-06-01 00:00", "2023-12-30 00:00", "2023-12-31 20:00", "2023-12-31 22:00", "2024-01-02 00:00", "2022-12-01 00:00"], utc=True))
    exit_time = pd.Series(pd.to_datetime(["2023-06-05 00:00", "2024-01-03 00:00", "2023-12-31 23:00", "2024-01-01 00:00", "2024-01-03 00:00", "2022-12-02 00:00"], utc=True))
    oae = pd.Series([False, False, False, False, False, False])
    got = ml.usable_for(exit_time, oae, date, ts, ws).tolist()
    assert got == [True, False, True, False, False, False], got
    assert not ml.usable_for(exit_time, pd.Series([True] * 6), date, ts, ws).any()


@check("portfolio: one position per coin, at most two at once, an exit at the open frees a slot on the same candle")
def _():
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    rows = []
    def add(pair, start_h, end_h, side=1, reason="stop"):
        rows.append({"pair": pair, "date": t0 + pd.Timedelta(hours=start_h - 1), "entry_time": t0 + pd.Timedelta(hours=start_h),
                     "exit_candle": t0 + pd.Timedelta(hours=end_h), "exit_reason": reason, "entry": 100.0, "exit_price": 101.0,
                     "atr": 1.0, "side": side, "funding_per_unit": 0.0, "min_cost": 5.0, "min_amount": 0.0, "r": 0.5})
    add("A", 1, 10)
    add("A", 2, 5)      # same coin while A is open -> skipped
    add("B", 3, 6, reason="opposite_donchian_10")
    add("C", 4, 8)      # two open -> skipped
    add("C", 6, 9)      # B exits at the open of 6 -> slot free -> taken
    taken, curve = ml.portfolio(pd.DataFrame(rows), ["A", "B", "C"], t0, t0 + pd.Timedelta(days=1))
    assert list(zip(taken.pair, taken.entry_time.dt.hour)) == [("A", 1), ("B", 3), ("C", 6)], taken
    # 1% risk: notional = 105 * 0.01 * 100 / 2 = 52.5 -> 0.525 units; pnl = 0.525 * (1 - 0.0006*201)
    assert np.isclose(taken.pnl.iloc[0], 0.525 * (1 - 0.0006 * 201))
    assert curve.index.is_monotonic_increasing


@check("portfolio: a stop inside candle T does not free its slot (or book PnL) for an entry at T's open")
def _():
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    def row(pair, start_h, end_h, reason="stop", exit_price=101.0):
        return {"pair": pair, "date": t0 + pd.Timedelta(hours=start_h - 1), "entry_time": t0 + pd.Timedelta(hours=start_h),
                "exit_candle": t0 + pd.Timedelta(hours=end_h), "exit_reason": reason, "entry": 100.0, "exit_price": exit_price,
                "atr": 1.0, "side": 1, "funding_per_unit": 0.0, "min_cost": 5.0, "min_amount": 0.0, "r": 0.5}
    # Two slots full; B stops inside candle 6. C at 6 must be skipped, C at 7 taken.
    rows = [row("A", 1, 10), row("B", 3, 6), row("C", 6, 9), row("D", 7, 9)]
    taken, _ = ml.portfolio(pd.DataFrame(rows), ["A", "B", "C", "D"], t0, t0 + pd.Timedelta(days=1))
    assert list(zip(taken.pair, taken.entry_time.dt.hour)) == [("A", 1), ("B", 3), ("D", 7)], taken
    # Same coin re-entry on the stop candle is also blocked.
    taken, _ = ml.portfolio(pd.DataFrame([row("A", 1, 5), row("A", 5, 8)]), ["A"], t0, t0 + pd.Timedelta(days=1))
    assert len(taken) == 1
    # Sizing at T must not include a stop PnL realised inside T. If the win were booked first
    # (as for an exit at the open), the second trade would be larger.
    rows = [row("A", 1, 5, exit_price=150.0), row("B", 5, 8)]
    taken, _ = ml.portfolio(pd.DataFrame(rows), ["A", "B"], t0, t0 + pd.Timedelta(days=1))
    assert np.isclose(taken.margin.iloc[1], taken.margin.iloc[0]), taken   # both sized on the 105 start balance
    rows[0]["exit_reason"] = "opposite_donchian_10"
    taken2, _ = ml.portfolio(pd.DataFrame(rows), ["A", "B"], t0, t0 + pd.Timedelta(days=1))
    assert taken2.margin.iloc[1] > taken.margin.iloc[1]


@check("portfolio skips a trade whose exchange minimum needs > 1.5x the target risk")
def _():
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    row = {"pair": "A", "date": t0, "entry_time": t0 + H, "exit_candle": t0 + 5 * H, "exit_reason": "stop", "entry": 100.0, "exit_price": 101.0, "atr": 10.0,
           "side": 1, "funding_per_unit": 0.0, "min_cost": 5.0, "min_amount": 0.0, "r": 0.1}
    # target notional = 105*0.01*100/20 = 5.25 >= 5 -> ok; with atr 20 -> 2.625, min 5 needs 1.9x -> skip
    taken, _ = ml.portfolio(pd.DataFrame([row]), ["A"], t0, t0 + pd.Timedelta(days=1))
    assert len(taken) == 1
    taken, _ = ml.portfolio(pd.DataFrame([{**row, "atr": 20.0}]), ["A"], t0, t0 + pd.Timedelta(days=1))
    assert len(taken) == 0


# ------------------------------------------------------------------ data
def btc_frame(end=None):
    from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
    from freqtrade.enums import CandleType
    frame = FeatherDataHandler(Path("/freqtrade/user_data/data/bitget")).ohlcv_load(
        "BTC/USDT:USDT", "1h", candle_type=CandleType.FUTURES, fill_missing=True, drop_incomplete=False)
    if frame.empty:
        raise SkipTest("no BTC candles")
    if end is not None:
        frame = frame.loc[frame.date < end]
    return frame.reset_index(drop=True)


def populate(frame):
    from SafeTrendV0 import SafeTrendV0
    s = SafeTrendV0({})
    f = s.populate_indicators(frame.copy(), {"pair": "BTC/USDT:USDT"})
    f = s.populate_entry_trend(f, {"pair": "BTC/USDT:USDT"})
    return s.populate_exit_trend(f, {"pair": "BTC/USDT:USDT"})


@check("[data] real BTC trades are identical when indicators are recomputed on data cut after each exit candle")
def _():
    import run_metalabel as rm
    full = populate(btc_frame(pd.Timestamp("2024-06-01", tz="UTC")))
    sims = rm.simulate_pair(full, None, start=pd.Timestamp("2023-01-01", tz="UTC"))
    assert len(sims) > 30
    for t in sims.loc[~sims.open_at_end].sample(12, random_state=1).itertuples():
        cut = populate(btc_frame(t.exit_candle + H))   # rows up to and including the exit candle
        again = rm.simulate_pair(cut, None, start=t.date)
        first = again.iloc[0]
        assert first.date == t.date and first.exit_candle == t.exit_candle and np.isclose(first.r, t.r), (t.date, first.exit_candle, t.exit_candle)


@check("[data] signal-row features equal features computed from data cut at the signal candle close")
def _():
    from features_c import indicator_features
    full = populate(btc_frame(pd.Timestamp("2024-06-01", tz="UTC")))
    feats = indicator_features(full)
    sig = full.index[(full.enter_long == 1) | (full.enter_short == 1)]
    for i in sig[-8:]:
        cut = populate(btc_frame(full.date.iat[i] + H))
        assert cut.date.iat[-1] == full.date.iat[i]
        assert (cut.enter_long.iat[-1] == full.enter_long.iat[i]) and (cut.enter_short.iat[-1] == full.enter_short.iat[i])
        pd.testing.assert_series_equal(indicator_features(cut).iloc[-1], feats.loc[i], check_names=False)


def phase_c_checks():
    """Import phase C's checks (they run at import) and relabel them."""
    try:
        import test_phase_c as tc
    except Exception:  # noqa: BLE001
        RESULTS.append(("phase C feature checks import", "FAIL", traceback.format_exc(limit=3)))
        return
    for name, status, detail in tc.RESULTS:
        RESULTS.append((f"[phase C] {name}", status, detail))


if __name__ == "__main__":
    phase_c_checks()
    for name, status, detail in RESULTS:
        print(f"{status:4} {name}" + (f"\n{detail}" if detail else ""))
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} ok ({sum(r[1] == 'SKIP' for r in RESULTS)} skipped)")
    sys.exit(1 if failed else 0)
