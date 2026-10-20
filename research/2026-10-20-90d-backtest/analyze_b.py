"""Phase B analysis: conservative funding, mark-to-market drawdown, calibration, post-mortem.

  python -P /research/analyze_b.py        (inside the FreqAI image, research dir at /research)

Reads backtests under user_data/backtest_results/spike_b/<run> (engine funding = 0 by
construction) and charges funding here at every 00/08/16 UTC crossing:
  headline   both sides pay +0.01%/8h
  sensitivity phase A model (longs pay, shorts receive)
Writes results/phase_b/*.csv and results/phase_b/summary_b.json.
"""
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from freqtrade.data.btanalysis import load_backtest_data
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType
from sklearn.metrics import brier_score_loss, roc_auc_score

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/freqtrade/user_data/strategies")
from event_impact import load_calendar  # noqa: E402
from phase_b_lib import (FUNDING_RATE, entry_fills, equity_curve, funding_schedule, max_drawdown_pct, mfe_mae_atr,  # noqa: E402
                         period_return_pct, reliability)
from SafeTrendAIV0 import forward_labels  # noqa: E402
from SafeTrendPhaseB import SafeTrendTieredV1TP2  # noqa: E402
from SafeTrendAITiered import TIER_PROFILES  # noqa: E402
from SafeTrendV0 import SafeTrendV0, parse_tag  # noqa: E402

OUT = ROOT / "results" / "phase_b"
DATA = Path("/freqtrade/user_data/data/bitget")
RESULTS = Path("/freqtrade/user_data/backtest_results/spike_b")
MODELS = Path("/freqtrade/user_data/models")
LOGS = Path("/freqtrade/user_data/logs/phase_b")
CALENDAR = Path("/freqtrade/user_data/events/events.csv")
WALLET = 105.0
END = pd.Timestamp("2026-10-20", tz="UTC")
OOS = pd.Timestamp("2023-10-01", tz="UTC")
FULL = pd.Timestamp("2022-10-01", tz="UTC")
Y2024 = pd.Timestamp("2024-01-01", tz="UTC")
LAST6M = pd.Timestamp("2026-04-20", tz="UTC")
KST = "Asia/Seoul"
NOISE_H = 48

# name: (strategy class, window start, kind, model identifier, profiles)
RUNS = {
    "baseline_4y": ("SafeTrendV0", FULL, "phase A corrected", None),
    "baseline_oos": ("SafeTrendV0", OOS, "phase B variant", None),
    "aiv0_raw": ("SafeTrendAIV0", OOS, "phase A corrected", "spike-p2-raw"),
    "tiered_raw": ("SafeTrendAITiered", OOS, "phase A corrected", "spike-p2-raw"),
    "aiv0_cal": ("SafeTrendAIV0", OOS, "phase B variant", "spike-p2-aiv0-cal"),
    "aiv1_cal": ("SafeTrendAIV1", OOS, "phase B variant", "spike-p2-aiv1-cal"),
    "tiered_v1": ("SafeTrendTieredV1", OOS, "phase B variant", "spike-p2-aiv1-cal"),
    "tiered_v1_plus": ("SafeTrendTieredV1Plus", OOS, "phase B variant", "spike-p2-aiv1-cal"),
    "tiered_v1_tp2": ("SafeTrendTieredV1TP2", OOS, "EXPLORATORY", "spike-p2-aiv1-cal"),
    "aiv1_raw": ("SafeTrendAIV1", OOS, "reference (uncalibrated p)", "spike-p2-aiv1-raw"),
    "tiered_v1_raw": ("SafeTrendTieredV1", OOS, "reference (uncalibrated p)", "spike-p2-aiv1-raw"),
    "tiered_v1_plus_raw": ("SafeTrendTieredV1Plus", OOS, "reference (uncalibrated p)", "spike-p2-aiv1-raw"),
}
STOP_REASONS = {"stop_loss", "trailing_stop_loss", "stoploss_on_exchange", "liquidation"}


def load_candles(pairs):
    handler = FeatherDataHandler(DATA)
    strategy = SafeTrendV0({})
    candles, marks = {}, {}
    for pair in pairs:
        frame = handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=False, drop_incomplete=False)
        frame = strategy.populate_indicators(frame.copy(), {"pair": pair})
        candles[pair] = frame
        mark = handler.ohlcv_load(pair, "1h", candle_type=CandleType.MARK, fill_missing=False, drop_incomplete=False)
        marks[pair] = mark.set_index("date").open
    return candles, marks


