"""Historical-only LightGBM entry filter on SafeTrendV0 signals (spec §18 stage 1).

Future outcomes are labels, never features. FreqAI 2026.9 calls
set_freqai_targets on data truncated at each training-window end, so the last
48 training rows have NaN labels and no label sees the prediction window.
"""
import numpy as np
import pandas as pd
from event_features import FEATURE_COLUMNS, compute_event_features, load_events
from freqtrade.strategy import BooleanParameter
from pandas import DataFrame
from SafeTrendV0 import SafeTrendV0

LABEL = "&-barrier"
HORIZON = 48


def forward_labels(dataframe: DataFrame, horizon: int = HORIZON, rows: pd.Series | None = None) -> pd.Series:
    """'yes' if +2xATR is touched before -2xATR within `horizon` hourly bars.

    Direction follows the daily trend (EMA50 vs EMA200). Entry is the next 1h
    open. Both barriers inside one candle count as a loss ('no'); unresolved
    paths are 'no'. Rows without a complete future window are NaN. When `rows`
    is given, only those rows get labels (the rest are NaN).
    """
    count = len(dataframe)
    result = pd.Series(np.nan, index=dataframe.index, dtype=object)
    if count <= horizon:
        return result
    atr = dataframe.atr_4h.to_numpy(dtype=float)
    entry = dataframe.open.shift(-1).to_numpy(dtype=float)
    ema50 = dataframe.ema50_1d.to_numpy(dtype=float)
    ema200 = dataframe.ema200_1d.to_numpy(dtype=float)
    high = dataframe.high.to_numpy(dtype=float)
    low = dataframe.low.to_numpy(dtype=float)
    mask = np.ones(count, dtype=bool) if rows is None else np.asarray(rows, dtype=bool)
    values = np.full(count, np.nan, dtype=object)
    for i in range(count - horizon):
        if not mask[i] or not np.isfinite(atr[i]) or atr[i] <= 0 or not np.isfinite(ema200[i]) or not np.isfinite(ema50[i]):
            continue
        direction = 1 if ema50[i] >= ema200[i] else -1
        target = entry[i] + direction * 2 * atr[i]
        stop = entry[i] - direction * 2 * atr[i]
        outcome = "no"
        for j in range(i + 1, i + horizon + 1):
            stop_hit = low[j] <= stop if direction == 1 else high[j] >= stop
            target_hit = high[j] >= target if direction == 1 else low[j] <= target
            if stop_hit:
                break
            if target_hit:
                outcome = "yes"
                break
        values[i] = outcome
    return pd.Series(values, index=dataframe.index, dtype=object)


class SafeTrendAIV0(SafeTrendV0):
    # Event features are OFF unless this parameter or config["event_features"]["enabled"] is true.
    use_event_features = BooleanParameter(default=False, space="buy", optimize=False, load=True)
    ai_threshold = 0.5

    def events_enabled(self) -> bool:
        cfg = self.config.get("event_features") or {}
        return bool(cfg.get("enabled", False)) or bool(self.use_event_features.value)

    def events_path(self) -> str:
        cfg = self.config.get("event_features") or {}
        return cfg.get("path", "/freqtrade/user_data/events/events.csv")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        self.freqai.class_names = ["no", "yes"]
        return self.freqai.start(dataframe, metadata, self)

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        if "atr_4h" not in dataframe:
            dataframe = SafeTrendV0.populate_indicators(self, dataframe, metadata)
        atr = dataframe.atr_4h
        dataframe["%-return_1h"] = dataframe.close.pct_change()
        dataframe["%-return_24h"] = dataframe.close.pct_change(24)
        dataframe["%-return_72h"] = dataframe.close.pct_change(72)
        dataframe["%-volume_ratio"] = dataframe.volume / dataframe.volume.rolling(24).mean()
        dataframe["%-atr_pct"] = dataframe.atr_pct_4h
        dataframe["%-atr_rank180"] = dataframe.atr_rank_4h
        dataframe["%-adx"] = dataframe.adx_4h
        dataframe["%-rsi_4h"] = dataframe.rsi_4h
        dataframe["%-daily_trend"] = dataframe.ema50_1d / dataframe.ema200_1d - 1
        dataframe["%-dist_ema200_atr"] = (dataframe.close - dataframe.ema200_1d) / atr
        dataframe["%-channel_position"] = (dataframe.close - dataframe.dc_low_4h) / (dataframe.dc_high_4h - dataframe.dc_low_4h)
        dataframe["%-channel_width_atr"] = (dataframe.dc_high_4h - dataframe.dc_low_4h) / atr
        dataframe["%-trend_side"] = np.where(dataframe.ema50_1d >= dataframe.ema200_1d, 1, -1)
        if self.events_enabled():
            events = load_events(self.events_path())
            features = compute_event_features(dataframe.date, events)
            for column in FEATURE_COLUMNS:
                dataframe[f"%-{column}"] = features[column].to_numpy()
        feature_cols = [c for c in dataframe.columns if c.startswith("%")]
        dataframe[feature_cols] = dataframe[feature_cols].replace([np.inf, -np.inf], np.nan)
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        # Label only rows where a 4h bar has just closed: that is where entries can
        # happen, and it cuts the overlap between neighbouring 48h windows by 4x.
        dataframe[LABEL] = forward_labels(dataframe, rows=dataframe.four_close)
        return dataframe

    def probability(self, dataframe: DataFrame) -> pd.Series:
        p = dataframe["yes"] if "yes" in dataframe else pd.Series(np.nan, index=dataframe.index)
        return p.where(dataframe.get("do_predict", 0) == 1)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        p = self.probability(dataframe)
        dataframe["ai_p"] = p
        blocked = ~(p >= self.ai_threshold)
        dataframe.loc[blocked, ["enter_long", "enter_short"]] = 0
        signal = (dataframe.enter_long == 1) | (dataframe.enter_short == 1)
        dataframe.loc[signal, "enter_tag"] = dataframe.loc[signal, "enter_tag"] + "|p=" + p[signal].round(3).astype(str)
        dataframe.loc[~signal, "enter_tag"] = None
        return dataframe
