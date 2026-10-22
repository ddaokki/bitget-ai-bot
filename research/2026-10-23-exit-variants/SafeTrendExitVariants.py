"""Exit-rule variants of the live SafeTrendLive, for backtest comparison only (2026-10-23).

Mounted next to the unchanged live `SafeTrendLive.py` / `live_rules.py`. Entries, sizing,
blackout and the exit signal are the live ones; only the stop/exit management differs:

- SafeTrendTrail25: chandelier trail 2.5 x ATR(4h) instead of 3.0.
- SafeTrendBreakeven: once the highest closed-1h high since entry reaches entry + 1R
  (R = initial stop distance = 2 x ATR at entry), the stop never sits below entry + 0.12%
  (the round-trip taker fee), so the trade cannot turn into a loss after that point.
- SafeTrendPartial: at entry + 1.5R (checked on the candle open, as Freqtrade's backtest
  does for position adjustment) sell half once; the rest keeps the live stop and exits.
  Freqtrade skips the partial exit when the half or the remainder is below the exchange
  minimum, exactly as it would live; such skips are reported.

No lookahead: every level uses closed 1h candles before `current_time` (the live
`_history`) or the candle open for the partial exit.
"""
import sys
from datetime import datetime
from pathlib import Path

from freqtrade.strategy import stoploss_from_absolute

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_rules as rules  # noqa: E402
from SafeTrendLive import SafeTrendLive  # noqa: E402

ROUND_TRIP_FEE = 2 * 0.0006


class _ExitVariant(SafeTrendLive):
    trail_atr = rules.TRAIL_ATR
    breakeven_r: float | None = None

    def custom_stoploss(self, pair: str, trade, current_time: datetime, current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> float | None:
        if not rules.is_strategy_tag(trade.enter_tag):
            return super().custom_stoploss(pair, trade, current_time, current_rate, current_profit, after_fill, **kwargs)
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
        entry = float(entry)
        since = history.loc[history.date >= self._entry_candle(trade)]
        highest = float(since.high.max()) if not since.empty else None
        previous = trade.get_custom_data("stop_ratchet")
        stop = rules.chandelier_stop(entry, entry_atr, highest, float(history.iloc[-1].atr_4h),
                                     None if previous is None else float(previous), trail_atr=self.trail_atr)
        if self.breakeven_r is not None and highest is not None:
            risk = rules.STOP_ATR * entry_atr
            if highest >= entry + self.breakeven_r * risk:
                stop = max(stop, entry * (1 + ROUND_TRIP_FEE))
        trade.set_custom_data("stop_ratchet", float(stop))
        return stoploss_from_absolute(stop, current_rate=current_rate, is_short=False, leverage=trade.leverage)


class SafeTrendBaselineCopy(_ExitVariant):
    """Same numbers as live, through the variant code path: must reproduce SafeTrendLive exactly."""


class SafeTrendTrail25(_ExitVariant):
    trail_atr = 2.5


class SafeTrendBreakeven(_ExitVariant):
    breakeven_r = 1.0


class SafeTrendPartial(_ExitVariant):
    position_adjustment_enable = True
    partial_r = 1.5

    def adjust_trade_position(self, trade, current_time: datetime, current_rate: float, current_profit: float,
                              min_stake: float | None, max_stake: float, current_entry_rate: float,
                              current_exit_rate: float, current_entry_profit: float, current_exit_profit: float,
                              **kwargs):
        if not rules.is_strategy_tag(trade.enter_tag) or trade.get_custom_data("partial_done"):
            return None
        entry_atr, entry = trade.get_custom_data("entry_atr"), trade.get_custom_data("entry_open")
        if entry_atr is None or entry is None:
            return None
        if current_rate >= float(entry) + self.partial_r * rules.STOP_ATR * float(entry_atr):
            trade.set_custom_data("partial_done", True)
            return -(trade.stake_amount / 2), "partial_tp"
        return None
