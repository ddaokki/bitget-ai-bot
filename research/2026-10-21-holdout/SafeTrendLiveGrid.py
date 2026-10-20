"""Research-only parameter grid around SafeTrendLive (Task B, parameter robustness).

Subclasses the live strategy unchanged and exposes four knobs as class attributes:
  DC_LEN     Donchian entry length on 4h bars          (live 20)
  STOP_ATR   initial stop distance in 4h-ATR units     (live 2.0)
  TRAIL_ATR  chandelier distance in 4h-ATR units       (live 3.0)
  ADX_MIN    4h ADX floor for entries                  (live 20)

Everything else (Donchian-10 exit, ATR% q90 filter, daily EMA trend, 2x leverage,
0.5% risk, entry caps, protections, blackout, chandelier_stop custom exit) is inherited.
Sizing keeps 0.5% risk at the initial stop, so STOP_ATR also changes the position size.

One class per grid point is generated below, named
  Grid_d{DC_LEN}_s{STOP_ATR}_c{TRAIL_ATR}_a{ADX_MIN}  (dots replaced by "p"),
so each backtest result carries its own setting in the strategy name.

Mount this file next to SafeTrendLive.py / live_rules.py in the strategies directory.
It is never used by the live bot.
"""
import sys
from datetime import datetime
from pathlib import Path

import talib.abstract as ta
from freqtrade.strategy import informative, stoploss_from_absolute
from pandas import DataFrame

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_rules as rules  # noqa: E402
from SafeTrendLive import ENTRY_TAG, SafeTrendLive  # noqa: E402


class SafeTrendLiveGrid(SafeTrendLive):
    DC_LEN = 20
    STOP_ATR = rules.STOP_ATR
    TRAIL_ATR = rules.TRAIL_ATR
    ADX_MIN = 20

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Same as the live 4h block except the entry channel length.
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["dc_high"] = dataframe.high.rolling(self.DC_LEN).max().shift(1)
        dataframe["dc_low"] = dataframe.low.rolling(self.DC_LEN).min().shift(1)
        dataframe["exit_low"] = dataframe.low.rolling(10).min().shift(1)
        dataframe["exit_high"] = dataframe.high.rolling(10).max().shift(1)
        dataframe["atr_pct"] = dataframe.atr / dataframe.close
        dataframe["atr_q90"] = dataframe.atr_pct.rolling(180, min_periods=180).quantile(0.9).shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = None
        long = (
            dataframe.four_close
            & (dataframe.adx_4h > self.ADX_MIN)
            & (dataframe.atr_pct_4h <= dataframe.atr_q90_4h)
            & (dataframe.volume > 0)
            & (dataframe.ema50_1d > dataframe.ema200_1d)
            & (dataframe.close_4h > dataframe.dc_high_4h)
        )
        long &= ~self._blackout_mask(dataframe.date)
        dataframe.loc[long, ["enter_long", "enter_tag"]] = (1, ENTRY_TAG)
        return dataframe

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        if not rules.is_strategy_tag(entry_tag):
            return proposed_stake
        history = self._history(pair, current_time)
        if history.empty:
            return 0.0
        market = self.dp.market(pair) if self.dp else None
        limits = (market or {}).get("limits") or {}
        minimum = rules.exchange_min_margin((limits.get("amount") or {}).get("min"), (limits.get("cost") or {}).get("min"),
                                            float((market or {}).get("contractSize") or 1.0), current_rate, leverage)
        return rules.risk_stake(self.wallets.get_total_stake_amount(), current_rate, float(history.iloc[-1].atr_4h),
                                leverage, minimum, max_stake, stop_atr=self.STOP_ATR)

    def custom_stoploss(self, pair: str, trade, current_time: datetime, current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> float | None:
        # Copy of the live method with the grid's stop / chandelier distances.
        if not rules.is_strategy_tag(trade.enter_tag):
            stop = float(trade.open_rate) * (1 - rules.MANUAL_STOP_PRICE_PCT)
            return stoploss_from_absolute(stop, current_rate=current_rate, is_short=False, leverage=trade.leverage)
        history = self._history(pair, current_time)
        if history.empty:
            return None
        entry_atr = self._entry_atr(trade, history)
        if entry_atr is None:
            return None
        entry = trade.get_custom_data("entry_open")
        if entry is None:
            entry = float(trade.open_rate)
            trade.set_custom_data("entry_open", entry)
        since = history.loc[history.date >= self._entry_candle(trade)]
        highest = float(since.high.max()) if not since.empty else None
        previous = trade.get_custom_data("stop_ratchet")
        stop = rules.chandelier_stop(float(entry), entry_atr, highest, float(history.iloc[-1].atr_4h),
                                     None if previous is None else float(previous),
                                     stop_atr=self.STOP_ATR, trail_atr=self.TRAIL_ATR)
        trade.set_custom_data("stop_ratchet", float(stop))
        return stoploss_from_absolute(stop, current_rate=current_rate, is_short=False, leverage=trade.leverage)


def grid_name(dc: int, stop: float, trail: float, adx: int) -> str:
    return f"Grid_d{dc}_s{stop:.1f}_c{trail:.1f}_a{adx}".replace(".", "p")


LIVE = (20, 2.0, 3.0, 20)
# One-factor-at-a-time around the live setting, plus the full stop x chandelier grid.
GRID = sorted(
    {(dc, 2.0, 3.0, 20) for dc in (15, 20, 30)}
    | {(20, 2.0, 3.0, adx) for adx in (15, 20, 25)}
    | {(20, s, c, 20) for s in (1.5, 2.0, 2.5) for c in (2.5, 3.0, 4.0)}
)

# Freqtrade finds a strategy by searching the file text for "class <Name>(", so every grid
# point is written out as its own class statement (checked against GRID below).
class Grid_d15_s2p0_c3p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 15, 2.0, 3.0, 20  # noqa: E701
class Grid_d30_s2p0_c3p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 30, 2.0, 3.0, 20  # noqa: E701
class Grid_d20_s2p0_c3p0_a15(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.0, 3.0, 15  # noqa: E701
class Grid_d20_s2p0_c3p0_a25(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.0, 3.0, 25  # noqa: E701
class Grid_d20_s1p5_c2p5_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 1.5, 2.5, 20  # noqa: E701
class Grid_d20_s1p5_c3p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 1.5, 3.0, 20  # noqa: E701
class Grid_d20_s1p5_c4p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 1.5, 4.0, 20  # noqa: E701
class Grid_d20_s2p0_c2p5_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.0, 2.5, 20  # noqa: E701
class Grid_d20_s2p0_c3p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.0, 3.0, 20  # noqa: E701
class Grid_d20_s2p0_c4p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.0, 4.0, 20  # noqa: E701
class Grid_d20_s2p5_c2p5_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.5, 2.5, 20  # noqa: E701
class Grid_d20_s2p5_c3p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.5, 3.0, 20  # noqa: E701
class Grid_d20_s2p5_c4p0_a20(SafeTrendLiveGrid): DC_LEN, STOP_ATR, TRAIL_ATR, ADX_MIN = 20, 2.5, 4.0, 20  # noqa: E701


for _p in GRID:
    _cls = globals()[grid_name(*_p)]
    assert (_cls.DC_LEN, _cls.STOP_ATR, _cls.TRAIL_ATR, _cls.ADX_MIN) == _p, _cls.__name__
assert len(GRID) == 13
