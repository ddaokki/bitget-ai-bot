"""Phase C features: phase B indicators + Binance positioning (funding, open interest,
long/short ratios, taker flow), point-in-time, no lookahead.

Timing rule (same as FreqAI/phase B): a feature on the 1h row with open time t is what
was known at that candle's CLOSE, as_of = t + 1h. Positioning snapshots and funding
settlements count only if their timestamp is STRICTLY before as_of (a 5-minute snapshot
stamped exactly at the close is treated as not yet published). Every rolling window is
backward-looking, so features never change when later data is appended
(test_phase_c.py checks this prefix invariance).

Pre-registered feature list (fixed before any phase C result was seen):
  open interest  oi_chg_4h, oi_chg_24h, oi_chg_72h   log change of OI in coins
                 oi_z_30d                            z-score of log OI vs the last 30 days
  long/short     ls_top_acct, ls_top_pos, ls_global  log of top-trader account/position and
                                                      global account long/short ratios
                 ls_global_z_30d, ls_top_pos_z_30d    z-scores vs the last 30 days
                 ls_global_chg_24h                    24h change of the global log ratio
  taker flow     taker_4h, taker_24h                  mean log taker buy/sell volume ratio
  funding        fund_last, fund_mean_3d, fund_z_30d  last settled rate (per 8h), 3-day mean,
                                                      z-score vs the last 30 days
  market (BTC)   btc_oi_chg_24h, btc_fund_last, btc_ls_global   same for BTC, on every coin
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HOUR = pd.Timedelta(hours=1)
GRID_START = pd.Timestamp("2022-01-01 01:00", tz="UTC")
Z_WINDOW = 720          # 30 days of hourly samples
Z_MIN = 360
METRIC_TOLERANCE = pd.Timedelta(hours=2)
FUNDING_TOLERANCE = pd.Timedelta(hours=9)

OWN_FEATURES = [
    "oi_chg_4h", "oi_chg_24h", "oi_chg_72h", "oi_z_30d",
    "ls_top_acct", "ls_top_pos", "ls_global", "ls_global_z_30d", "ls_top_pos_z_30d", "ls_global_chg_24h",
    "taker_4h", "taker_24h",
    "fund_last", "fund_mean_3d", "fund_z_30d",
]
MARKET_FEATURES = ["btc_oi_chg_24h", "btc_fund_last", "btc_ls_global"]
DERIV_FEATURES = OWN_FEATURES + MARKET_FEATURES

# Phase B indicator features (SafeTrendAIV0.feature_engineering_standard, event part off).
INDICATOR_FEATURES = [
    "return_1h", "return_24h", "return_72h", "volume_ratio", "atr_pct", "atr_rank180", "adx", "rsi_4h",
    "daily_trend", "dist_ema200_atr", "channel_position", "channel_width_atr", "trend_side",
]


def hourly_grid(end: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(GRID_START, pd.Timestamp(end), freq="h")


def _asof(frame: pd.DataFrame, grid: pd.DatetimeIndex, tolerance: pd.Timedelta) -> pd.DataFrame:
    """Last row with frame.date < as_of for every as_of in grid (strict), NaN past `tolerance`."""
    left = pd.DataFrame({"as_of": grid.astype("datetime64[ns, UTC]")})
    right = frame.rename(columns={"date": "_ts"}).sort_values("_ts")
    right["_ts"] = right["_ts"].astype("datetime64[ns, UTC]")   # feather files mix ms/us units
    out = pd.merge_asof(left, right, left_on="as_of", right_on="_ts", direction="backward",
                        allow_exact_matches=False, tolerance=tolerance)
    return out.drop(columns=["_ts"]).set_index("as_of")


def _z(series: pd.Series) -> pd.Series:
    mean = series.rolling(Z_WINDOW, min_periods=Z_MIN).mean()
    std = series.rolling(Z_WINDOW, min_periods=Z_MIN).std()
    return (series - mean) / std.replace(0, np.nan)


def _log(series: pd.Series) -> pd.Series:
    return np.log(series.where(series > 0))


def metrics_hourly(metrics: pd.DataFrame, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Open interest / long-short / taker features on the hourly as_of grid."""
    m = metrics.sort_values("date").set_index("date")
    taker = _log(m.sum_taker_long_short_vol_ratio)
    # Time-based windows (t-4h, t] on the 5-minute series: only snapshots up to t.
    m = m.assign(taker_4h=taker.rolling("4h", min_periods=24).mean(),
                 taker_24h=taker.rolling("24h", min_periods=144).mean())
    h = _asof(m.reset_index(), grid, METRIC_TOLERANCE)
    log_oi = _log(h.sum_open_interest)
    out = pd.DataFrame(index=grid)
    out["oi_chg_4h"] = log_oi - log_oi.shift(4)
    out["oi_chg_24h"] = log_oi - log_oi.shift(24)
    out["oi_chg_72h"] = log_oi - log_oi.shift(72)
    out["oi_z_30d"] = _z(log_oi)
    out["ls_top_acct"] = _log(h.count_toptrader_long_short_ratio)
    out["ls_top_pos"] = _log(h.sum_toptrader_long_short_ratio)
    out["ls_global"] = _log(h.count_long_short_ratio)
    out["ls_global_z_30d"] = _z(out.ls_global)
    out["ls_top_pos_z_30d"] = _z(out.ls_top_pos)
    out["ls_global_chg_24h"] = out.ls_global - out.ls_global.shift(24)
    out["taker_4h"] = h.taker_4h
    out["taker_24h"] = h.taker_24h
    return out


