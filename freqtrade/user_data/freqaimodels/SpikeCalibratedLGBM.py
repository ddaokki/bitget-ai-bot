"""Research-only FreqAI model: LightGBM + walk-forward event weights + Platt calibration.

Config (freqai.spike_model):
  calibrate:      fit LightGBM on the first part of each training window and a Platt
                  map on a purged, time-ordered holdout at the END of that window.
  event_weights:  recompute the spec §18 event-impact table from THIS training
                  window's BTC/ETH prices only, keep groups with ratio >= 1.15 and
                  n >= 8, and fill the strategy's %-ev_w_* placeholder columns with
                  impact-weighted features (event_impact.weighted_event_features).
  holdout_frac, purge_rows: calibration split (defaults 0.25, 12 labelled 4h rows = 48h).

Side files per window (dk.data_path): spike_window.json (weights, kept groups,
calibration fit) and spike_raw_cal.feather (date, raw p, calibrated p, do_predict)
for the out-of-sample reliability report. The full-period impact table is never used.
"""
import json
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from freqtrade.data.history.datahandlers.featherdatahandler import FeatherDataHandler
from freqtrade.enums import CandleType
from freqtrade.freqai.data_kitchen import FreqaiDataKitchen
from freqtrade.freqai.prediction_models.LightGBMClassifier import LightGBMClassifier

sys.path.insert(0, "/freqtrade/user_data/strategies")
from event_impact import WEIGHTED_FEATURES, impact_table, load_calendar, weighted_event_features, weights_from_table  # noqa: E402
from spike_calibration import CalibratedModel, PlattMap, purged_holdout_split  # noqa: E402

logger = logging.getLogger(__name__)
IMPACT_PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT")


class SpikeCalibratedLGBM(LightGBMClassifier):
    def _spike(self) -> dict:
        return self.freqai_info.get("spike_model", {})

    def _calendar(self) -> pd.DataFrame:
        if not hasattr(self, "_spike_calendar"):
            path = (self.config.get("event_features") or {}).get("path", "/freqtrade/user_data/events/events.csv")
            self._spike_calendar = load_calendar(path)
        return self._spike_calendar

    def _prices(self) -> dict:
        if not hasattr(self, "_spike_prices"):
            handler = FeatherDataHandler(Path(self.config["datadir"]))
            prices = {}
            for pair in IMPACT_PAIRS:
                frame = handler.ohlcv_load(pair, "1h", candle_type=CandleType.FUTURES, fill_missing=False, drop_incomplete=False)
                prices[pair] = frame.set_index("date").close
            self._spike_prices = prices
        return self._spike_prices

    def _fill_event_features(self, frame: pd.DataFrame, weights: dict) -> pd.DataFrame:
        frame = frame.copy()
        feats = weighted_event_features(frame["date"], self._calendar(), weights)
        for column in WEIGHTED_FEATURES:
            name = f"%-{column}"
            if name not in frame:
                raise ValueError(f"strategy must provide placeholder column {name}")
            frame[name] = feats[column].to_numpy()
        return frame

    def train(self, unfiltered_df: pd.DataFrame, pair: str, dk: FreqaiDataKitchen, **kwargs) -> Any:
        cfg = self._spike()
        weights, table = {}, None
        start = pd.Timestamp(unfiltered_df["date"].iloc[0])
        end = pd.Timestamp(unfiltered_df["date"].iloc[-1]) + pd.Timedelta(hours=1)
        if cfg.get("event_weights"):
            # Prices and the baseline are cut to [start, end): only this window's data.
            table = impact_table(self._calendar(), self._prices(), start, end)
            weights = weights_from_table(table)
            unfiltered_df = self._fill_event_features(unfiltered_df, weights)
            logger.info(f"{pair} window {start:%Y-%m-%d}..{end:%Y-%m-%d}: {len(weights)} kept event groups {sorted(weights)}")
        model = super().train(unfiltered_df, pair, dk, **kwargs)
        if isinstance(model, CalibratedModel):
            model.event_weights = weights
            info = dict(model.info)
        else:
            info = {}
        info.update({
            "pair": pair, "train_start": str(start), "train_end": str(end), "event_weights": weights,
            "impact_table": None if table is None else table.replace({np.nan: None}).to_dict(orient="records"),
        })
        Path(dk.data_path).mkdir(parents=True, exist_ok=True)
        (Path(dk.data_path) / "spike_window.json").write_text(json.dumps(info, indent=1, default=str), encoding="utf-8")
        return model

    def fit(self, data_dictionary: dict, dk: FreqaiDataKitchen, **kwargs) -> Any:
        cfg = self._spike()
        if not cfg.get("calibrate"):
            return CalibratedModel(super().fit(data_dictionary, dk, **kwargs), PlattMap())
        X = data_dictionary["train_features"]
        y = data_dictionary["train_labels"]
        w = data_dictionary["train_weights"]
        fit_idx, hold_idx = purged_holdout_split(len(X), cfg.get("holdout_frac", 0.25), cfg.get("purge_rows", 12))
        part = dict(data_dictionary)
        part["train_features"] = X.iloc[fit_idx]
        part["train_labels"] = y.iloc[fit_idx]
        part["train_weights"] = np.asarray(w)[fit_idx]
        base = super().fit(part, dk, **kwargs)
        model = CalibratedModel(base, PlattMap())
        raw = model.raw_yes(X.iloc[hold_idx])
        hit = (y.iloc[hold_idx, 0].astype(str) == "yes").to_numpy()
        model.platt = PlattMap.fit(raw, hit)
        model.info = {
            "rows": int(len(X)), "fit_rows": int(len(fit_idx)), "holdout_rows": int(len(hold_idx)),
            "holdout_yes_rate": float(hit.mean()) if len(hit) else None,
            "holdout_raw_mean": float(raw.mean()) if len(raw) else None,
            "platt_a": model.platt.a, "platt_b": model.platt.b, "platt_fitted": model.platt.fitted,
        }
        return model

    def predict(self, unfiltered_df: pd.DataFrame, dk: FreqaiDataKitchen, **kwargs):
        if self._spike().get("event_weights"):
            unfiltered_df = self._fill_event_features(unfiltered_df, getattr(self.model, "event_weights", {}))
        pred_df, do_predict = super().predict(unfiltered_df, dk, **kwargs)
        if isinstance(self.model, CalibratedModel):
            raw = self.model.raw_yes(dk.data_dictionary["prediction_features"])
            side = pd.DataFrame({
                "date": pd.to_datetime(unfiltered_df["date"]).to_numpy(), "raw_yes": raw,
                "cal_yes": pred_df["yes"].to_numpy(), "do_predict": np.asarray(do_predict),
            })
            Path(dk.data_path).mkdir(parents=True, exist_ok=True)
            side.to_feather(Path(dk.data_path) / "spike_raw_cal.feather")
        return pred_df, do_predict