def profiles_for(run: str) -> dict:
    return SafeTrendTieredV1TP2.profiles if run == "tiered_v1_tp2" else TIER_PROFILES


def enrich(run: str, trades: pd.DataFrame, marks: dict) -> pd.DataFrame:
    trades = trades.copy()
    trades["hold_hours"] = (trades.close_date - trades.open_date).dt.total_seconds() / 3600
    trades["fills"] = [entry_fills(o) for o in trades.orders]
    trades["entries"] = [len(f) for f in trades.fills]
    times, costs, cons, signed = [], [], [], []
    for t in trades.itertuples():
        schedule = funding_schedule(t.open_date, t.close_date, t.fills, marks[t.pair])
        times.append([ts for ts, _ in schedule])
        costs.append([FUNDING_RATE * n for _, n in schedule])
        notional = sum(n for _, n in schedule)
        cons.append(FUNDING_RATE * notional)
        signed.append((-FUNDING_RATE if t.is_short else FUNDING_RATE) * notional)
    trades["funding_times"], trades["funding_costs"] = times, costs
    trades["funding_conservative"], trades["funding_signed"] = cons, signed
    trades["net"] = trades.profit_abs - trades.funding_conservative
    trades["net_signed"] = trades.profit_abs - trades.funding_signed
    trades["tier"] = [parse_tag(tag).get("tier", "base") for tag in trades.enter_tag]
    return trades


def side_split(trades: pd.DataFrame) -> dict:
    out = {}
    for side, group in (("long", trades.loc[~trades.is_short]), ("short", trades.loc[trades.is_short])):
        out[side] = {"trades": int(len(group)), "win_rate": round(float((group.net > 0).mean() * 100), 1) if len(group) else None, "net_usdt": round(float(group.net.sum()), 2)}
    return out


def max_concurrent(trades: pd.DataFrame) -> int:
    events = sorted([(t, 1) for t in trades.open_date] + [(t, -1) for t in trades.close_date], key=lambda x: (x[0], x[1]))
    best = cur = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


def metrics(run: str, trades: pd.DataFrame, start, closes: dict, btc: pd.DataFrame) -> tuple[dict, pd.Series]:
    equity = equity_curve(trades, closes, WALLET, start, END) if len(trades) else pd.Series(WALLET, index=pd.date_range(start, END, freq="h") + pd.Timedelta(hours=1))
    closed = pd.concat([pd.Series([WALLET]), WALLET + trades.sort_values("close_date").net.cumsum()], ignore_index=True)
    wins, losses = trades.net[trades.net > 0].sum(), -trades.net[trades.net < 0].sum()
    years = {}
    for year in range(start.year, END.year + 1):
        y0 = max(pd.Timestamp(f"{year}-01-01", tz="UTC"), start)
        y1 = min(pd.Timestamp(f"{year + 1}-01-01", tz="UTC"), END)
        in_year = trades.loc[(trades.close_date >= y0) & (trades.close_date < y1)]
        years[year] = {"trades": int(len(in_year)), "net_pct": round(period_return_pct(equity, WALLET, y0, y1), 2),
                       "mdd_mtm_pct": round(max_drawdown_pct(equity.loc[(equity.index > y0) & (equity.index <= y1)]), 2),
                       "btc_hold_pct": bnh(btc, y0, y1)}
    m = {
        "kind": RUNS[run][2], "strategy": RUNS[run][0], "window_start": str(start.date()),
        "trades": int(len(trades)),
        "win_rate": round(float((trades.net > 0).mean() * 100), 1) if len(trades) else None,
        "net_usdt": round(float(equity.iloc[-1] - WALLET), 2),
        "net_pct": round(float((equity.iloc[-1] / WALLET - 1) * 100), 2),
        "net_pct_signed_funding_phaseA": round(float(trades.net_signed.sum() / WALLET * 100), 2),
        "net_pct_no_funding": round(float(trades.profit_abs.sum() / WALLET * 100), 2),
        "funding_paid_usdt": round(float(trades.funding_conservative.sum()), 2),
        "mdd_mtm_pct": round(max_drawdown_pct(equity), 2),
        "mdd_closed_trade_pct": round(max_drawdown_pct(closed), 2),
        "pf": round(float(wins / losses), 2) if losses > 0 else None,
        "avg_hold_hours": round(float(trades.hold_hours.mean()), 1) if len(trades) else None,
        "net_2024_plus_pct": round(period_return_pct(equity, WALLET, Y2024, END), 2) if start < Y2024 else None,
        "net_last6m_pct": round(period_return_pct(equity, WALLET, LAST6M, END), 2),
        "trades_2024_plus": int((trades.close_date >= Y2024).sum()),
        "trades_last6m": int((trades.close_date >= LAST6M).sum()),
        "long_short": side_split(trades),
        "per_year": years,
        "max_concurrent_positions": max_concurrent(trades) if len(trades) else 0,
        "scale_in_orders": int((trades.entries - 1).clip(lower=0).sum()) if len(trades) else 0,
        "tiers": trades.tier.value_counts().to_dict() if len(trades) else {},
    }
    return m, equity


