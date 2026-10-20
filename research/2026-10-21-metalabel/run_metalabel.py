"""Phase D: why phase B/C failed, and a meta-label filter trained on real trade outcomes.

  python -P /research/run_metalabel.py     (FreqAI image; phase C dir at /research_c, phase B at /research_b)

Pre-registered in README.md before any result was seen. Steps:
  1. signal table: every SafeTrendV0 entry signal (10 coins, 2022-10-01 .. 2026-10-20) with the
     phase C features (imported, not copied), the 48h barrier label and the simulated
     base-profile trade (metalabel_lib.simulate_trade).
  2. diagnostics (label mismatch, base-rate drift, adversarial validation, sample size,
     payoff concentration) and a trade-level check against the phase C engine backtest.
  3. walk-forward models (LR / LightGBM x per-coin / pooled x ind / ind_der) and fixed rules.
  4. evaluation: OOS AUC + week-block CI, tercile mean R, 2-slot portfolio vs all signals.
Outputs go to results/.

Added after the first results (review fixes, not pre-registered): portfolio stop-exit timing
(metalabel_lib.portfolio), adversarial-validation controls (early vs late training year,
week-grouped CV), Fisher CIs for window persistence, month/window-block AUC CIs, a block CI
for top-minus-bottom mean R, a pass-rate-matched random-filter null for the portfolio
returns, per-period long/short PnL, and the full engine mismatch list.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/research_c")
sys.path.insert(0, "/research_b")
sys.path.insert(0, "/freqtrade/user_data/strategies")
import metalabel_lib as ml  # noqa: E402
import walkforward_c as wf  # noqa: E402
from features_c import DERIV_FEATURES, INDICATOR_FEATURES  # noqa: E402

warnings.filterwarnings("ignore", category=UserWarning)
OUT = ROOT / "results"
START = pd.Timestamp("2022-10-01", tz="UTC")
OOS, END = wf.OOS, wf.END
P1 = (OOS, pd.Timestamp("2025-01-01", tz="UTC"))
P2 = (pd.Timestamp("2025-01-01", tz="UTC"), END)
Y2024 = pd.Timestamp("2024-01-01", tz="UTC")
LAST6M = pd.Timestamp("2026-04-20", tz="UTC")
FEATURES = {"ind": INDICATOR_FEATURES, "ind_der": INDICATOR_FEATURES + DERIV_FEATURES}
LR_C = 0.1
MIN_TRAIN = 20     # not pre-registered: fewer rows (or one class) -> constant base-rate prediction
NULL_DRAWS = 300
NULL_SEED = 81


# ------------------------------------------------------------------ data
def load_selection() -> list[dict]:
    return json.loads(Path("/research_c/pair_selection_c.json").read_text(encoding="utf-8"))["selected"]


def candle_frames(pairs: list[str]) -> tuple[dict, dict]:
    """SafeTrendV0-populated 1h frames (fill_missing=True, as load_dataset/Freqtrade) and mark opens."""
    from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
    from freqtrade.enums import CandleType
    from SafeTrendV0 import SafeTrendV0

    handler = FeatherDataHandler(Path("/freqtrade/user_data/data/bitget"))
    strategy = SafeTrendV0({})
    frames, marks = {}, {}
    for pair in pairs:
        candles = handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=True, drop_incomplete=False)
        candles = candles.loc[candles.date < END].reset_index(drop=True)
        frame = strategy.populate_indicators(candles.copy(), {"pair": pair})
        frame = strategy.populate_entry_trend(frame, {"pair": pair})
        frame = strategy.populate_exit_trend(frame, {"pair": pair})
        frames[pair] = frame
        mark = handler.ohlcv_load(pair, "1h", candle_type=CandleType.MARK, fill_missing=False, drop_incomplete=False)
        marks[pair] = mark.set_index("date").open
    return frames, marks


def simulate_pair(frame: pd.DataFrame, mark: pd.Series, start: pd.Timestamp = START, mode: str = "spec") -> pd.DataFrame:
    arr = {k: frame[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "atr_4h")}
    dates = frame.date.to_numpy()
    ex_long = (frame.exit_long == 1).to_numpy()
    ex_short = (frame.exit_short == 1).to_numpy()
    rows = []
    for i in np.flatnonzero(((frame.enter_long == 1) | (frame.enter_short == 1)).to_numpy() & (frame.date >= start).to_numpy()):
        side = 1 if frame.enter_long.iat[i] == 1 else -1
        res = ml.simulate_trade(dates, arr["open"], arr["high"], arr["low"], arr["close"], arr["atr_4h"],
                                ex_long if side == 1 else ex_short, i, side, mark, mode=mode)
        if res is not None:
            rows.append({"date": frame.date.iat[i], "side": side, **res})
    return pd.DataFrame(rows)


def build_signal_table(selection, mode: str = "spec") -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    pairs = [r["pair"] for r in selection]
    data, _ = wf.load_dataset(selection)
    frames, marks = candle_frames(pairs)
    sims = []
    for row in selection:
        sim = simulate_pair(frames[row["pair"]], marks[row["pair"]], mode=mode)
        sim["pair"] = row["pair"]
        sim["min_cost"] = float(row.get("minimum_cost") or 0)
        sim["min_amount"] = float(row.get("minimum_amount") or 0)
        sims.append(sim)
    sims = pd.concat(sims, ignore_index=True)
    cols = ["pair", "date", "signal", "label"] + INDICATOR_FEATURES + DERIV_FEATURES
    table = sims.merge(data[cols], on=["pair", "date"], how="left", validate="one_to_one")
    # The phase C dataset must flag the same rows with the same side.
    assert (table.signal == table.side).all(), "signal mismatch between load_dataset and simulation"
    phase_c_sig = data.loc[(data.signal != 0) & (data.date >= START)]
    assert len(phase_c_sig) == len(table), f"signal rows {len(phase_c_sig)} vs simulated {len(table)}"
    table["win"] = (table.r > 0).astype(float)
    table["barrier"] = table.label
    # BTC daily trend side at the same hour, for the btc_regime rule.
    btc = data.loc[data.pair == "BTC/USDT:USDT", ["date", "trend_side"]].rename(columns={"trend_side": "btc_trend_side"})
    table = table.merge(btc, on="date", how="left")
    table = table.sort_values(["date", "pair"]).reset_index(drop=True)
    return table, data, frames


# ------------------------------------------------------------------ engine check
def engine_check(table: pd.DataFrame) -> dict:
    """Compare simulated trades with phase C engine trades (baseline_oos) entered at the same candle."""
    path = Path("/research_c/results/trades_postmortem_c.csv")
    eng = pd.read_csv(path)
    eng = eng.loc[eng.run == "baseline_oos"].copy()
    eng["entry_time"] = pd.to_datetime(eng.open_kst).dt.tz_localize("Asia/Seoul").dt.tz_convert("UTC")
    eng["close_time"] = pd.to_datetime(eng.close_kst).dt.tz_localize("Asia/Seoul").dt.tz_convert("UTC")
    m = eng.merge(table[["pair", "entry_time", "exit_candle", "exit_price", "entry", "exit_reason", "r", "side"]].rename(columns={"side": "sim_side"}), on=["pair", "entry_time"], how="left")
    found = m.exit_candle.notna()
    same_time = found & (m.exit_candle == m.close_time)
    rel = (m.exit_price - m.close_rate).abs() / m.close_rate
    same_px = found & (rel < 1e-4)
    m["rel_price_gap_pct"] = rel * 100
    m["candle_diff_h"] = (m.exit_candle - m.close_time) / pd.Timedelta(hours=1)
    m["r_diff"] = m.r - m.r_multiple
    mism = m.loc[found & ~(same_time & same_px), ["pair", "sim_side", "entry_time", "close_time", "exit_candle", "candle_diff_h", "close_rate", "exit_price",
                                                  "rel_price_gap_pct", "exit_reason_x", "exit_reason_y", "r_multiple", "r", "r_diff"]]
    mism = mism.rename(columns={"close_time": "engine_exit_candle", "exit_candle": "sim_exit_candle", "close_rate": "engine_exit_price",
                                "exit_price": "sim_exit_price", "exit_reason_x": "engine_exit_reason", "exit_reason_y": "sim_exit_reason",
                                "r_multiple": "engine_r", "r": "sim_r"})
    same_candle_gap = m.loc[found & same_time, "rel_price_gap_pct"]
    return {"engine_trades": int(len(m)), "matched_entry": int(found.sum()), "same_exit_candle": int(same_time.sum()),
            "same_exit_price_1bp": int(same_px.sum()), "entry_price_match_1bp": int((found & ((m.entry - m.open_rate).abs() / m.open_rate < 1e-4)).sum()),
            "r_corr_engine_vs_sim": float(np.corrcoef(m.loc[found, "r_multiple"], m.loc[found, "r"])[0, 1]) if found.sum() > 2 else None,
            "mismatch_count": int(len(mism)), "exit_candle_mismatch_count": int((found & ~same_time).sum()),
            "max_rel_price_gap_pct_same_candle": float(same_candle_gap.max()) if len(same_candle_gap) else None,
            "max_rel_price_gap_pct_all": float(m.loc[found, "rel_price_gap_pct"].max()) if found.any() else None,
            "max_abs_r_diff": float(m.loc[found, "r_diff"].abs().max()) if found.any() else None,
            "mismatches": mism.astype(str).to_dict(orient="records")}


# ------------------------------------------------------------------ diagnostics
def adversarial_auc(train: pd.DataFrame, target: pd.DataFrame, cols: list[str], cv: str = "shuffle") -> tuple[float, list]:
    """AUC of a LightGBM telling `train` rows from `target` rows.

    cv "shuffle" (pre-registered): 5-fold stratified shuffled CV. cv "week" (added after review):
    5-fold GroupKFold by calendar week, so rows from the same week (shared market-wide features,
    repeated breakouts on nearby 4h bars) never sit on both sides of a fold.
    """
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold, StratifiedKFold, cross_val_predict
    if min(len(train), len(target)) < 10:
        return float("nan"), []
    x = pd.concat([train[cols], target[cols]], ignore_index=True)
    y = np.r_[np.zeros(len(train)), np.ones(len(target))]
    model = LGBMClassifier(**wf.PARAMS)
    if cv == "week":
        weeks = pd.concat([train.date, target.date], ignore_index=True).dt.to_period("W").astype(str)
        p = cross_val_predict(model, x, y, cv=GroupKFold(5), groups=weeks, method="predict_proba")[:, 1]
        return float(roc_auc_score(y, p)), []
    p = cross_val_predict(model, x, y, cv=StratifiedKFold(5, shuffle=True, random_state=81), method="predict_proba")[:, 1]
    gain = model.fit(x, y).booster_.feature_importance(importance_type="gain")
    top = sorted(zip(cols, gain / max(gain.sum(), 1e-12)), key=lambda z: -z[1])[:5]
    return float(roc_auc_score(y, p)), [(c, round(float(g), 3)) for c, g in top]


def pearson_ci(x, y, level: float = 0.90) -> dict:
    """Pearson r with a Fisher-z CI (assumes independent points; overlapping windows make it wider)."""
    from scipy.stats import norm, pearsonr, spearmanr
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    r = float(pearsonr(x, y)[0])
    z, se = np.arctanh(np.clip(r, -0.999999, 0.999999)), 1 / np.sqrt(len(x) - 3)
    q = norm.ppf(0.5 + level / 2)
    return {"n": int(len(x)), "pearson": r, "ci90": [float(np.tanh(z - q * se)), float(np.tanh(z + q * se))],
            "spearman": float(spearmanr(x, y)[0])}


def diagnostics(table: pd.DataFrame) -> tuple:
    from scipy.stats import pearsonr, spearmanr
    done = table.loc[~table.open_at_end]
    both = done.loc[done.barrier.notna()]
    a = {
        "rows": int(len(both)),
        "pearson_label_r": float(pearsonr(both.barrier, both.r)[0]),
        "spearman_label_r": float(spearmanr(both.barrier, both.r)[0]),
        "pearson_label_win": float(pearsonr(both.barrier, both.win)[0]),
        "auc_label_as_score_for_win": wf.auc(both.win, both.barrier),
        "barrier_yes_rate": float(both.barrier.mean()), "trade_win_rate": float(both.win.mean()),
        "by_label": {("yes" if k == 1 else "no"): {"n": int(len(g)), "mean_r": float(g.r.mean()), "win_rate": float(g.win.mean()),
                                                   "median_r": float(g.r.median()), "sum_r": float(g.r.sum())}
                     for k, g in both.groupby("barrier")},
        "crosstab_label_win": pd.crosstab(both.barrier, both.win).to_dict(),
    }
    win_rows, adv_rows = [], []
    for train_start, ws, we in wf.windows():
        tr = done.loc[ml.usable_for(done.exit_time, done.open_at_end, done.date, train_start, ws)]
        te = table.loc[(table.date >= ws) & (table.date < we)]
        te_done = te.loc[~te.open_at_end]
        rec = {"predict_start": str(ws.date()), "train_signals_usable": int(len(tr)), "oos_signals": int(len(te)),
               "train_win_rate": float(tr.win.mean()), "oos_win_rate": float(te_done.win.mean()) if len(te_done) else None,
               "train_barrier_yes": float(tr.barrier.mean()), "oos_barrier_yes": float(te_done.barrier.mean()) if len(te_done) else None,
               "train_mean_r": float(tr.r.mean()), "oos_mean_r": float(te_done.r.mean()) if len(te_done) else None,
               "oos_long_share": float((te.side == 1).mean()) if len(te) else None,
               "min_train_signals_per_coin": int(tr.groupby("pair").size().reindex(table.pair.unique(), fill_value=0).min()),
               "min_oos_signals_per_coin": int(te.groupby("pair").size().reindex(table.pair.unique(), fill_value=0).min()),
               "max_train_signals_per_coin": int(tr.groupby("pair").size().reindex(table.pair.unique(), fill_value=0).max()),
               "max_oos_signals_per_coin": int(te.groupby("pair").size().reindex(table.pair.unique(), fill_value=0).max())}
        # Control (added after review): earlier ~275 days vs last 90 days of the SAME training year.
        cut = ws - wf.STEP
        early, late = tr.loc[tr.date < cut], tr.loc[tr.date >= cut]
        for fs, cols in FEATURES.items():
            score, top = adversarial_auc(tr, te, cols)
            rec[f"adv_auc_{fs}"] = score
            adv_rows.append({"predict_start": str(ws.date()), "features": fs, "adv_auc": score,
                             "adv_auc_weekcv": adversarial_auc(tr, te, cols, cv="week")[0],
                             "ctrl_early_vs_late_auc": adversarial_auc(early, late, cols)[0],
                             "ctrl_early_vs_late_auc_weekcv": adversarial_auc(early, late, cols, cv="week")[0],
                             "top_features": top})
        win_rows.append(rec)
    by_window = pd.DataFrame(win_rows)
    by_pair = done.groupby("pair").agg(signals=("r", "size"), win_rate=("win", "mean"), barrier_yes=("barrier", "mean"),
                                       mean_r=("r", "mean"), sum_r=("r", "sum")).reset_index()
    yearly = done.assign(year=done.date.dt.year).groupby(["pair", "year"]).agg(n=("r", "size"), win_rate=("win", "mean"), mean_r=("r", "mean")).reset_index()
    r = done.r.sort_values(ascending=False)
    k = max(1, int(round(len(r) * 0.1)))
    e = {"trades": int(len(r)), "win_rate": float((r > 0).mean()), "mean_r": float(r.mean()), "sum_r": float(r.sum()),
         "top10pct_sum_r": float(r.iloc[:k].sum()), "rest_sum_r": float(r.iloc[k:].sum()),
         "top10pct_share_of_gross_wins": float(r.iloc[:k].sum() / r[r > 0].sum()),
         "avg_win_r": float(r[r > 0].mean()), "avg_loss_r": float(r[r <= 0].mean()),
         "r_quantiles": {str(q): float(r.quantile(q)) for q in (0.05, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99)},
         "median_bars_held": float(done.bars_held.median()), "share_held_over_48h": float((done.bars_held > 48).mean()),
         "exit_reasons": done.exit_reason.value_counts().to_dict(),
         "by_side": {("long" if s == 1 else "short"): {"n": int(len(g)), "win_rate": float(g.win.mean()), "mean_r": float(g.r.mean())} for s, g in done.groupby("side")}}
    done_oos = done.loc[(done.date >= OOS) & (done.date < END)]
    e["by_side_oos"] = {("long" if s == 1 else "short"): {"n": int(len(g)), "win_rate": float(g.win.mean()), "mean_r": float(g.r.mean())}
                        for s, g in done_oos.groupby("side")}
    adv = pd.DataFrame(adv_rows)
    adv_cols = ["adv_auc", "adv_auc_weekcv", "ctrl_early_vs_late_auc", "ctrl_early_vs_late_auc_weekcv"]
    summary = {"a_label_mismatch": a, "e_payoff": e,
               "c_adversarial_auc_mean": {fs: float(adv.loc[adv.features == fs, "adv_auc"].mean()) for fs in FEATURES},
               "c_adversarial_with_controls": {fs: {c: {"mean": float(adv.loc[adv.features == fs, c].mean()), "min": float(adv.loc[adv.features == fs, c].min()),
                                                        "max": float(adv.loc[adv.features == fs, c].max())} for c in adv_cols} for fs in FEATURES},
               "b_win_rate_by_window_range": [float(by_window.oos_win_rate.min()), float(by_window.oos_win_rate.max())],
               "b_persistence_train_vs_oos": {"win_rate": pearson_ci(by_window.train_win_rate, by_window.oos_win_rate),
                                              "mean_r": pearson_ci(by_window.train_mean_r, by_window.oos_mean_r),
                                              "note": "13 overlapping 365-day training windows: points are autocorrelated, the true CI is wider"},
               "d_per_coin_counts_range": {"train": [int(by_window.min_train_signals_per_coin.min()), int(by_window.max_train_signals_per_coin.max())],
                                           "oos": [int(by_window.min_oos_signals_per_coin.min()), int(by_window.max_oos_signals_per_coin.max())]}}
    return summary, by_window, by_pair, adv.assign(top_features=adv.top_features.astype(str)), yearly


# ------------------------------------------------------------------ models
def make_model(kind: str, pooled: bool, cols: list[str], pairs: list[str]):
    """Returns (fit(train_df, y) -> predictor(df) -> p)."""
    if kind == "lr":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        def design(df):
            x = df[cols].astype(float).copy()
            if pooled:
                for p in pairs[1:]:   # first coin is the reference level
                    x[f"coin_{p}"] = (df.pair == p).astype(float)
            return x

        def fit(df, y):
            model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=LR_C, max_iter=5000))
            model.fit(design(df), y)
            return lambda d: model.predict_proba(design(d))[:, 1]
        return fit
    from lightgbm import LGBMClassifier

    def design(df):
        x = df[cols].astype(float).copy()
        if pooled:
            x["coin"] = pd.Categorical(df.pair, categories=pairs)
        return x

    def fit(df, y):
        model = LGBMClassifier(**wf.PARAMS)
        model.fit(design(df), y)
        return lambda d: model.predict_proba(design(d))[:, 1]
    return fit


def run_models(table: pd.DataFrame, pairs: list[str]) -> pd.DataFrame:
    done = table.loc[~table.open_at_end]
    out = []
    for kind in ("lr", "lgb"):
        for pooled in (False, True):
            for fs, cols in FEATURES.items():
                name = f"{kind}_{'pool' if pooled else 'pc'}_{fs}"
                fit = make_model(kind, pooled, cols, pairs)
                for train_start, ws, we in wf.windows():
                    tr_all = done.loc[ml.usable_for(done.exit_time, done.open_at_end, done.date, train_start, ws)]
                    te_all = table.loc[(table.date >= ws) & (table.date < we)]
                    groups = [(None, tr_all, te_all)] if pooled else [(p, tr_all.loc[tr_all.pair == p], te_all.loc[te_all.pair == p]) for p in pairs]
                    for pair, tr, te in groups:
                        if te.empty:
                            continue
                        y = tr.win.astype(int)
                        if len(tr) < MIN_TRAIN or y.nunique() < 2:
                            base = float(y.mean()) if len(tr) else 0.5
                            p_tr, p_te, fallback = np.full(len(tr), base), np.full(len(te), base), True
                        else:
                            predict = fit(tr, y)
                            p_tr, p_te, fallback = predict(tr), predict(te), False
                        med = float(np.median(p_tr)) if len(p_tr) else 0.5
                        q33, q67 = (np.quantile(p_tr, [1 / 3, 2 / 3]) if len(p_tr) else (0.5, 0.5))
                        out.append(pd.DataFrame({
                            "model": name, "pair": te.pair.to_numpy(), "date": te.date.to_numpy(), "p": p_te,
                            "thr_median": med, "thr_q33": float(q33), "thr_q67": float(q67), "fallback": fallback,
                            "window_start": ws, "train_rows": len(tr)}))
                print("model done", name, flush=True)
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------------------ evaluation
def bnh(close: pd.Series, start, end) -> float:
    s = close.loc[(close.index >= start) & (close.index < end)]
    return float(s.iloc[-1] / s.iloc[0] * 100 - 100)


def run_portfolio(table: pd.DataFrame, take: pd.Series, pairs: list[str]) -> tuple:
    chosen = table.loc[take.to_numpy()]
    taken, curve = ml.portfolio(chosen, pairs, OOS, END)
    res = {"signals_offered": int(len(chosen)), "trades": int(len(taken)),
           "win_rate": float((taken.pnl > 0).mean() * 100) if len(taken) else None,
           "net_pct": ml.period_return(curve, OOS, END + pd.Timedelta(days=30)),
           "p1_2023q4_2024_pct": ml.period_return(curve, *P1), "p2_2025_2026_pct": ml.period_return(curve, P2[0], END + pd.Timedelta(days=30)),
           "from_2024_pct": ml.period_return(curve, Y2024, END + pd.Timedelta(days=30)),
           "last6m_pct": ml.period_return(curve, LAST6M, END + pd.Timedelta(days=30)),
           "mdd_closed_pct": ml.max_drawdown(curve),
           "pf": float(taken.pnl[taken.pnl > 0].sum() / -taken.pnl[taken.pnl < 0].sum()) if len(taken) and (taken.pnl < 0).any() else None,
           "long_short_pnl": {("long" if s == 1 else "short"): round(float(g.pnl.sum()), 2) for s, g in taken.groupby("side")} if len(taken) else {}}
    for key, (a, b) in (("p1", P1), ("p2", (P2[0], END + pd.Timedelta(days=30)))):
        part = taken.loc[(taken.exit_candle >= a) & (taken.exit_candle < b)] if len(taken) else taken
        res[f"long_short_pnl_{key}"] = {("long" if s == 1 else "short"): round(float(g.pnl.sum()), 2) for s, g in part.groupby("side")} if len(part) else {}
    return res, taken, curve


def random_null(oos: pd.DataFrame, rate: float, pairs: list[str], base: dict, draws: int = NULL_DRAWS, seed: int = NULL_SEED) -> dict:
    """Portfolio returns when each OOS signal is kept at random with probability `rate` (added after review)."""
    rng = np.random.default_rng(seed)
    p1, p2, net = [], [], []
    for _ in range(draws):
        keep = pd.Series(rng.random(len(oos)) < rate, index=oos.index)
        res, _, _ = run_portfolio(oos, keep, pairs)
        p1.append(res["p1_2023q4_2024_pct"])
        p2.append(res["p2_2025_2026_pct"])
        net.append(res["net_pct"])
    p1, p2, net = np.array(p1), np.array(p2), np.array(net)
    return {"rate": rate, "draws": draws, "p1": p1, "p2": p2, "net": net,
            "beats_base_p1": float((p1 > base["p1_2023q4_2024_pct"]).mean()),
            "beats_base_p2": float((p2 > base["p2_2025_2026_pct"]).mean()),
            "beats_base_both": float(((p1 > base["p1_2023q4_2024_pct"]) & (p2 > base["p2_2025_2026_pct"])).mean()),
            "p1_pct_5_50_95": [float(v) for v in np.percentile(p1, [5, 50, 95])],
            "p2_pct_5_50_95": [float(v) for v in np.percentile(p2, [5, 50, 95])]}


def null_rank(port: dict, null: dict) -> dict:
    """Share of random-filter draws the filter beats, per period (0..1)."""
    return {"null_rate": null["rate"], "null_pctile_p1": float((null["p1"] < port["p1_2023q4_2024_pct"]).mean()),
            "null_pctile_p2": float((null["p2"] < port["p2_2025_2026_pct"]).mean()),
            "null_pctile_net": float((null["net"] < port["net_pct"]).mean()),
            "null_beats_base_p1": null["beats_base_p1"], "null_beats_base_p2": null["beats_base_p2"],
            "null_beats_base_both": null["beats_base_both"],
            "null_p1_5_50_95": null["p1_pct_5_50_95"], "null_p2_5_50_95": null["p2_pct_5_50_95"]}


_NULL_CACHE: dict = {}


def null_for(oos: pd.DataFrame, rate: float, pairs: list[str], base: dict) -> dict:
    key = round(float(rate), 3)
    if key not in _NULL_CACHE:
        _NULL_CACHE[key] = random_null(oos, key, pairs, base)
        print("null done", key, {k: v for k, v in _NULL_CACHE[key].items() if k not in ("p1", "p2", "net")}, flush=True)
    return _NULL_CACHE[key]


def evaluate_models(table: pd.DataFrame, preds: pd.DataFrame, pairs: list[str], base: dict) -> tuple[dict, pd.DataFrame, list]:
    summary, by_window, taken_all = {}, [], []
    oos = table.loc[(table.date >= OOS) & (table.date < END)]
    for name, pm in preds.groupby("model", sort=False):
        frame = oos.merge(pm[["pair", "date", "p", "thr_median", "thr_q33", "thr_q67", "fallback"]], on=["pair", "date"], how="left", validate="one_to_one")
        assert frame.p.notna().all(), name
        scored = frame.loc[~frame.open_at_end].copy()
        scored["label"] = scored.win
        top = scored.p >= scored.thr_q67
        bot = scored.p < scored.thr_q33
        above = scored.p > scored.thr_median
        take = frame.p > frame.thr_median
        port, taken, _ = run_portfolio(frame, take, pairs)
        port.update(null_rank(port, null_for(oos, float(take.mean()), pairs, base)))
        taken_all.append(taken.assign(model=name))
        ci = wf.block_bootstrap_auc(scored, "p")
        # Sensitivity CIs (added after review): coarser blocks than weeks.
        scored = scored.merge(pm[["pair", "date", "window_start"]], on=["pair", "date"], how="left", validate="one_to_one")
        top = scored.p >= scored.thr_q67
        bot = scored.p < scored.thr_q33
        above = scored.p > scored.thr_median

        def auc_stat(f):
            return wf.auc(f.win, f.p)

        def gap_stat(f):
            t, b = f.r[f.p >= f.thr_q67], f.r[f.p < f.thr_q33]
            return float(t.mean() - b.mean()) if len(t) and len(b) else float("nan")
        week = scored.date.dt.to_period("W").astype(str)
        month = scored.date.dt.to_period("M").astype(str)
        window = scored.window_start.astype(str)
        rec = {
            "oos_signals_scored": int(len(scored)), "auc": wf.auc(scored.win, scored.p), "auc_ci90": ci,
            "spearman_p_r": float(scored[["p", "r"]].corr(method="spearman").iloc[0, 1]),
            "fallback_share": float(frame.fallback.mean()),
            "pass_rate_above_train_median": float(take.mean()),
            "mean_r_top_third": float(scored.r[top].mean()) if top.any() else None, "n_top": int(top.sum()),
            "mean_r_bottom_third": float(scored.r[bot].mean()) if bot.any() else None, "n_bottom": int(bot.sum()),
            "auc_ci90_month_blocks": ml.block_bootstrap(scored, auc_stat, month),
            "auc_ci90_window_blocks": ml.block_bootstrap(scored, auc_stat, window),
            "top_minus_bottom_r": gap_stat(scored),
            "top_minus_bottom_r_ci90_week": ml.block_bootstrap(scored, gap_stat, week),
            "top_minus_bottom_r_ci90_window": ml.block_bootstrap(scored, gap_stat, window),
            "mean_r_above_median": float(scored.r[above].mean()) if above.any() else None,
            "mean_r_below_median": float(scored.r[~above].mean()) if (~above).any() else None,
            "win_above_median": float(scored.win[above].mean()) if above.any() else None,
            "auc_2024_plus": wf.auc(scored.win[scored.date >= Y2024], scored.p[scored.date >= Y2024]),
            "auc_last6m": wf.auc(scored.win[scored.date >= LAST6M], scored.p[scored.date >= LAST6M]),
            "portfolio": port,
        }
        rec["pass_auc_ci"] = bool(ci[0] > 0.5)
        rec["pass_p1"] = bool(port["p1_2023q4_2024_pct"] > base["p1_2023q4_2024_pct"])
        rec["pass_p2"] = bool(port["p2_2025_2026_pct"] > base["p2_2025_2026_pct"])
        rec["pass_all"] = rec["pass_auc_ci"] and rec["pass_p1"] and rec["pass_p2"]
        summary[name] = rec
        for ws, g in scored.groupby("window_start"):
            by_window.append({"model": name, "predict_start": str(pd.Timestamp(ws).date()), "n": int(len(g)),
                              "auc": wf.auc(g.win, g.p), "mean_r_above": float(g.r[g.p > g.thr_median].mean()) if (g.p > g.thr_median).any() else None,
                              "mean_r_below": float(g.r[g.p <= g.thr_median].mean()) if (g.p <= g.thr_median).any() else None})
        print(name, {k: rec[k] for k in ("auc", "auc_ci90", "mean_r_top_third", "mean_r_bottom_third")}, port["net_pct"], flush=True)
    return summary, pd.DataFrame(by_window), taken_all


RULES = {
    "long_only": lambda t: t.side == 1,
    "btc_only": lambda t: t.pair == "BTC/USDT:USDT",
    "btc_eth": lambda t: t.pair.isin(["BTC/USDT:USDT", "ETH/USDT:USDT"]),
    "adx25": lambda t: t.adx >= 25,
    "btc_regime": lambda t: t.side == t.btc_trend_side,
}


def evaluate_rules(table: pd.DataFrame, pairs: list[str], base: dict) -> dict:
    oos = table.loc[(table.date >= OOS) & (table.date < END)]
    scored = oos.loc[~oos.open_at_end]
    out = {}
    for name, rule in RULES.items():
        take = rule(oos).fillna(False).astype(bool)
        keep = rule(scored).fillna(False).astype(bool)
        port, _, _ = run_portfolio(oos, take, pairs)
        port.update(null_rank(port, null_for(oos, float(take.mean()), pairs, base)))
        out[name] = {"pass_share": float(take.mean()), "mean_r_pass": float(scored.r[keep].mean()) if keep.any() else None,
                     "mean_r_fail": float(scored.r[~keep].mean()) if (~keep).any() else None,
                     "win_pass": float(scored.win[keep].mean()) if keep.any() else None,
                     "portfolio": port,
                     "pass_p1": bool(port["p1_2023q4_2024_pct"] > base["p1_2023q4_2024_pct"]),
                     "pass_p2": bool(port["p2_2025_2026_pct"] > base["p2_2025_2026_pct"])}
    return out


def main(mode: str = "spec"):
    """mode spec = pre-registered trade rules; engine = Freqtrade stop handling (sensitivity)."""
    global OUT
    if mode not in ("spec", "engine"):
        raise SystemExit("mode must be spec or engine")
    if mode == "engine":
        OUT = ROOT / "results" / "engine_mode"
    OUT.mkdir(parents=True, exist_ok=True)
    selection = load_selection()
    pairs = [r["pair"] for r in selection]
    table, data, frames = build_signal_table(selection, mode)
    keep_cols = ["pair", "date", "side", "entry_time", "entry", "atr", "exit_candle", "exit_time", "exit_price", "exit_reason",
                 "open_at_end", "bars_held", "funding_per_unit", "fees_per_unit", "r_gross", "r", "win", "barrier", "btc_trend_side"]
    table[keep_cols + INDICATOR_FEATURES + DERIV_FEATURES].to_csv(OUT / "signals_metalabel.csv.gz", index=False, compression="gzip")
    print("signals", len(table), "open_at_end", int(table.open_at_end.sum()), flush=True)

    check = engine_check(table)
    (OUT / "sim_vs_engine.json").write_text(json.dumps(check, indent=1, default=str), encoding="utf-8")
    print("engine check", {k: v for k, v in check.items() if k != "mismatches_head"}, flush=True)

    diag, by_window, by_pair, adv, yearly = diagnostics(table)
    by_window.to_csv(OUT / "diag_by_window.csv", index=False)
    by_pair.to_csv(OUT / "diag_by_pair.csv", index=False)
    yearly.to_csv(OUT / "diag_by_pair_year.csv", index=False)
    adv.to_csv(OUT / "diag_adversarial.csv", index=False)
    (OUT / "diagnostics.json").write_text(json.dumps(diag, indent=1, default=str), encoding="utf-8")
    print("diagnostics", json.dumps(diag, default=str)[:1500], flush=True)

    oos = table.loc[(table.date >= OOS) & (table.date < END)]
    base, base_taken, base_curve = run_portfolio(oos, pd.Series(True, index=oos.index), pairs)
    base_taken.to_csv(OUT / "portfolio_all_signals_trades.csv", index=False)
    closes = {p: frames[p].set_index("date").close for p in pairs}
    btc = closes["BTC/USDT:USDT"]
    btc_hold = {"oos": bnh(btc, OOS, END), "p1": bnh(btc, *P1), "p2": bnh(btc, *P2), "from_2024": bnh(btc, Y2024, END), "last6m": bnh(btc, LAST6M, END)}
    print("all signals", base, "btc", btc_hold, flush=True)

    preds = run_models(table, pairs)
    preds.to_csv(OUT / "oos_predictions.csv.gz", index=False, compression="gzip")
    models, model_windows, taken = evaluate_models(table, preds, pairs, base)
    model_windows.to_csv(OUT / "model_by_window.csv", index=False)
    pd.concat(taken, ignore_index=True).to_csv(OUT / "portfolio_model_trades.csv.gz", index=False, compression="gzip")
    rules = evaluate_rules(table, pairs, base)
    # Descriptive only (added after review): does a window's train/test separability go with a
    # worse model AUC in that window? 13 points per model.
    adv = pd.read_csv(OUT / "diag_adversarial.csv")
    adv_vs_model = {}
    for name, g in model_windows.groupby("model"):
        fs = "ind_der" if name.endswith("ind_der") else "ind"
        merged = g.merge(adv.loc[adv.features == fs, ["predict_start", "adv_auc", "adv_auc_weekcv"]], on="predict_start")
        adv_vs_model[name] = {"vs_adv_auc": pearson_ci(merged.adv_auc, merged.auc), "vs_adv_auc_weekcv": pearson_ci(merged.adv_auc_weekcv, merged.auc)}
    half = null_for(oos, 0.5, pairs, base)
    summary = {"pairs": pairs, "signals_total": int(len(table)), "signals_oos": int(len(oos)),
               "open_at_end": int(table.open_at_end.sum()), "all_signals_portfolio": base, "btc_hold_pct": btc_hold,
               "models": models, "rules": rules,
               "null_random_half": {k: v for k, v in half.items() if k not in ("p1", "p2", "net")},
               "model_auc_vs_adversarial_auc": adv_vs_model,
               "models_passing": [m for m, r in models.items() if r["pass_all"]],
               "rules_passing": [r for r, v in rules.items() if v["pass_p1"] and v["pass_p2"]]}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    rows = [{"name": "all_signals", **{k: v for k, v in base.items() if not k.startswith("long_short_pnl")}}]
    for m, r in models.items():
        rows.append({"name": m, "auc": r["auc"], "auc_lo": r["auc_ci90"][0], "auc_hi": r["auc_ci90"][1],
                     "auc_lo_month": r["auc_ci90_month_blocks"][0], "auc_hi_month": r["auc_ci90_month_blocks"][1],
                     "auc_lo_window": r["auc_ci90_window_blocks"][0], "auc_hi_window": r["auc_ci90_window_blocks"][1],
                     "r_top3": r["mean_r_top_third"], "r_bot3": r["mean_r_bottom_third"],
                     "gap_r": r["top_minus_bottom_r"], "gap_lo_week": r["top_minus_bottom_r_ci90_week"][0], "gap_hi_week": r["top_minus_bottom_r_ci90_week"][1],
                     "gap_lo_window": r["top_minus_bottom_r_ci90_window"][0], "gap_hi_window": r["top_minus_bottom_r_ci90_window"][1],
                     "pass_all": r["pass_all"],
                     **{k: v for k, v in r["portfolio"].items() if not k.startswith("long_short_pnl")}})
    for n, r in rules.items():
        rows.append({"name": f"rule_{n}", "r_top3": r["mean_r_pass"], "r_bot3": r["mean_r_fail"], "pass_all": r["pass_p1"] and r["pass_p2"],
                     **{k: v for k, v in r["portfolio"].items() if not k.startswith("long_short_pnl")}})
    pd.DataFrame(rows).to_csv(OUT / "portfolio_summary.csv", index=False)
    print(pd.DataFrame(rows).round(3).to_string(), flush=True)
    print("passing models", summary["models_passing"], "passing rules", summary["rules_passing"], flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "spec")
