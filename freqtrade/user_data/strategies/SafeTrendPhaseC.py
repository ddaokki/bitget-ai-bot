"""Phase C research strategies (historical only; bot_start refuses dry-run/live).

The walk-forward probabilities come from research/2026-10-21-derivs/walkforward_c.py
(one LightGBM pooled over 10 coins, each 90-day window predicted by a model trained
only on the 365 days before it). This strategy just looks p up by (pair, candle open
time) from config["precomputed"]["path"]; a missing p blocks the entry.

SafeTrendPrecomp        phase B AIV1 rule: SafeTrendV0 signal AND p >= 0.5.
SafeTrendPrecompTiered  phase B TieredV1 rule: §7.1a tiers (0.45/0.55/0.65) + §7.1 blackout.
"""
import event_impact  # noqa: F401 - import now, while the strategy folder is on sys.path: SafeTrendV0's blackout imports it lazily at runtime
import numpy as np
import pandas as pd
from pandas import DataFrame
from SafeTrendAITiered import TieredMixin
from SafeTrendV0 import SafeTrendV0


class SafeTrendPrecomp(SafeTrendV0):
    ai_threshold = 0.5

    def bot_start(self, **kwargs):
        super().bot_start(**kwargs)
        cfg = self.config.get("precomputed") or {}
        if not cfg.get("path"):
            raise RuntimeError(f"{type(self).__name__} needs config precomputed.path (walkforward_c.py output)")
        frame = pd.read_feather(cfg["path"])
        column = cfg.get("column", "p_cal")
        frame["date"] = pd.to_datetime(frame.date, utc=True).dt.as_unit("ns")
        self._p = {pair: group.set_index("date")[column] for pair, group in frame.groupby("pair")}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        series = self._p.get(metadata["pair"])
        dates = pd.to_datetime(dataframe.date, utc=True).dt.as_unit("ns")
        dataframe["ai_p"] = dates.map(series).astype(float) if series is not None else np.nan
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        p = dataframe.ai_p
        blocked = ~(p >= self.ai_threshold)
        dataframe.loc[blocked, ["enter_long", "enter_short"]] = 0
        signal = (dataframe.enter_long == 1) | (dataframe.enter_short == 1)
        if signal.any():
            dataframe.loc[signal, "enter_tag"] = dataframe.loc[signal, "enter_tag"] + "|p=" + p[signal].round(3).astype(str)
        dataframe.loc[~signal, "enter_tag"] = None
        return dataframe


class SafeTrendPrecompTiered(TieredMixin, SafeTrendPrecomp):
    use_blackout = True
