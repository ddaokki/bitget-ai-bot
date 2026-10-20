"""Pure helpers for the meta-labeling study (no Freqtrade imports, unit-testable).

simulate_trade  replays the SafeTrendV0 base-profile trade for ONE entry signal row,
                independently of any portfolio (2xATR initial stop, 3xATR chandelier
                trail from CLOSED 1h candles only, opposite Donchian-10 exit at the next
                open, gap-aware stop fills, 0.06%/side fees, both-sides 0.01%/8h funding).
usable_for      variable-horizon purge: a row may train a model for a prediction
                window starting at ws only if its trade's exit time < ws.
portfolio       1 position per coin, max 2 concurrent, 1% risk, 2x leverage, compounding
                closed-trade balance. On the entry candle T only exits that fill at T's open
                (opposite Donchian) are settled first; a stop hit inside candle T is not yet
                known at T's open and frees its slot / books its PnL from T+1 on.
block_bootstrap resample whole blocks (weeks, months, model windows) for a statistic's CI.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HOUR = pd.Timedelta(hours=1)
FEE = 0.0006
FUNDING_RATE = 0.0001
FUNDING_HOURS = (0, 8, 16)
STOP_ATR = 2.0
TRAIL_ATR = 3.0
RISK = 0.01
LEVERAGE = 2.0
MIN_SIZE_TOLERANCE = 1.5
WALLET = 105.0


def funding_crossings(open_date: pd.Timestamp, close_date: pd.Timestamp) -> pd.DatetimeIndex:
    """Settlement times T with open_date < T <= close_date (same rule as phase_b_lib)."""
    start = pd.Timestamp(open_date).floor("8h")
    grid = pd.date_range(start, pd.Timestamp(close_date), freq="8h")
    return grid[(grid.hour.isin(FUNDING_HOURS)) & (grid > open_date) & (grid <= close_date)]


def simulate_trade(dates: np.ndarray, o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray,
                   atr: np.ndarray, exit_sig: np.ndarray, i: int, side: int,
                   mark_open: pd.Series | None = None, stop_atr: float = STOP_ATR, trail_atr: float = TRAIL_ATR,
                   fee: float = FEE, funding_rate: float = FUNDING_RATE, mode: str = "spec") -> dict | None:
    """Trade for the entry signal on row i (side +1 long / -1 short).

    Row arrays are aligned 1h candles (open time `dates`). `atr` is the 4h ATR as known at
    each row's close, `exit_sig` the opposite-Donchian exit flag for THIS side on each row.
    Decisions on candle j use only rows < j, plus candle j's own open/high/low for the fill,
    which is what the Freqtrade engine sees. Returns None when the trade cannot be opened.

    mode "spec" (pre-registered): every new stop level applies; if the candle opens beyond
    it the fill is the worse open. mode "engine": Freqtrade's backtest stop handling.
    """
    n = len(o)
    if i + 1 >= n or not np.isfinite(atr[i]) or atr[i] <= 0:
        return None
    entry_atr = float(atr[i])
    entry = float(o[i + 1])
    stop = entry - side * stop_atr * entry_atr
    applied = entry * (1 - side * 0.25)   # Freqtrade initial stop: stoploss -0.50 at 2x leverage
    best = None
    exit_j, exit_price, reason = None, None, None
    for j in range(i + 1, n):
        if j > i + 1:
            # Chandelier from closed candles i+1 .. j-1 and the latest closed 4h ATR (row j-1).
            ext = h[j - 1] if side == 1 else lo[j - 1]
            best = ext if best is None else (max(best, ext) if side == 1 else min(best, ext))
            if trail_atr and np.isfinite(atr[j - 1]):
                trail = best - side * trail_atr * float(atr[j - 1])
                stop = max(stop, trail) if side == 1 else min(stop, trail)   # ratchet
        if exit_sig[j - 1]:
            exit_j, exit_price, reason = j, float(o[j]), "opposite_donchian_10"
            break
        if mode == "engine":
            # Freqtrade 2026.9 backtest rules (sensitivity, added after the engine check):
            # the custom stop is consulted only while the applied stop is not hit by this
            # candle (dir_correct), and a requested stop beyond the candle bound (high for
            # long, low for short) makes stoploss_from_absolute return 0, which is ignored.
            hit_old = (lo[j] <= applied) if side == 1 else (h[j] >= applied)
            if not hit_old:
                if side == 1 and stop < h[j]:
                    applied = max(applied, stop)
                elif side == -1 and stop > lo[j]:
                    applied = min(applied, stop)
            if side == 1 and lo[j] <= applied:
                exit_j, exit_price, reason = j, float(o[j] if applied > h[j] else applied), "stop"
                break
            if side == -1 and h[j] >= applied:
                exit_j, exit_price, reason = j, float(o[j] if applied < lo[j] else applied), "stop"
                break
            continue
        if side == 1 and lo[j] <= stop:
            exit_j, exit_price, reason = j, float(min(stop, o[j])), "stop"
            break
        if side == -1 and h[j] >= stop:
            exit_j, exit_price, reason = j, float(max(stop, o[j])), "stop"
            break
    if exit_j is None:
        exit_j, exit_price, reason = n - 1, float(c[n - 1]), "open_at_end"
    open_date = pd.Timestamp(dates[i + 1])
    exit_candle = pd.Timestamp(dates[exit_j])
    fund = 0.0
    crossings = funding_crossings(open_date, exit_candle)
    for ts in crossings:
        price = None
        if mark_open is not None:
            price = mark_open.get(ts)
            if price is None or not np.isfinite(price):
                prior = mark_open.loc[:ts]
                price = float(prior.iloc[-1]) if len(prior) else None
        fund += funding_rate * float(price if price is not None else entry)
    gross = side * (exit_price - entry)
    fees = fee * (entry + exit_price)
    risk = stop_atr * entry_atr
    return {
        "entry_time": open_date, "entry": entry, "atr": entry_atr, "exit_candle": exit_candle,
        "exit_time": exit_candle + HOUR,           # outcome fully known at the exit candle's close
        "exit_price": exit_price, "exit_reason": reason, "open_at_end": reason == "open_at_end",
        "bars_held": exit_j - i, "funding_per_unit": fund, "fees_per_unit": fees,
        "r_gross": gross / risk, "r": (gross - fees - fund) / risk,
    }


def usable_for(exit_time: pd.Series, open_at_end: pd.Series, signal_date: pd.Series,
               train_start: pd.Timestamp, ws: pd.Timestamp) -> pd.Series:
    """Variable-horizon purge: signal in [train_start, ws) AND trade exit known before ws."""
    return (signal_date >= train_start) & (signal_date < ws) & (exit_time < ws) & ~open_at_end.astype(bool)


def portfolio(trades: pd.DataFrame, pair_order: list[str], start: pd.Timestamp, end: pd.Timestamp,
              wallet: float = WALLET, risk: float = RISK, leverage: float = LEVERAGE, max_open: int = 2,
              min_tolerance: float = MIN_SIZE_TOLERANCE) -> tuple[pd.DataFrame, pd.Series]:
    """Replay chosen signals as a 2-slot portfolio.

    trades: one row per signal to TAKE, with pair, entry_time, exit_candle, exit_reason, entry,
    exit_price, atr, side, funding_per_unit, min_cost, min_amount. Returns (taken trades with pnl,
    closed-trade balance indexed by exit candle).
    """
    order = {p: k for k, p in enumerate(pair_order)}
    cand = trades.loc[(trades.entry_time >= start) & (trades.entry_time < end)].copy()
    cand["_ord"] = cand.pair.map(order)
    cand = cand.sort_values(["entry_time", "_ord"])
    balance = wallet
    open_pos: list[dict] = []
    taken, curve = [], [(pd.Timestamp(start), wallet)]

    def known_by(pos, until) -> bool:
        # An exit at the open of candle X is known at X; a stop inside candle X only at X's close.
        if pos["exit_reason"] == "opposite_donchian_10":
            return pos["exit_candle"] <= until
        return pos["exit_candle"] < until

    def settle(until):
        nonlocal balance, open_pos
        keep = []
        for pos in sorted(open_pos, key=lambda x: x["exit_candle"]):
            if known_by(pos, until):
                balance += pos["pnl"]
                curve.append((pos["exit_candle"], balance))
            else:
                keep.append(pos)
        open_pos = keep

    for t in cand.itertuples():
        settle(t.entry_time)
        if len(open_pos) >= max_open or any(p["pair"] == t.pair for p in open_pos):
            continue
        rate, atr = float(t.entry), float(t.atr)
        target_margin = balance * risk / ((STOP_ATR * atr / rate) * leverage)
        min_margin = max(float(t.min_amount) * rate, float(t.min_cost)) / leverage
        margin = max(target_margin, min_margin)
        free = balance - sum(p["margin"] for p in open_pos)
        if margin > target_margin * min_tolerance or margin > free or margin <= 0:
            continue
        amount = margin * leverage / rate
        pnl = amount * (t.side * (t.exit_price - rate) - FEE * (rate + t.exit_price) - t.funding_per_unit)
        pos = {"pair": t.pair, "exit_candle": t.exit_candle, "exit_reason": t.exit_reason, "margin": margin, "pnl": pnl}
        open_pos.append(pos)
        taken.append({"pair": t.pair, "signal_date": t.date, "entry_time": t.entry_time, "exit_candle": t.exit_candle,
                      "exit_reason": t.exit_reason, "side": t.side, "margin": margin, "pnl": pnl, "r": t.r})
    settle(pd.Timestamp.max.tz_localize("UTC"))
    curve_s = pd.Series([b for _, b in curve], index=pd.DatetimeIndex([t for t, _ in curve]))
    curve_s = curve_s.iloc[np.argsort(curve_s.index.to_numpy(), kind="stable")]
    return pd.DataFrame(taken), curve_s


def block_bootstrap(frame: pd.DataFrame, stat, blocks: pd.Series, draws: int = 1000, seed: int = 81,
                    level: float = 0.90) -> tuple[float, float]:
    """CI of stat(frame_sample) resampling whole blocks (rows sharing a `blocks` key) with replacement."""
    rng = np.random.default_rng(seed)
    codes, _ = pd.factorize(blocks.to_numpy())
    groups = [np.flatnonzero(codes == k) for k in range(codes.max() + 1)]
    stats = []
    for _ in range(draws):
        idx = np.concatenate([groups[k] for k in rng.integers(0, len(groups), len(groups))])
        stats.append(stat(frame.iloc[idx]))
    tail = (1 - level) / 2 * 100
    lo, hi = np.nanpercentile(np.asarray(stats, dtype=float), [tail, 100 - tail])
    return float(lo), float(hi)


def period_return(curve: pd.Series, start, end, wallet: float = WALLET) -> float:
    """% change of the closed-trade balance between two instants (exits at t < bound count)."""
    before = curve.loc[curve.index < pd.Timestamp(start)]
    first = float(before.iloc[-1]) if len(before) else wallet
    upto = curve.loc[curve.index < pd.Timestamp(end)]
    last = float(upto.iloc[-1]) if len(upto) else first
    return (last / first - 1) * 100


def max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    peak = curve.cummax()
    return float(((peak - curve) / peak).max() * 100)
