"""Deterministic comparison + per-trade post-mortem (spec §7.6, no LLM).

Run inside the Freqtrade image with this directory mounted at /research:
  python -P /research/analyze.py
Reads the exported backtest results under user_data/backtest_results/spike/<run>.
Writes CSV/JSON tables to /research/results/.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from freqtrade.data.btanalysis import load_backtest_data
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType

sys.path.insert(0, "/freqtrade/user_data/strategies")
from event_features import load_events  # noqa: E402
from SafeTrendAITiered import TIER_PROFILES  # noqa: E402
from SafeTrendAIV0 import forward_labels  # noqa: E402
from SafeTrendV0 import SafeTrendV0, parse_tag  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results"
DATA = Path("/freqtrade/user_data/data/bitget")
RESULTS = Path("/freqtrade/user_data/backtest_results/spike")
EVENTS = Path("/freqtrade/user_data/events/events.csv")
WALLET = 105.0
END = pd.Timestamp("2026-10-20", tz="UTC")
OOS_START = pd.Timestamp("2023-10-01", tz="UTC")
FULL_START = pd.Timestamp("2022-10-01", tz="UTC")
LAST90_START = END - pd.Timedelta(days=90)
KST = "Asia/Seoul"
RUNS = {
    "baseline_4y": (FULL_START, "SafeTrendV0"),
    "baseline_oos": (OOS_START, "SafeTrendV0"),
    "ai_v0": (OOS_START, "SafeTrendAIV0"),
    "tiered": (OOS_START, "SafeTrendAITiered"),
}
NOISE_LOOKAHEAD_H = 48
PREDICTIONS = Path("/freqtrade/user_data/models/spike-safetrend-4y-v1/backtesting_predictions")


def load_candles(pairs):
    handler = FeatherDataHandler(DATA)
    strategy = SafeTrendV0({})
    out = {}
    for pair in pairs:
        frame = handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=False, drop_incomplete=False)
        out[pair] = strategy.populate_indicators(frame.copy(), {"pair": pair})[["date", "open", "high", "low", "close", "atr_4h"]].set_index("date")
    return out


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((equity - peak) / peak).min() * -100)


def metrics(trades: pd.DataFrame, start_balance: float) -> dict:
    if trades.empty:
        return {"trades": 0, "win_rate": None, "net_profit_usdt": 0.0, "net_profit_pct": 0.0, "max_drawdown_pct": 0.0, "profit_factor": None, "avg_hold_hours": None}
    ordered = trades.sort_values("close_date")
    equity = pd.concat([pd.Series([start_balance]), start_balance + ordered.profit_abs.cumsum()], ignore_index=True)
    wins = ordered.profit_abs[ordered.profit_abs > 0].sum()
    losses = -ordered.profit_abs[ordered.profit_abs < 0].sum()
    return {
        "trades": int(len(ordered)),
        "win_rate": round(float((ordered.profit_abs > 0).mean() * 100), 1),
        "net_profit_usdt": round(float(ordered.profit_abs.sum()), 2),
        "net_profit_pct": round(float(ordered.profit_abs.sum() / start_balance * 100), 2),
        "max_drawdown_pct": round(max_drawdown(equity), 2),
        "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
        "avg_hold_hours": round(float(ordered.hold_hours.mean()), 1),
        "long_short": f"{int((~ordered.is_short).sum())}/{int(ordered.is_short.sum())}",
    }


def per_year(trades: pd.DataFrame, start_balance: float) -> dict:
    out, balance = {}, start_balance
    if trades.empty:
        return out
    ordered = trades.sort_values("close_date")
    for year, group in ordered.groupby(ordered.close_date.dt.year):
        m = metrics(group, balance)
        m["start_balance"] = round(balance, 2)
        out[int(year)] = m
        balance += group.profit_abs.sum()
    return out


def buy_and_hold(btc: pd.DataFrame, start: pd.Timestamp) -> dict:
    frame = btc.loc[(btc.index >= start) & (btc.index < END)]
    out = {}
    for year, group in frame.groupby(frame.index.year):
        out[int(year)] = round(float(group.close.iloc[-1] / group.open.iloc[0] * 100 - 100), 1)
    out["whole_window"] = round(float(frame.close.iloc[-1] / frame.open.iloc[0] * 100 - 100), 1)
    return out


def postmortem(run: str, trades: pd.DataFrame, candles: dict, events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for t in trades.itertuples():
        frame = candles[t.pair]
        tag = parse_tag(t.enter_tag)
        tier = tag.get("tier", "base")
        prof = TIER_PROFILES.get(tier, TIER_PROFILES["base"])
        before = frame.loc[frame.index < t.open_date]
        atr = float(before.atr_4h.iloc[-1])
        sign = -1 if t.is_short else 1
        during = frame.loc[(frame.index >= t.open_date) & (frame.index <= t.close_date)]
        after = frame.loc[(frame.index > t.close_date) & (frame.index <= t.close_date + pd.Timedelta(hours=NOISE_LOOKAHEAD_H))]
        if t.is_short:
            mfe = (t.open_rate - during.low.min()) / atr
            mae = (during.high.max() - t.open_rate) / atr
            recovered = (not after.empty) and after.low.min() <= t.open_rate + atr
        else:
            mfe = (during.high.max() - t.open_rate) / atr
            mae = (t.open_rate - during.low.min()) / atr
            recovered = (not after.empty) and after.high.max() >= t.open_rate - atr
        initial_stop = t.open_rate - sign * prof["stop_atr"] * atr
        exit_vs_initial = sign * (t.close_rate - initial_stop) / atr  # >0: exited better than the initial stop
        risk_abs = t.amount * prof["stop_atr"] * atr
        r_multiple = t.profit_abs / risk_abs if risk_abs > 0 else np.nan
        reason = str(t.exit_reason)
        event_hit = None
        if len(events):
            window = events.loc[events.high & ((events.ts_utc - t.close_date).abs() <= pd.Timedelta(hours=2))]
            event_hit = ";".join(sorted(set(window.category))) or None
        if reason == "force_exit":
            cause = "open_at_end"
        elif event_hit and t.profit_abs < 0:
            cause = "event_shock"
        elif reason == "roi":
            cause = "take_profit"
        elif reason in {"stop_loss", "trailing_stop_loss", "stoploss_on_exchange", "liquidation"}:
            if exit_vs_initial > 0.05:
                cause = "trailing_exit"
            elif recovered:
                cause = "noise_stop"
            else:
                cause = "trend_reversal"
        elif reason == "opposite_donchian_10":
            cause = "trailing_exit" if t.profit_abs > 0 else "trend_reversal"
        else:
            cause = "other"
        rows.append({
            "run": run, "pair": t.pair, "side": "short" if t.is_short else "long",
            "open_utc": t.open_date, "close_utc": t.close_date,
            "open_kst": t.open_date.tz_convert(KST).strftime("%Y-%m-%d %H:%M"), "close_kst": t.close_date.tz_convert(KST).strftime("%Y-%m-%d %H:%M"),
            "year": t.close_date.year, "tier": tier, "p": float(tag["p"]) if "p" in tag else np.nan,
            "leverage": t.leverage, "stake_usdt": round(t.stake_amount, 4), "open_rate": t.open_rate, "close_rate": t.close_rate,
            "atr_entry": atr, "atr_pct_entry": round(atr / t.open_rate * 100, 3), "stop_atr": prof["stop_atr"],
            "hold_hours": round(t.hold_hours, 1), "mfe_atr": round(float(mfe), 2), "mae_atr": round(float(mae), 2),
            "exit_reason": reason, "exit_vs_initial_stop_atr": round(float(exit_vs_initial), 2),
            "price_back_within_1atr_48h": bool(recovered), "event_within_2h": event_hit or "n/a (no event data)",
            "cause": cause, "profit_pct": round(t.profit_ratio * 100, 2), "profit_usdt": round(t.profit_abs, 4),
            "r_multiple": round(float(r_multiple), 2), "win": t.profit_abs > 0,
        })
    return pd.DataFrame(rows)


def calibration(pairs) -> pd.DataFrame:
    """Out-of-sample p vs realised label on every 4h-close row (not only signals)."""
    handler = FeatherDataHandler(DATA)
    strategy = SafeTrendV0({})
    rows = []
    for pair in pairs:
        files = sorted(PREDICTIONS.glob(f"cb_{pair.split('/')[0].lower()}_*_prediction.feather"))
        if not files:
            continue
        preds = pd.concat([pd.read_feather(f) for f in files], ignore_index=True)
        if "date_pred" in preds:
            preds = preds.rename(columns={"date_pred": "date"})
        frame = strategy.populate_indicators(handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=False, drop_incomplete=False), {"pair": pair})
        frame["label"] = forward_labels(frame, rows=frame.four_close)
        merged = frame[["date", "label"]].merge(preds[["date", "yes", "do_predict"]], on="date")
        merged = merged.loc[merged.label.notna() & (merged.do_predict == 1) & (merged.date >= OOS_START)]
        merged["pair"] = pair
        rows.append(merged)
    if not rows:
        return pd.DataFrame()
    allrows = pd.concat(rows, ignore_index=True)
    allrows["p_bucket"] = pd.cut(allrows.yes, [0, 0.3, 0.45, 0.55, 0.65, 1.0], right=False).astype(str)
    allrows["hit"] = allrows.label == "yes"
    table = allrows.groupby("p_bucket").agg(rows=("hit", "size"), mean_p=("yes", "mean"), realised_yes_rate=("hit", "mean")).reset_index()
    total = pd.DataFrame([{"p_bucket": "ALL", "rows": len(allrows), "mean_p": allrows.yes.mean(), "realised_yes_rate": allrows.hit.mean()}])
    return pd.concat([table, total], ignore_index=True).round(3)


def group_table(pm: pd.DataFrame, by: str) -> pd.DataFrame:
    g = pm.groupby(["run", by])
    table = g.agg(trades=("win", "size"), win_rate=("win", "mean"), avg_r=("r_multiple", "mean"), profit_usdt=("profit_usdt", "sum"), avg_hold_h=("hold_hours", "mean"), avg_mfe_atr=("mfe_atr", "mean"), avg_mae_atr=("mae_atr", "mean")).reset_index()
    table["win_rate"] = (table.win_rate * 100).round(1)
    return table.round({"avg_r": 2, "profit_usdt": 2, "avg_hold_h": 1, "avg_mfe_atr": 2, "avg_mae_atr": 2})


def main():
    OUT.mkdir(exist_ok=True)
    loaded = {}
    for run, (start, strategy) in RUNS.items():
        path = RESULTS / run
        if not path.exists():
            print("missing run", run)
            continue
        trades = load_backtest_data(path, strategy)
        trades["hold_hours"] = (trades.close_date - trades.open_date).dt.total_seconds() / 3600
        loaded[run] = (start, trades)
    pairs = sorted({p for _, t in loaded.values() for p in t.pair.unique()} | {"BTC/USDT:USDT"})
    candles = load_candles(pairs)
    events = load_events(EVENTS)
    summary = {"runs": {}, "per_year": {}, "bnh_btc": {}}
    pms = []
    for run, (start, trades) in loaded.items():
        assert trades.empty or trades.open_date.min() >= start, f"{run} has trades before {start}"
        summary["runs"][run] = metrics(trades, WALLET)
        summary["per_year"][run] = per_year(trades, WALLET)
        pms.append(postmortem(run, trades, candles, events))
    summary["bnh_btc"]["from_2022-10-01"] = buy_and_hold(candles["BTC/USDT:USDT"], FULL_START)
    summary["bnh_btc"]["from_2023-10-01"] = buy_and_hold(candles["BTC/USDT:USDT"], OOS_START)
    pm = pd.concat(pms, ignore_index=True)
    pm.drop(columns=["open_utc", "close_utc"]).to_csv(OUT / "trades_postmortem.csv", index=False)
    for by in ("tier", "cause", "year", "pair"):
        group_table(pm, by).to_csv(OUT / f"postmortem_by_{by}.csv", index=False)
    # Win rate and R by p bucket (all AI runs) to see whether p separates outcomes at all.
    ai = pm.loc[pm.run.isin(["ai_v0", "tiered"]) & pm.p.notna()].copy()
    if not ai.empty:
        ai["p_bucket"] = pd.cut(ai.p, [0, 0.45, 0.5, 0.55, 0.6, 0.65, 1.0], right=False).astype(str)
        group_table(ai, "p_bucket").to_csv(OUT / "postmortem_by_p_bucket.csv", index=False)
    cal = calibration(pairs)
    cal.to_csv(OUT / "calibration_oos.csv", index=False)
    summary["calibration_oos"] = cal.to_dict(orient="records")
    last90 = pm.loc[pm.run.isin(["baseline_oos", "tiered"]) & (pm.open_utc >= LAST90_START)]
    last90 = last90[["run", "pair", "side", "tier", "p", "open_kst", "open_rate", "close_kst", "close_rate", "exit_reason", "cause", "profit_pct", "profit_usdt", "win"]].sort_values(["run", "open_kst"])
    last90.to_csv(OUT / "last90_entries.csv", index=False)
    summary["last90_entries"] = int(len(last90))
    counts = last90.pair.value_counts()
    summary["most_traded_pair_last90"] = counts.idxmax() if len(counts) else None
    others = counts.drop("BTC/USDT:USDT", errors="ignore")
    summary["most_traded_non_btc_pair_last90"] = others.idxmax() if len(others) else None
    summary["last90_counts_by_pair"] = counts.to_dict()
    summary["funding_shorts_symmetric_stress_usdt"] = {
        run: round(float((t.loc[t.is_short, "amount"] * t.loc[t.is_short, "open_rate"] * t.loc[t.is_short, "hold_hours"] / 8 * 0.0002).sum()), 2)
        for run, (_, t) in loaded.items()
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
