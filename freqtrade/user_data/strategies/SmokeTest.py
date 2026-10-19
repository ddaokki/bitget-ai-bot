"""Manual-only smoke strategy. Never emits automatic entry or exit signals."""

from datetime import datetime

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class SmokeTest(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    stoploss = -0.06  # At 2x leverage, this is a 3% price move.
    minimal_roi = {"0": 100}
    use_exit_signal = False
    trailing_stop = False
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return 2.0