def funding_hourly(funding: pd.DataFrame, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Settled funding (normalised to an 8h rate) as known at each as_of."""
    f = funding.sort_values("date").reset_index(drop=True)
    interval = f.date.diff().dt.total_seconds().div(3600).fillna(8).clip(1, 8)
    f = f.assign(rate8=f.funding_rate * 8 / interval).set_index("date")
    f["fund_mean_3d"] = f.rate8.rolling("3D").mean()
    mean30 = f.rate8.rolling("30D", min_periods=30).mean()
    std30 = f.rate8.rolling("30D", min_periods=30).std()
    f["fund_z_30d"] = (f.rate8 - mean30) / std30.replace(0, np.nan)
    h = _asof(f.reset_index()[["date", "rate8", "fund_mean_3d", "fund_z_30d"]], grid, FUNDING_TOLERANCE)
    return pd.DataFrame({"fund_last": h.rate8, "fund_mean_3d": h.fund_mean_3d, "fund_z_30d": h.fund_z_30d}, index=grid)


def positioning_hourly(metrics: pd.DataFrame, funding: pd.DataFrame, end: pd.Timestamp) -> pd.DataFrame:
    grid = hourly_grid(end)
    return metrics_hourly(metrics, grid).join(funding_hourly(funding, grid))


def deriv_features(dates: pd.Series, own: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
    """Rows aligned to candle open times `dates`; values as of the candle close (date + 1h)."""
    as_of = pd.DatetimeIndex(pd.to_datetime(dates, utc=True)).as_unit("ns") + HOUR
    frame = own.reindex(as_of)[OWN_FEATURES].copy()
    frame["btc_oi_chg_24h"] = btc.reindex(as_of).oi_chg_24h.to_numpy()
    frame["btc_fund_last"] = btc.reindex(as_of).fund_last.to_numpy()
    frame["btc_ls_global"] = btc.reindex(as_of).ls_global.to_numpy()
    frame.index = dates.index if isinstance(dates, pd.Series) else pd.RangeIndex(len(frame))
    return frame[DERIV_FEATURES]


def indicator_features(dataframe: pd.DataFrame) -> pd.DataFrame:
    """The 13 phase B indicator features from a SafeTrendV0-populated frame (identical formulas)."""
    d = dataframe
    atr = d.atr_4h
    out = pd.DataFrame(index=d.index)
    out["return_1h"] = d.close.pct_change()
    out["return_24h"] = d.close.pct_change(24)
    out["return_72h"] = d.close.pct_change(72)
    out["volume_ratio"] = d.volume / d.volume.rolling(24).mean()
    out["atr_pct"] = d.atr_pct_4h
    out["atr_rank180"] = d.atr_rank_4h
    out["adx"] = d.adx_4h
    out["rsi_4h"] = d.rsi_4h
    out["daily_trend"] = d.ema50_1d / d.ema200_1d - 1
    out["dist_ema200_atr"] = (d.close - d.ema200_1d) / atr
    out["channel_position"] = (d.close - d.dc_low_4h) / (d.dc_high_4h - d.dc_low_4h)
    out["channel_width_atr"] = (d.dc_high_4h - d.dc_low_4h) / atr
    out["trend_side"] = np.where(d.ema50_1d >= d.ema200_1d, 1, -1)
    return out.replace([np.inf, -np.inf], np.nan)
