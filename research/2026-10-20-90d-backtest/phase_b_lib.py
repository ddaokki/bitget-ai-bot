"""Pure helpers for the phase B analysis (funding, MFE/MAE, mark-to-market equity).

Kept free of Freqtrade imports so test_phase_b.py can check them directly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FUNDING_RATE = 0.0001          # per 8h
FUNDING_HOURS = (0, 8, 16)     # Bitget 8h settlement times (UTC)
HOUR = pd.Timedelta(hours=1)


def funding_crossings(open_date: pd.Timestamp, close_date: pd.Timestamp) -> pd.DatetimeIndex:
    """Settlement times T with open_date < T <= close_date.

    An entry filled at a candle open just after 08:00:00 does not pay the 08:00
    settlement; a position still open at 08:00:00 (closed after it) does.
    """
    start = pd.Timestamp(open_date).floor("8h")
    grid = pd.date_range(start, pd.Timestamp(close_date), freq="8h")
    grid = grid[(grid.hour.isin(FUNDING_HOURS)) & (grid > open_date) & (grid <= close_date)]
    return grid


def entry_fills(orders: list[dict]) -> list[tuple[pd.Timestamp, float, float]]:
    """(time, amount, price) for every filled entry order, time-ordered."""
    fills = []
    for order in orders or []:
        if order.get("ft_is_entry") and order.get("amount"):
            ts = pd.Timestamp(order["order_filled_timestamp"], unit="ms", tz="UTC")
            fills.append((ts, float(order["amount"]), float(order["safe_price"])))
    return sorted(fills)


def amount_at(fills, when: pd.Timestamp) -> float:
    """Position size held at instant `when` (fills at or after `when` not yet included)."""
    return float(sum(a for t, a, _ in fills if t < when))


def funding_schedule(open_date, close_date, fills, mark_open: pd.Series) -> list[tuple[pd.Timestamp, float]]:
    """(settlement time, notional held) for every crossing; notional at the mark candle open."""
    out = []
    for ts in funding_crossings(open_date, close_date):
        amount = amount_at(fills, ts)
        if amount <= 0:
            continue
        price = mark_open.get(ts)
        if price is None or not np.isfinite(price):
            prior = mark_open.loc[:ts]
            price = float(prior.iloc[-1]) if len(prior) else np.nan
        out.append((ts, amount * float(price)))
    return out


def trade_funding(open_date, close_date, is_short: bool, fills, mark_open: pd.Series, rate: float = FUNDING_RATE) -> tuple[float, float, int]:
    """Funding COST in USDT under (conservative, phase-A signed) models, plus crossing count.

    conservative: both sides pay `rate` x notional at every crossing.
    signed (phase A): longs pay, shorts receive.
    """
    schedule = funding_schedule(open_date, close_date, fills, mark_open)
    notional = sum(n for _, n in schedule)
    return rate * notional, (-rate if is_short else rate) * notional, len(funding_crossings(open_date, close_date))


def clipped_extremes(candles: pd.DataFrame, open_date, close_date, close_rate: float) -> tuple[float, float]:
    """(highest, lowest) price seen while the trade was open, clipped at the exit.

    Candles that opened in [open_date, close_date) count fully. The exit candle
    (open time == close_date) counts only between its open and the exit price,
    because what happens after the exit inside that candle is not the trade's.
    """
    held = candles.loc[(candles.index >= open_date) & (candles.index < close_date)]
    highs = list(held.high.to_numpy(dtype=float))
    lows = list(held.low.to_numpy(dtype=float))
    if close_date in candles.index:
        exit_open = float(candles.loc[close_date, "open"])
        highs.append(max(exit_open, close_rate))
        lows.append(min(exit_open, close_rate))
    else:
        highs.append(close_rate)
        lows.append(close_rate)
    return max(highs), min(lows)


def mfe_mae_atr(candles: pd.DataFrame, open_date, close_date, open_rate: float, close_rate: float, is_short: bool, atr: float) -> tuple[float, float]:
    high, low = clipped_extremes(candles, open_date, close_date, close_rate)
    if is_short:
        return (open_rate - low) / atr, (high - open_rate) / atr
    return (high - open_rate) / atr, (open_rate - low) / atr


def trade_mtm(close: pd.Series, open_date, close_date, is_short: bool, fills, fee: float) -> pd.Series:
    """Unrealized PnL (after entry fees and an estimated exit fee) at each candle close while open.

    Indexed by candle open time t (value at t + 1h). Candles with open_date <= t < close_date.
    """
    idx = close.loc[(close.index >= open_date) & (close.index < close_date)].index
    out = pd.Series(0.0, index=idx)
    sign = -1.0 if is_short else 1.0
    for t in idx:
        held = [(a, p) for ft, a, p in fills if ft <= t]
        amount = sum(a for a, _ in held)
        if amount <= 0:
            continue
        cost = sum(a * p for a, p in held)
        avg = cost / amount
        price = float(close.loc[t])
        out.loc[t] = sign * amount * (price - avg) - fee * cost - fee * amount * price
    return out


def equity_curve(trades: pd.DataFrame, closes: dict[str, pd.Series], start_balance: float, start, end) -> pd.Series:
    """Hourly mark-to-market equity, indexed by evaluation time (candle open + 1h).

    trades needs: pair, open_date, close_date, is_short, fills, fee_open, profit_abs,
    funding_cost (charged at its crossing times via funding_times/funding_amounts).
    """
    grid = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="h", inclusive="left")
    equity = pd.Series(float(start_balance), index=grid)
    for t in trades.itertuples():
        close = closes[t.pair]
        mtm = trade_mtm(close, t.open_date, t.close_date, bool(t.is_short), t.fills, float(t.fee_open))
        equity = equity.add(mtm.reindex(grid).fillna(0.0), fill_value=0.0)
        realized = pd.Series(0.0, index=grid)
        realized.loc[grid >= t.close_date] = float(t.profit_abs)
        equity = equity.add(realized)
        for ts, cost in zip(t.funding_times, t.funding_costs, strict=True):
            # A settlement at ts is visible in equity evaluated at time >= ts, i.e. candle open >= ts - 1h.
            equity.loc[grid >= ts - HOUR] -= cost
    equity.index = equity.index + HOUR
    return equity


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((peak - equity) / peak).max() * 100)


def period_return_pct(equity: pd.Series, start_balance: float, start, end) -> float:
    """Equity change between two instants (evaluation-time index) in % of equity at `start`."""
    before = equity.loc[:pd.Timestamp(start)]
    first = float(before.iloc[-1]) if len(before) else float(start_balance)
    after = equity.loc[:pd.Timestamp(end)]
    last = float(after.iloc[-1]) if len(after) else first
    return (last / first - 1) * 100


def reliability(p: np.ndarray, hit: np.ndarray, edges=(0, 0.3, 0.45, 0.55, 0.65, 1.0001)) -> tuple[pd.DataFrame, float]:
    """Bucketed predicted vs realised rate and the count-weighted mean absolute gap."""
    frame = pd.DataFrame({"p": np.asarray(p, dtype=float), "hit": np.asarray(hit, dtype=float)})
    frame["bucket"] = pd.cut(frame.p, list(edges), right=False)
    table = frame.groupby("bucket", observed=True).agg(rows=("hit", "size"), mean_p=("p", "mean"), realised=("hit", "mean")).reset_index()
    table["bucket"] = table.bucket.astype(str)
    gap = float((table.rows * (table.mean_p - table.realised).abs()).sum() / table.rows.sum()) if len(table) else np.nan
    return table, gap