def bnh(btc: pd.DataFrame, start, end) -> float:
    frame = btc.loc[(btc.date >= start) & (btc.date < end)]
    return round(float(frame.close.iloc[-1] / frame.open.iloc[0] * 100 - 100), 1) if len(frame) else None


def postmortem(run: str, trades: pd.DataFrame, indexed: dict, calendar: pd.DataFrame) -> pd.DataFrame:
    rows = []
    shocks = calendar.loc[calendar.exact & (calendar.importance == "high")]
    for t in trades.itertuples():
        frame = indexed[t.pair]
        prof = profiles_for(run).get(t.tier, TIER_PROFILES["base"])
        atr = float(frame.loc[frame.index < t.open_date].atr_4h.iloc[-1])
        first_rate = t.fills[0][2] if t.fills else t.open_rate
        first_amount = t.fills[0][1] if t.fills else t.amount
        mfe, mae = mfe_mae_atr(frame, t.open_date, t.close_date, t.open_rate, t.close_rate, bool(t.is_short), atr)
        sign = -1 if t.is_short else 1
        after = frame.loc[(frame.index > t.close_date) & (frame.index <= t.close_date + pd.Timedelta(hours=NOISE_H))]
        if t.is_short:
            recovered = (not after.empty) and after.low.min() <= first_rate + atr
        else:
            recovered = (not after.empty) and after.high.max() >= first_rate - atr
        initial_stop = first_rate - sign * prof["stop_atr"] * atr
        exit_vs_initial = sign * (t.close_rate - initial_stop) / atr
        risk_abs = first_amount * prof["stop_atr"] * atr
        near = shocks.loc[(shocks.ts_utc - t.close_date).abs() <= pd.Timedelta(hours=2)]
        shock = ";".join(sorted(set(near.group))) or None
        around = calendar.loc[(calendar.available >= t.open_date - pd.Timedelta(hours=24)) & (calendar.available <= t.close_date + pd.Timedelta(hours=24))]
        reason = str(t.exit_reason)
        if reason == "force_exit":
            cause = "open_at_end"
        elif shock and t.net < 0:
            cause = "event_shock"
        elif reason == "roi":
            cause = "take_profit"
        elif reason in STOP_REASONS:
            cause = "trailing_exit" if exit_vs_initial > 0.05 else ("noise_stop" if recovered else "trend_reversal")
        elif reason == "opposite_donchian_10":
            cause = "trailing_exit" if t.net > 0 else "trend_reversal"
        else:
            cause = "other"
        tag = parse_tag(t.enter_tag)
        rows.append({
            "run": run, "pair": t.pair, "side": "short" if t.is_short else "long",
            "open_kst": t.open_date.tz_convert(KST).strftime("%Y-%m-%d %H:%M"), "close_kst": t.close_date.tz_convert(KST).strftime("%Y-%m-%d %H:%M"),
            "open_utc": t.open_date, "close_utc": t.close_date, "year": t.close_date.year,
            "tier": t.tier, "p": float(tag["p"]) if "p" in tag else np.nan, "entries": t.entries,
            "leverage": t.leverage, "open_rate": t.open_rate, "close_rate": t.close_rate, "atr_entry": atr,
            "hold_hours": round(t.hold_hours, 1), "mfe_atr": round(float(mfe), 2), "mae_atr": round(float(mae), 2),
            "exit_reason": reason, "exit_vs_initial_stop_atr": round(float(exit_vs_initial), 2),
            "price_back_within_1atr_48h": bool(recovered), "event_within_2h_of_exit": shock or "",
            "events_pm24h": len(around), "cause": cause,
            "profit_usdt_engine": round(t.profit_abs, 4), "funding_usdt": round(t.funding_conservative, 4),
            "net_usdt": round(t.net, 4), "r_multiple": round(float(t.net / risk_abs), 2) if risk_abs > 0 else np.nan, "win": t.net > 0,
        })
    return pd.DataFrame(rows)


