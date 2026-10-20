"""Phase C walk-forward: ONE LightGBM pooled over all 10 coins, four feature sets.

  python -P /research/walkforward_c.py      (inside the FreqAI image; phase B dir at /research_b)

Pre-registered (fixed before any phase C result was seen):
  windows      same as phase B FreqAI: predict 90 days at a time from 2023-10-01 to
               2026-10-20, each model trained on the 365 days before the window.
               A training row is used only if its 48h label window closed before the
               prediction window starts (row date + 49h <= window start).
  label        phase B barrier label (+2xATR before -2xATR within 48h, daily-trend side),
               on rows where a 4h bar just closed.
  model        phase B LightGBM settings (100 trees, 15 leaves, depth 5, lr 0.05,
               min_child_samples 40, seed 81). NaN features are left to LightGBM.
  calibration  Platt map on the last 25% of each training window (by time, 49h purge),
               slope > 0 or fall back to the training base rate (phase B fix).
  event part   phase B impact weights, recomputed from each training window only.
  feature sets ind (13 indicators), ind_ev (+4 event), ind_der (+18 positioning),
               all (13 + 4 + 18).
Differences from phase B: one model for all coins instead of one per coin, 10 coins
instead of 5, positioning features. Outputs: user_data/models/phase_c/pred_<set>.feather
(read by the backtest strategy) and results/wf_*.{json,csv}.

Modes (argv[1]):
  all_rows     pre-registered pooled model (above).
  per_coin     CONTROL added after the code review (not pre-registered): identical data,
               features, windows and calibration, but one model per coin as in phase B.
               It separates the effect of pooling from the effect of the new features.
               Outputs carry the suffix _pc.
  signal_rows  EXPLORATORY: pooled, trained only on rows where the entry signal fires. _sig.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/research_b")
sys.path.insert(0, "/freqtrade/user_data/strategies")
from features_c import DERIV_FEATURES, INDICATOR_FEATURES, deriv_features, indicator_features, positioning_hourly  # noqa: E402

HOUR = pd.Timedelta(hours=1)
OOS = pd.Timestamp("2023-10-01", tz="UTC")
END = pd.Timestamp("2026-10-20", tz="UTC")
TRAIN = pd.Timedelta(days=365)
STEP = pd.Timedelta(days=90)
LABEL_SPAN = pd.Timedelta(hours=49)          # 48 future 1h bars, the last one closes at t + 49h
HOLDOUT_FRAC = 0.25
EVENT_FEATURES = ["ev_w_recent6h", "ev_w_last24h", "ev_w_next24h", "ev_hours_to_next_kept"]
VARIANTS = {
    "ind": INDICATOR_FEATURES,
    "ind_ev": INDICATOR_FEATURES + EVENT_FEATURES,
    "ind_der": INDICATOR_FEATURES + DERIV_FEATURES,
    "all": INDICATOR_FEATURES + EVENT_FEATURES + DERIV_FEATURES,
}
PHASE_B_PAIRS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "NEAR/USDT:USDT"]
SUFFIX = {"all_rows": "", "per_coin": "_pc", "signal_rows": "_sig"}
PARAMS = dict(n_estimators=100, num_leaves=15, max_depth=5, learning_rate=0.05, min_child_samples=40,
              random_state=81, n_jobs=4, verbosity=-1)


# ------------------------------------------------------------------ windows
def windows(start: pd.Timestamp = OOS, end: pd.Timestamp = END) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """(train_start, predict_start, predict_end) for every 90-day prediction window."""
    out, ws = [], start
    while ws < end:
        out.append((ws - TRAIN, ws, min(ws + STEP, end)))
        ws += STEP
    return out


def train_mask(data: pd.DataFrame, train_start, predict_start) -> pd.Series:
    """Labelled rows of the training year whose label window closed before predict_start."""
    return (data.date >= train_start) & (data.date + LABEL_SPAN <= predict_start) & data.label.notna()


def predict_mask(data: pd.DataFrame, predict_start, predict_end) -> pd.Series:
    return (data.date >= predict_start) & (data.date < predict_end)


def holdout_split(dates: pd.Series, frac: float = HOLDOUT_FRAC) -> tuple[pd.Series, pd.Series]:
    """Time split of a pooled training set: fit < cut - 49h, holdout >= cut."""
    ordered = dates.sort_values()
    cut = ordered.iloc[int(len(ordered) * (1 - frac))]
    return dates < cut - LABEL_SPAN, dates >= cut


# ------------------------------------------------------------------ data
def load_dataset(pairs: list[dict]) -> tuple[pd.DataFrame, dict]:
    from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
    from freqtrade.enums import CandleType
    from SafeTrendAIV0 import forward_labels
    from SafeTrendV0 import SafeTrendV0

    handler = FeatherDataHandler(Path("/freqtrade/user_data/data/bitget"))
    vision = Path("/freqtrade/user_data/data/binance_vision")
    strategy = SafeTrendV0({})
    positioning = {}
    for row in pairs:
        symbol = row["binance_symbol"]
        metrics = pd.read_feather(vision / f"metrics_{symbol}.feather")
        funding = pd.read_feather(vision / f"funding_{symbol}.feather")
        positioning[row["pair"]] = positioning_hourly(metrics, funding, END + HOUR)
    btc = positioning["BTC/USDT:USDT"]
    frames, closes = [], {}
    for row in pairs:
        pair = row["pair"]
        # Same gap filling as Freqtrade backtesting (load_pair_history fill_up_missing=True), so
        # indicators, signals and labels match what the backtest strategy sees.
        candles = handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=True, drop_incomplete=False)
        candles = candles.loc[candles.date < END].reset_index(drop=True)
        closes[pair] = candles.set_index("date").close
        frame = strategy.populate_indicators(candles.copy(), {"pair": pair})
        entries = strategy.populate_entry_trend(frame.copy(), {"pair": pair})
        label = forward_labels(frame, rows=frame.four_close)
        out = pd.DataFrame({
            "pair": pair, "date": frame.date, "four_close": frame.four_close.astype(bool),
            "label": label.map({"yes": 1.0, "no": 0.0}).astype(float),
            "signal": np.where(entries.enter_long == 1, 1, np.where(entries.enter_short == 1, -1, 0)),
        })
        out = pd.concat([out, indicator_features(frame), deriv_features(frame.date, positioning[pair], btc)], axis=1)
        frames.append(out)
    data = pd.concat(frames, ignore_index=True).sort_values(["date", "pair"]).reset_index(drop=True)
    return data, closes


# ------------------------------------------------------------------ model
def fit_window(train: pd.DataFrame, columns: list[str]):
    from lightgbm import LGBMClassifier
    from spike_calibration import PlattMap

    fit, hold = holdout_split(train.date)
    y = train.label.astype(int)
    model = LGBMClassifier(**PARAMS).fit(train.loc[fit, columns], y[fit])
    raw_hold = model.predict_proba(train.loc[hold, columns])[:, 1]
    base_rate = float(y.mean())
    platt = PlattMap.fit(raw_hold, y[hold].to_numpy(), base_rate=base_rate)
    gain = model.booster_.feature_importance(importance_type="gain")
    info = {
        "train_rows": int(len(train)), "fit_rows": int(fit.sum()), "holdout_rows": int(hold.sum()),
        "train_yes_rate": base_rate, "platt_a": platt.a, "platt_b": platt.b, "platt_fallback": platt.fallback,
        "platt_raw_slope": platt.raw_slope,
    }
    return model, platt, info, dict(zip(columns, (gain / gain.sum()).tolist() if gain.sum() else [0.0] * len(columns), strict=True))


def auc(y, p) -> float:
    from sklearn.metrics import roc_auc_score
    y, p = np.asarray(y), np.asarray(p)
    return float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else float("nan")


def block_bootstrap_auc(frame: pd.DataFrame, column: str, draws: int = 1000, seed: int = 81) -> tuple[float, float]:
    """90% CI of AUC resampling whole calendar weeks (labels overlap within a week)."""
    rng = np.random.default_rng(seed)
    weeks = frame.date.dt.to_period("W").astype(str)
    groups = [g for _, g in frame.groupby(weeks)]
    stats = []
    for _ in range(draws):
        sample = pd.concat([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        stats.append(auc(sample.label, sample[column]))
    lo, hi = np.nanpercentile(stats, [5, 95])
    return float(lo), float(hi)


def evaluate(pred: pd.DataFrame) -> dict:
    from phase_b_lib import reliability
    from sklearn.metrics import brier_score_loss

    rows = pred.loc[pred.four_close & pred.label.notna()]
    sig = rows.loc[rows.signal != 0]
    # The tiered strategy also blocks entries near FOMC/CPI/PPI/NFP (§7.1 blackout). These are
    # still signal ROWS, not trades: a breakout can repeat on consecutive 4h bars and at most
    # 2 positions are open.
    sig_tiered = sig.loc[~sig.blackout]
    old = rows.loc[rows.pair.isin(PHASE_B_PAIRS)]
    old_sig = old.loc[old.signal != 0]
    table, gap = reliability(rows.p_cal.to_numpy(), rows.label.to_numpy())
    out = {
        "oos_rows": int(len(rows)), "oos_yes_rate": float(rows.label.mean()),
        "auc_raw": auc(rows.label, rows.p_raw), "auc_cal": auc(rows.label, rows.p_cal),
        "auc_raw_ci90_week_blocks": block_bootstrap_auc(rows, "p_raw"),
        "brier_cal": float(brier_score_loss(rows.label, rows.p_cal)),
        "brier_train_base_rate": float(brier_score_loss(rows.label, rows.base_rate)),
        "reliability_gap": gap, "reliability": table.to_dict(orient="records"),
        "auc_raw_by_window": {str(k.date()): auc(g.label, g.p_raw) for k, g in rows.groupby("window_start")},
        "auc_raw_by_pair": {k: auc(g.label, g.p_raw) for k, g in rows.groupby("pair")},
        "signal_rows": int(len(sig)), "signal_yes_rate": float(sig.label.mean()) if len(sig) else None,
        "signal_auc_raw": auc(sig.label, sig.p_raw), "signal_auc_cal": auc(sig.label, sig.p_cal),
        "signal_auc_raw_ci90_week_blocks": block_bootstrap_auc(sig, "p_raw") if len(sig) > 30 else None,
        "signal_by_threshold": {
            str(t): {"n": int((sig.p_cal >= t).sum()), "yes_rate": float(sig.loc[sig.p_cal >= t, "label"].mean()) if (sig.p_cal >= t).any() else None}
            for t in (0.45, 0.5, 0.55, 0.65)
        },
        "signal_rows_outside_blackout": int(len(sig_tiered)),
        "signal_by_threshold_outside_blackout": {
            str(t): {"n": int((sig_tiered.p_cal >= t).sum()), "yes_rate": float(sig_tiered.loc[sig_tiered.p_cal >= t, "label"].mean()) if (sig_tiered.p_cal >= t).any() else None}
            for t in (0.45, 0.5, 0.55, 0.65)
        },
        "phase_b_5_coins": {"rows": int(len(old)), "auc_raw": auc(old.label, old.p_raw),
                            "signal_rows": int(len(old_sig)), "signal_auc_raw": auc(old_sig.label, old_sig.p_raw)},
        "p_cal_quantiles": {str(q): float(rows.p_cal.quantile(q)) for q in (0.5, 0.9, 0.99)},
    }
    return out


def main(mode: str = "all_rows"):
    """See the module docstring for the three modes."""
    from event_impact import blackout_mask, impact_table, load_calendar, weighted_event_features, weights_from_table

    if mode not in SUFFIX:
        raise SystemExit(f"unknown mode {mode}; use one of {sorted(SUFFIX)}")
    selection = json.loads((ROOT / "pair_selection_c.json").read_text(encoding="utf-8"))["selected"]
    data, closes = load_dataset(selection)
    calendar = load_calendar("/freqtrade/user_data/events/events_c.csv")   # calendar_fix_c.py output (review fix)
    # Blackout keeps the ORIGINAL calendar: a re-dated US release was public days ahead, so
    # blocking entries around it is not look-ahead (events_c only fixes the 14-day feature lead).
    # Same call as SafeTrendV0.populate_entry_trend (entry decided at the candle close).
    data["blackout"] = blackout_mask(data.date + HOUR, load_calendar("/freqtrade/user_data/events/events.csv"))
    prices = {p: closes[p] for p in ("BTC/USDT:USDT", "ETH/USDT:USDT")}
    models_dir = Path("/freqtrade/user_data/models/phase_c")
    models_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "results").mkdir(exist_ok=True)
    preds = {v: [] for v in VARIANTS}
    window_rows, importance = [], []
    for train_start, ws, we in windows():
        weights = weights_from_table(impact_table(calendar, prices, train_start, ws))
        ev = weighted_event_features(data.date, calendar, weights)
        frame = data.copy()
        frame[EVENT_FEATURES] = ev[EVENT_FEATURES].to_numpy()
        train = frame.loc[train_mask(frame, train_start, ws)]
        if mode == "signal_rows":
            train = train.loc[train.signal != 0]
        target = frame.loc[predict_mask(frame, ws, we)]
        # One pooled model, or (per_coin control) one model per coin on that coin's rows only.
        if mode == "per_coin":
            groups = [(pair, train.loc[train.pair == pair], target.loc[target.pair == pair]) for pair in sorted(target.pair.unique())]
        else:
            groups = [(None, train, target)]
        for variant, columns in VARIANTS.items():
            for pair, part_train, part_target in groups:
                model, platt, info, gain = fit_window(part_train, columns)
                raw = model.predict_proba(part_target[columns])[:, 1]
                preds[variant].append(pd.DataFrame({
                    "pair": part_target.pair.to_numpy(), "date": part_target.date.to_numpy(), "four_close": part_target.four_close.to_numpy(),
                    "label": part_target.label.to_numpy(), "signal": part_target.signal.to_numpy(), "blackout": part_target.blackout.to_numpy(),
                    "p_raw": raw, "p_cal": platt(raw), "base_rate": info["train_yes_rate"], "window_start": ws,
                }))
                window_rows.append({"variant": variant, "model_pair": pair or "pooled", "train_start": str(train_start.date()),
                                    "predict_start": str(ws.date()), "predict_end": str(we.date()), "kept_event_groups": len(weights), **info})
                importance.append({"variant": variant, "model_pair": pair or "pooled", "predict_start": str(ws.date()), **gain})
        print(f"window {ws.date()}: train rows {len(train)}, predict rows {len(target)}, event groups {len(weights)}", flush=True)
    suffix = SUFFIX[mode]
    summary = {"pairs": [r["pair"] for r in selection], "mode": mode, "variants": {}}
    for variant in VARIANTS:
        pred = pd.concat(preds[variant], ignore_index=True)
        pred.to_feather(models_dir / f"pred_{variant}{suffix}.feather")
        summary["variants"][variant] = evaluate(pred)
        print(variant, {k: summary["variants"][variant][k] for k in ("auc_raw", "auc_cal", "signal_rows", "signal_auc_raw")}, flush=True)
    summary["windows"] = window_rows
    (ROOT / "results" / f"wf_summary_c{suffix}.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    pd.DataFrame(window_rows).to_csv(ROOT / "results" / f"wf_windows_c{suffix}.csv", index=False)
    imp = pd.DataFrame(importance)
    imp.to_csv(ROOT / "results" / f"wf_importance_by_window_c{suffix}.csv", index=False)
    imp.drop(columns=["predict_start", "model_pair"]).groupby("variant").mean().T.sort_values("all", ascending=False).to_csv(ROOT / "results" / f"wf_importance_c{suffix}.csv")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all_rows")
