"""Compare the exit variants (run inside the Freqtrade image):

  python -P /variants/analyze.py

Reads backtest_results/exitvar_<span>_<strategy> and writes results.json next to this file.
Same conventions as the 2026-10-21 holdout study: 105 USDT wallet, 0.06% fee per side already
in the backtest, funding charged afterwards at 0.01% of entry notional per 00/08/16 UTC crossing
(both sides pay, conservative), drawdown on the closed-trade equity curve.
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from freqtrade.data.btanalysis import load_backtest_data

ROOT = Path(__file__).resolve().parent
RESULTS = Path("/freqtrade/user_data/backtest_results")
WALLET = 105.0
FUNDING_PER_8H = 0.0001
STRATEGIES = ["SafeTrendLive", "SafeTrendBaselineCopy", "SafeTrendTrail25", "SafeTrendBreakeven", "SafeTrendPartial"]
SPANS = {"is": "in-sample 2023-10~2026-10", "ho": "holdout 2020-01~2023-09"}


def funding_crossings(opened: pd.Timestamp, closed: pd.Timestamp) -> int:
    start = opened.floor("8h") + pd.Timedelta(hours=8)
    if start > closed:
        return 0
    return int((closed - start) // pd.Timedelta(hours=8)) + 1


def entry_amount(row) -> float:
    orders = getattr(row, "orders", None) or []
    entries = [o for o in orders if o.get("ft_is_entry")]
    return float(entries[0].get("amount") or row["amount"]) if entries else float(row["amount"])


def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0}
    trades = trades.sort_values("close_date").reset_index(drop=True)
    profit = trades.profit_abs.astype(float)
    funding = [FUNDING_PER_8H * float(r.open_rate) * entry_amount(r)
               * funding_crossings(pd.Timestamp(r.open_date), pd.Timestamp(r.close_date)) for r in trades.itertuples()]
    equity = WALLET + profit.cumsum()
    peak = np.maximum.accumulate(np.concatenate([[WALLET], equity.values]))[1:]
    drawdown = float(((peak - equity) / peak).max() * 100)
    wins, losses = profit[profit > 0], profit[profit <= 0]
    streak = longest = 0
    for value in profit:
        streak = streak + 1 if value <= 0 else 0
        longest = max(longest, streak)
    partial_trades = 0
    for orders in trades.get("orders", pd.Series([[]] * len(trades))):
        exits = [o for o in (orders or []) if not o.get("ft_is_entry")]
        if len(exits) > 1:
            partial_trades += 1
    # How much of the best open profit the exit kept (price based, long only).
    mfe = (trades.max_rate.astype(float) / trades.open_rate.astype(float) - 1) * 100
    realized = (trades.close_rate.astype(float) / trades.open_rate.astype(float) - 1) * 100
    by_year = {}
    for year, group in trades.groupby(pd.to_datetime(trades.open_date).dt.year):
        by_year[str(year)] = {"trades": int(len(group)), "usdt": round(float(group.profit_abs.sum()), 3)}
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    per_trade = profit.values
    t_stat = float(per_trade.mean() / (per_trade.std(ddof=1) / math.sqrt(len(per_trade)))) if len(per_trade) > 2 else None
    return {
        "trades": int(len(trades)),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "usdt": round(float(profit.sum()), 3),
        "pct": round(float(profit.sum()) / WALLET * 100, 2),
        "usdt_after_funding": round(float(profit.sum()) - sum(funding), 3),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_dd_pct": round(drawdown, 2),
        "worst": round(float(profit.min()), 3),
        "best": round(float(profit.max()), 3),
        "avg_win": round(float(wins.mean()), 3) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 3) if len(losses) else None,
        "longest_losing_streak": int(longest),
        "t_stat": round(t_stat, 2) if t_stat is not None else None,
        "partial_exit_trades": partial_trades,
        "avg_mfe_pct": round(float(mfe.mean()), 2),
        "avg_realized_price_pct": round(float(realized.mean()), 2),
        "exit_reasons": {k: int(v) for k, v in trades.exit_reason.value_counts().items()},
        "by_year": by_year,
    }


def main() -> None:
    out = {"spans": SPANS, "runs": {}}
    for span in SPANS:
        for strategy in STRATEGIES:
            # Freqtrade writes <backtest-directory>-<timestamp>.zip; take the newest run.
            runs = sorted(RESULTS.glob(f"exitvar_{span}_{strategy}-*.zip"))
            try:
                if not runs:
                    raise FileNotFoundError(f"no exitvar_{span}_{strategy}-*.zip")
                trades = load_backtest_data(runs[-1], strategy)
            except Exception as error:  # a failed run is reported, not hidden
                out["runs"][f"{span}_{strategy}"] = {"error": str(error)[:300]}
                continue
            out["runs"][f"{span}_{strategy}"] = summarize(trades)
    (ROOT / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    for key, value in out["runs"].items():
        keep = {k: value.get(k) for k in ("trades", "win_rate", "usdt", "usdt_after_funding", "pf", "max_dd_pct",
                                          "worst", "avg_win", "avg_loss", "longest_losing_streak", "t_stat",
                                          "partial_exit_trades", "avg_mfe_pct", "avg_realized_price_pct", "error")}
        print(key, json.dumps(keep, ensure_ascii=False))


if __name__ == "__main__":
    main()
