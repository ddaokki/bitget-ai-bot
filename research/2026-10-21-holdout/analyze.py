"""Holdout analysis for SafeTrendLive (run inside the Freqtrade image):

  python -P /research/analyze.py

Reads the exported trades of each backtest run listed in RUNS and writes
results.json next to this file. All money is USDT on a 105 USDT wallet.

Metrics per span (total, calendar year, half-year; a trade belongs to the span of
its OPEN date): trades, win rate, net USDT and % of the equity at span start,
profit factor, max drawdown of the closed-trade equity curve, worst trade,
longest losing streak, exposure (share of the span with a position open), BTC
(or the traded coin) buy-and-hold over the same span, and a conservative funding
charge: the position pays 0.01% of its entry notional at every 00/08/16 UTC
funding time it is open across (both sides pay; the backtests ran with zero funding).
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import talib.abstract as ta
from freqtrade.data.btanalysis import load_backtest_data

ROOT = Path(__file__).resolve().parent
USER = Path("/freqtrade/user_data")
DATA = USER / "data" / "bitget_synth_funding_zero" / "futures"
RESULTS = USER / "backtest_results"
WALLET = 105.0
FUNDING_PER_8H = 0.0001
STARTUP_4H_BARS = 14 + 180     # ATR(14) then the 180-bar ATR% quantile (shifted by one bar)

# name -> (backtest result prefix, pair, timerange start, timerange end exclusive)
RUNS = {
    "BTC_holdout_repaired": ("holdout_btc_rep", "BTC/USDT:USDT", "2020-01-01", "2023-10-01"),
    "BTC_holdout_raw": ("holdout_btc", "BTC/USDT:USDT", "2020-01-01", "2023-10-01"),
    "BTC_holdout_repaired_protections": ("holdout_btc_prot", "BTC/USDT:USDT", "2020-01-01", "2023-10-01"),
    "ETH_holdout": ("holdout_eth", "ETH/USDT:USDT", "2020-01-01", "2023-10-01"),
    "SOL_holdout": ("holdout_sol", "SOL/USDT:USDT", "2021-09-01", "2023-10-01"),
    "BTC_insample_rerun": ("holdout_insample_btc", "BTC/USDT:USDT", "2023-10-01", "2026-10-20"),
    "BTC_insample_protections": ("holdout_insample_btc_prot", "BTC/USDT:USDT", "2023-10-01", "2026-10-20"),
    "ETH_insample": ("holdout_insample_eth", "ETH/USDT:USDT", "2023-10-01", "2026-10-20"),
    "SOL_insample": ("holdout_insample_sol", "SOL/USDT:USDT", "2023-10-01", "2026-10-20"),
}


def candles(pair: str, timeframe: str) -> pd.DataFrame:
    path = DATA / (pair.replace("/", "_").replace(":", "_") + f"-{timeframe}-futures.feather")
    frame = pd.read_feather(path)
    frame["date"] = pd.to_datetime(frame.date, utc=True)
    return frame.sort_values("date").reset_index(drop=True)


def first_entry_possible(pair: str) -> dict:
    """First 1h candle on which every entry indicator is defined (EMA200 daily, ATR% q90 4h)."""
    daily = candles(pair, "1d")
    daily["ema200"] = ta.EMA(daily, timeperiod=200)
    daily["ema50"] = ta.EMA(daily, timeperiod=50)
    ema_day = daily.loc[daily.ema200.notna(), "date"].min()
    # A daily value is usable after its day has closed.
    ema_ready = ema_day + pd.Timedelta(days=1)
    four = candles(pair, "4h")
    q90_ready = four.date.iloc[STARTUP_4H_BARS] + pd.Timedelta(hours=4)
    trend = daily.loc[daily.ema200.notna() & (daily.ema50 > daily.ema200), "date"].min()
    return {"daily_first_utc": str(daily.date.min()), "fourh_first_utc": str(four.date.min()),
            "ema200_ready_utc": str(ema_ready), "atr_q90_ready_utc": str(q90_ready),
            "first_entry_possible_utc": str(max(ema_ready, q90_ready)),
            "first_uptrend_day_utc": str(trend + pd.Timedelta(days=1)) if pd.notna(trend) else None}


def funding_crossings(open_date: pd.Timestamp, close_date: pd.Timestamp) -> int:
    """Number of 00/08/16 UTC funding times t with open_date < t <= close_date."""
    start = open_date.floor("8h")
    times = pd.date_range(start, close_date, freq="8h")
    return int(((times > open_date) & (times <= close_date)).sum())


def max_drawdown(start_equity: float, profits: np.ndarray) -> tuple[float, float]:
    equity = start_equity + np.concatenate([[0.0], np.cumsum(profits)])
    peak = np.maximum.accumulate(equity)
    dd_abs = peak - equity
    dd_pct = dd_abs / peak
    return float(dd_abs.max()), float(dd_pct.max())


def longest_losing_streak(profits: np.ndarray) -> int:
    best = run = 0
    for value in profits:
        run = run + 1 if value < 0 else 0
        best = max(best, run)
    return best


def exposure(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    total = 0.0
    for row in trades.itertuples():
        lo, hi = max(row.open_date, start), min(row.close_date, end)
        total += max((hi - lo).total_seconds(), 0.0)
    return total / (end - start).total_seconds()


def buy_and_hold(hourly: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float | None:
    inside = hourly.loc[(hourly.date >= start) & (hourly.date < end)]
    if inside.empty:
        return None
    return float(inside.close.iloc[-1] / inside.open.iloc[0] - 1)


def span_stats(trades: pd.DataFrame, start_equity: float, start: pd.Timestamp, end: pd.Timestamp,
               hourly: pd.DataFrame) -> dict:
    profits = trades.profit_abs.to_numpy(dtype=float)
    net_funding = (trades.profit_abs - trades.funding_cost).to_numpy(dtype=float)
    wins, losses = profits[profits > 0].sum(), -profits[profits < 0].sum()
    dd_abs, dd_pct = max_drawdown(start_equity, profits)
    dd_abs_f, dd_pct_f = max_drawdown(start_equity, net_funding)
    worst = trades.loc[trades.profit_abs.idxmin()] if len(trades) else None
    return {
        "start_utc": str(start), "end_utc": str(end), "start_equity": round(start_equity, 4),
        "trades": int(len(trades)),
        "win_rate": round(float((profits > 0).mean()), 4) if len(trades) else None,
        "net_usdt": round(float(profits.sum()), 4),
        "net_pct": round(float(profits.sum() / start_equity * 100), 3),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else (None if wins == 0 else "inf"),
        "max_dd_usdt": round(dd_abs, 4), "max_dd_pct": round(dd_pct * 100, 3),
        "worst_trade_usdt": round(float(worst.profit_abs), 4) if worst is not None else None,
        "worst_trade_open_utc": str(worst.open_date) if worst is not None else None,
        "longest_losing_streak": longest_losing_streak(profits),
        "exposure_pct": round(exposure(trades, start, end) * 100, 2),
        "buy_hold_pct": None if (bh := buy_and_hold(hourly, start, end)) is None else round(bh * 100, 2),
        "funding_cost_usdt": round(float(trades.funding_cost.sum()), 4),
        "net_after_funding_usdt": round(float(net_funding.sum()), 4),
        "net_after_funding_pct": round(float(net_funding.sum() / start_equity * 100), 3),
        "max_dd_after_funding_pct": round(dd_pct_f * 100, 3),
        "exit_reasons": {str(k): int(v) for k, v in trades.exit_reason.value_counts().items()},
        "mean_trade_usdt": round(float(profits.mean()), 4) if len(trades) else None,
        # t statistic of the mean trade P&L (no fees/funding adjustment beyond the backtest's own fees)
        "t_stat_mean_trade": round(float(profits.mean() / (profits.std(ddof=1) / np.sqrt(len(profits)))), 3) if len(trades) > 2 else None,
        "net_without_best3_usdt": round(float(np.sort(profits)[:-3].sum()), 4) if len(trades) > 3 else None,
    }


def periods(start: pd.Timestamp, end: pd.Timestamp, half: bool) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """Calendar years (or half-years) clipped to [start, end)."""
    out = []
    for year in range(start.year, end.year + 1):
        parts = [(f"{year}H1", 1, 7), (f"{year}H2", 7, 13)] if half else [(str(year), 1, 13)]
        for label, m0, m1 in parts:
            lo = pd.Timestamp(year=year, month=m0, day=1, tz="UTC")
            hi = pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC") if m1 == 13 else pd.Timestamp(year=year, month=m1, day=1, tz="UTC")
            lo, hi = max(lo, start), min(hi, end)
            if lo < hi:
                out.append((label, lo, hi))
    return out


def load_trades(directory: str) -> pd.DataFrame | None:
    # This Freqtrade version treats --backtest-directory <name> as a file prefix inside
    # backtest_results/ (shared .last_result.json), so pick the newest zip with that exact prefix.
    zips = sorted(RESULTS.glob(f"{directory}-20*.zip"))
    if not zips:
        return None
    trades = load_backtest_data(zips[-1])
    if trades.empty:
        columns = ["open_date", "close_date", "profit_abs", "amount", "open_rate", "stake_amount", "exit_reason",
                   "funding_crossings", "funding_cost"]
        return pd.DataFrame({c: pd.Series(dtype="datetime64[ns, UTC]" if c.endswith("_date") else "float64") for c in columns})
    trades["open_date"] = pd.to_datetime(trades.open_date, utc=True)
    trades["close_date"] = pd.to_datetime(trades.close_date, utc=True)
    trades = trades.sort_values("close_date").reset_index(drop=True)
    notional = trades.amount * trades.open_rate
    trades["funding_crossings"] = [funding_crossings(o, c) for o, c in zip(trades.open_date, trades.close_date)]
    trades["funding_cost"] = notional * FUNDING_PER_8H * trades.funding_crossings
    return trades


def analyse_run(directory: str, pair: str, start: str, end: str) -> dict | None:
    trades = load_trades(directory)
    if trades is None:
        return None
    start, end = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    # Buy-and-hold uses 4h candles: Bitget's 1h BTC last-price history has a frozen run in Dec 2020.
    hourly = candles(pair, "4h")
    ready = first_entry_possible(pair)
    first_entry = max(pd.Timestamp(ready["first_entry_possible_utc"]), start)
    result = {"pair": pair, "timerange": [str(start), str(end)], "readiness": ready,
              "total": span_stats(trades, WALLET, start, end, hourly),
              "total_from_first_possible_entry": span_stats(trades, WALLET, first_entry, end, hourly),
              "first_trade_open_utc": str(trades.open_date.min()) if len(trades) else None,
              "open_at_end": int((trades.exit_reason == "force_exit").sum()),
              "mean_hold_hours": round(float((trades.close_date - trades.open_date).dt.total_seconds().mean() / 3600), 2) if len(trades) else None,
              "mean_notional_usdt": round(float((trades.amount * trades.open_rate).mean()), 3) if len(trades) else None,
              "mean_stake_usdt": round(float(trades.stake_amount.mean()), 3) if len(trades) else None}
    for key, half in (("by_year", False), ("by_half", True)):
        rows = {}
        for label, lo, hi in periods(start, end, half):
            mask = (trades.open_date >= lo) & (trades.open_date < hi)
            before = trades.loc[trades.close_date < lo, "profit_abs"].sum()
            rows[label] = span_stats(trades.loc[mask], WALLET + float(before), lo, hi, hourly)
        result[key] = rows
    return result


def main() -> None:
    out = {"wallet_usdt": WALLET, "funding_model": "both sides pay 0.01% of entry notional per 00/08/16 UTC crossing",
           "fee_per_side": 0.0006, "runs": {}}
    for name, (directory, pair, start, end) in RUNS.items():
        result = analyse_run(directory, pair, start, end)
        if result is not None:
            out["runs"][name] = result
    runs = out["runs"]
    if "BTC_holdout_repaired" in runs and "BTC_insample_rerun" in runs:
        hold, ins = runs["BTC_holdout_repaired"], runs["BTC_insample_rerun"]
        keys = ("trades", "win_rate", "net_usdt", "net_pct", "profit_factor", "max_dd_pct", "net_after_funding_usdt",
                "net_after_funding_pct", "exposure_pct", "buy_hold_pct", "t_stat_mean_trade", "net_without_best3_usdt")
        out["headline"] = {
            "btc_holdout_2020_01_01_to_2023_09_30": {k: hold["total"][k] for k in keys},
            "btc_insample_2023_10_01_to_2026_10_19": {k: ins["total"][k] for k in keys},
            "btc_holdout_without_2020H2": {
                "trades": hold["total"]["trades"] - hold["by_half"]["2020H2"]["trades"],
                "net_usdt": round(hold["total"]["net_usdt"] - hold["by_half"]["2020H2"]["net_usdt"], 4),
                "net_after_funding_usdt": round(hold["total"]["net_after_funding_usdt"] - hold["by_half"]["2020H2"]["net_after_funding_usdt"], 4)},
        }
    (ROOT / "results.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    for name, result in out["runs"].items():
        t = result["total"]
        print(name, t["trades"], t["win_rate"], t["net_usdt"], t["net_pct"], t["profit_factor"], t["max_dd_pct"],
              t["net_after_funding_usdt"], t["exposure_pct"], t["buy_hold_pct"])


if __name__ == "__main__":
    main()