def group_table(pm: pd.DataFrame, by: str) -> pd.DataFrame:
    table = pm.groupby(["run", by]).agg(trades=("win", "size"), win_rate=("win", "mean"), avg_r=("r_multiple", "mean"), net_usdt=("net_usdt", "sum"),
                                        avg_hold_h=("hold_hours", "mean"), avg_mfe_atr=("mfe_atr", "mean"), avg_mae_atr=("mae_atr", "mean")).reset_index()
    table["win_rate"] = (table.win_rate * 100).round(1)
    return table.round({"avg_r": 2, "net_usdt": 2, "avg_hold_h": 1, "avg_mfe_atr": 2, "avg_mae_atr": 2})


# ------------------------------------------------------------------ calibration
def labelled_rows(candles: dict) -> pd.DataFrame:
    rows = []
    for pair, frame in candles.items():
        label = forward_labels(frame, rows=frame.four_close)
        signal_long = frame.four_close & (frame.adx_4h > 20) & (frame.atr_pct_4h <= frame.atr_q90_4h) & (frame.ema50_1d > frame.ema200_1d) & (frame.close_4h > frame.dc_high_4h)
        signal_short = frame.four_close & (frame.adx_4h > 20) & (frame.atr_pct_4h <= frame.atr_q90_4h) & (frame.ema50_1d < frame.ema200_1d) & (frame.close_4h < frame.dc_low_4h)
        rows.append(pd.DataFrame({"pair": pair, "date": frame.date, "label": label, "signal": (signal_long | signal_short).to_numpy()}))
    out = pd.concat(rows, ignore_index=True)
    return out.loc[out.label.notna() & (out.date >= OOS)]


