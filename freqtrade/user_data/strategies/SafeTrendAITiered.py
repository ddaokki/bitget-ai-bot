"""Historical-only confidence tiers (spec §7.1a) on the SafeTrendAIV0 model.

Same features, labels and FreqAI identifier as SafeTrendAIV0, so FreqAI reuses
the saved walk-forward predictions and p is identical between both runs.
Tier thresholds are the spec's FIXED values; they were not tuned on the test
period. The LLM ALLOW and event-blackout conditions of the 'high' tier do not
exist in this backtest, so 'high' here means p >= 0.65 only.
"""
import numpy as np
import pandas as pd
from pandas import DataFrame
from SafeTrendAIV0 import SafeTrendAIV0
from SafeTrendV0 import PROFILES

TIER_PROFILES = {
    **PROFILES,
    # high: 1.5% risk, 3x, 2xATR stop, no fixed TP, 4xATR chandelier, no Donchian exit
    "high": {"risk": 0.015, "leverage": 3.0, "stop_atr": 2.0, "trail_atr": 4.0, "tp_atr": None, "donchian_exit": False},
    # mid: same as the baseline
    "mid": {"risk": 0.01, "leverage": 2.0, "stop_atr": 2.0, "trail_atr": 3.0, "tp_atr": None, "donchian_exit": True},
    # low: 0.5% risk, 2x, 1.5xATR stop, fixed 2xATR take-profit, no trailing
    "low": {"risk": 0.005, "leverage": 2.0, "stop_atr": 1.5, "trail_atr": None, "tp_atr": 2.0, "donchian_exit": True},
}
THRESHOLDS = (("high", 0.65), ("mid", 0.55), ("low", 0.45))


def tier_for(p: pd.Series) -> pd.Series:
    tiers = pd.Series(None, index=p.index, dtype=object)
    for name, floor in reversed(THRESHOLDS):
        tiers[p >= floor] = name
    return tiers


class SafeTrendAITiered(SafeTrendAIV0):
    profiles = TIER_PROFILES
    ai_threshold = THRESHOLDS[-1][1]

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        signal = (dataframe.enter_long == 1) | (dataframe.enter_short == 1)
        tiers = tier_for(dataframe.ai_p)
        dataframe["ai_tier"] = tiers
        name = dataframe.enter_tag.str.split("|").str[0]
        p = dataframe.ai_p.round(3).astype(str)
        dataframe.loc[signal, "enter_tag"] = name[signal] + "|tier=" + tiers[signal].astype(str) + "|p=" + p[signal]
        return dataframe