def model_predictions(identifier: str) -> pd.DataFrame:
    frames = []
    for path in sorted((MODELS / identifier).glob("sub-train-*/spike_raw_cal.feather")):
        pair = path.parent.name.split("-")[2].split("_")[0] + "/USDT:USDT"
        frame = pd.read_feather(path)
        frame["pair"] = pair
        frames.append(frame)
    if frames:
        return pd.concat(frames, ignore_index=True)
    frames = []  # vanilla LightGBM run: raw p only, from FreqAI's saved backtesting predictions
    for path in sorted((MODELS / identifier / "backtesting_predictions").glob("cb_*_prediction.feather")):
        frame = pd.read_feather(path).rename(columns={"date_pred": "date"})
        frame = frame.assign(pair=path.name.split("_")[1].upper() + "/USDT:USDT", raw_yes=frame["yes"], cal_yes=np.nan)
        frames.append(frame[["date", "pair", "raw_yes", "cal_yes", "do_predict"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def calibration_report(identifier: str, labels: pd.DataFrame) -> dict:
    preds = model_predictions(identifier)
    if preds.empty:
        return {}
    preds["date"] = pd.to_datetime(preds.date, utc=True).dt.as_unit("ns")
    labels = labels.assign(date=pd.to_datetime(labels.date, utc=True).dt.as_unit("ns"))
    merged = labels.merge(preds, on=["pair", "date"])
    merged = merged.loc[merged.do_predict == 1]
    hit = (merged.label == "yes").to_numpy()
    out = {"rows": int(len(merged)), "base_rate": round(float(hit.mean()), 4)}
    for name in ("raw_yes", "cal_yes"):
        p = merged[name].to_numpy(dtype=float)
        if np.isnan(p).all():
            continue
        table, gap = reliability(p, hit)
        key = "pre" if name == "raw_yes" else "post"
        table.round(4).to_csv(OUT / f"reliability_{identifier}_{key}.csv", index=False)
        sig = merged.signal.to_numpy()
        out[key] = {
            "bucket_gap": round(gap, 4), "auc": round(float(roc_auc_score(hit, p)), 4), "brier": round(float(brier_score_loss(hit, p)), 4),
            "mean_p": round(float(p.mean()), 4), "table": table.round(3).to_dict(orient="records"),
            "signal_rows": int(sig.sum()), "signal_auc": round(float(roc_auc_score(hit[sig], p[sig])), 4) if sig.sum() > 10 and 0 < hit[sig].mean() < 1 else None,
            "signal_yes_rate": round(float(hit[sig].mean()), 4) if sig.sum() else None,
            "signal_share_p_ge": {str(x): round(float((p[sig] >= x).mean()), 4) for x in (0.45, 0.5, 0.55, 0.65)},
            "signal_p_quantiles": {str(q): round(float(np.quantile(p[sig], q)), 4) for q in (0.5, 0.9, 0.99, 1.0)} if sig.sum() else {},
        }
    windows = []
    for path in sorted((MODELS / identifier).glob("sub-train-BTC_*/spike_window.json")):
        info = json.loads(path.read_text())
        windows.append({k: info.get(k) for k in ("train_start", "train_end", "platt_a", "platt_b", "holdout_yes_rate", "holdout_raw_mean")} | {"kept_groups": sorted((info.get("event_weights") or {}).keys())})
    out["windows_btc"] = windows
    return out


def blackout_counts(run: str) -> int:
    path = LOGS / f"{run}.log"
    if not path.is_file():
        return 0
    return sum(int(m.group(1)) for m in re.finditer(r"blackout blocked (\d+) ", path.read_text(errors="ignore")))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    calendar = load_calendar(CALENDAR)
    loaded = {}
    for run, (strategy, start, _, _) in RUNS.items():
        path = RESULTS / run
        if not path.exists():
            print("missing run", run)
            continue
        trades = load_backtest_data(path, strategy)
        assert trades.empty or trades.open_date.min() >= start, f"{run} trades before {start}"
        assert trades.empty or float(trades.funding_fees.abs().sum()) < 1e-9, f"{run}: engine charged funding; expected zero-funding datadir"
        loaded[run] = (start, trades)
    pairs = sorted({p for _, t in loaded.values() for p in t.pair.unique()} | {"BTC/USDT:USDT"})
    candles, marks = load_candles(pairs)
    indexed = {p: f.set_index("date") for p, f in candles.items()}
    closes = {p: f.close for p, f in indexed.items()}
    btc = candles["BTC/USDT:USDT"]
    summary = {"funding_model": "headline: both sides pay 0.01%/8h at each 00/08/16 UTC crossing (mark price notional); sensitivity: phase A signed model",
               "mdd": "hourly mark-to-market (candle close, incl. entry fee + estimated exit fee + funding)",
               "runs": {}, "btc_hold": {"oos": bnh(btc, OOS, END), "from_2024": bnh(btc, Y2024, END), "last6m": bnh(btc, LAST6M, END), "full": bnh(btc, FULL, END)}}
    pms, equities = [], {}
    for run, (start, trades) in loaded.items():
        trades = enrich(run, trades, marks)
        m, equity = metrics(run, trades, start, closes, btc)
        m["blackout_blocked_signals"] = blackout_counts(run)
        summary["runs"][run] = m
        equities[run] = equity
        if len(trades):
            pms.append(postmortem(run, trades, indexed, calendar))
    pd.DataFrame(equities).to_csv(OUT / "equity_mtm_hourly.csv.gz", compression="gzip")
    pm = pd.concat(pms, ignore_index=True)
    pm.drop(columns=["open_utc", "close_utc"]).to_csv(OUT / "trades_postmortem_b.csv", index=False)
    for by in ("cause", "tier", "year", "side", "pair"):
        group_table(pm, by).to_csv(OUT / f"postmortem_b_by_{by}.csv", index=False)
    last6 = pm.loc[pm.run.isin(["baseline_oos", "tiered_v1", "tiered_v1_plus", "tiered_v1_raw", "tiered_v1_plus_raw"]) & (pm.open_utc >= LAST6M)]
    last6[["run", "pair", "side", "tier", "p", "entries", "open_kst", "close_kst", "exit_reason", "cause", "net_usdt"]].to_csv(OUT / "last6m_entries.csv", index=False)
    labels = labelled_rows(candles)
    summary["calibration"] = {ident: calibration_report(ident, labels) for ident in ("spike-p2-raw", "spike-p2-aiv0-cal", "spike-p2-aiv1-cal", "spike-p2-aiv1-raw")}
    summary["postmortem_cause_counts"] = pm.groupby(["run", "cause"]).size().unstack(fill_value=0).to_dict(orient="index")
    (OUT / "summary_b.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    brief = {r: {k: m[k] for k in ("trades", "win_rate", "net_pct", "net_pct_signed_funding_phaseA", "mdd_mtm_pct", "mdd_closed_trade_pct", "pf", "net_2024_plus_pct", "net_last6m_pct", "max_concurrent_positions", "scale_in_orders")} for r, m in summary["runs"].items()}
    print(json.dumps(brief, indent=1))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk in ("rows", "base_rate")} | {s: {kk: v[s][kk] for kk in ("bucket_gap", "auc", "brier", "signal_share_p_ge", "signal_p_quantiles")} for s in ("pre", "post") if s in v} for k, v in summary["calibration"].items()}, indent=1))


if __name__ == "__main__":
    main()
