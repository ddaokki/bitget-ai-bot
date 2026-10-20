"""Throwaway historical experiment (spec §7.1 safe mode), not a deployable/live strategy.

Only completed 4h/daily bars contribute indicators. Orders execute on the
following 1h open. Companion rails, LLM gate and event blackouts are absent.

Warmup: Bitget only allows ~999 1h startup candles, far fewer than the ~4800
needed for a daily EMA200. Backtests therefore load data from 2022-01-01 and
suppress entries before config["spike"]["entry_start"].

Phase B (review fixes): the 1.5x minimum-size rule uses the REAL exchange
minimum from market limits (fix 4); stops/take-profits are anchored to the
first fill so a later scale-in cannot move them; an optional breakeven move
(spec §7.5 code rule 1) and spec §7.1 event blackout are off unless a subclass
enables them.
"""
import logging
from datetime import datetime

import numpy as np
import pandas as pd
import talib.abstract as ta
from freqtrade.strategy import IStrategy, merge_informative_pair, stoploss_from_absolute
from pandas import DataFrame

# Per-entry parameters. The baseline only uses "base"; tiered subclasses add more.
#   risk: fraction of wallet lost at the initial stop
#   leverage: isolated leverage
#   stop_atr: initial stop distance in 4h-ATR units
#   trail_atr: chandelier distance in ATR units (None = no trailing)
#   tp_atr: fixed take-profit distance in ATR units (None = none)
#   donchian_exit: honour the opposite Donchian-10 exit signal
PROFILES = {
    "base": {"risk": 0.01, "leverage": 2.0, "stop_atr": 2.0, "trail_atr": 3.0, "tp_atr": None, "donchian_exit": True},
}
MIN_SIZE_TOLERANCE = 1.5
EVENTS_PATH = "/freqtrade/user_data/events/events.csv"
logger = logging.getLogger(__name__)


def parse_tag(tag: str | None) -> dict:
    """'safe_long|tier=mid|p=0.583' -> {'name': 'safe_long', 'tier': 'mid', 'p': '0.583'}."""
    parts = (tag or "").split("|")
    out = {"name": parts[0]}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            out[key] = value
    return out


class SafeTrendV0(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = True
    # Bitget serves 200 historic 1h candles per call and Freqtrade allows 5 calls.
    startup_candle_count = 990
    minimal_roi = {}
    stoploss = -0.50
    use_custom_stoploss = True
    use_custom_roi = True
    trailing_stop = False
    use_exit_signal = True
    process_only_new_candles = True
    position_adjustment_enable = False
    profiles = PROFILES
    # §7.5 code rule 1: once the trade is 1xATR in profit, lift the stop to breakeven (entry + round-trip fee).
    breakeven_atr: float | None = None
    # §7.1 blackout: no new entry within +-2h of FOMC/CPI/PPI/NFP releases.
    use_blackout = False

    def bot_start(self, **kwargs):
        mode = str(getattr(self.config.get("runmode"), "value", self.config.get("runmode", "")))
        if mode in {"live", "dry_run"}:
            raise RuntimeError(f"{type(self).__name__} is a historical-only research strategy; live/dry-run execution forbidden")

    # ---------- indicators ----------
    @staticmethod
    def closed_resample(dataframe: DataFrame, hours: int) -> DataFrame:
        frame = dataframe[["date", "open", "high", "low", "close", "volume"]].copy()
        indexed = frame.set_index("date")
        result = indexed.resample(f"{hours}h", origin="epoch", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        counts = indexed["close"].resample(f"{hours}h", origin="epoch", label="left", closed="left").count()
        return result.loc[counts == hours].dropna().reset_index()

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        four = self.closed_resample(dataframe, 4)
        daily = self.closed_resample(dataframe, 24)
        four["atr"] = ta.ATR(four, timeperiod=14)
        four["adx"] = ta.ADX(four, timeperiod=14)
        four["rsi"] = ta.RSI(four, timeperiod=14)
        # Channels exclude the current 4h bar: a breakout is the close beyond the prior 20 bars.
        four["dc_high"] = four.high.rolling(20).max().shift(1)
        four["dc_low"] = four.low.rolling(20).min().shift(1)
        four["exit_low"] = four.low.rolling(10).min().shift(1)
        four["exit_high"] = four.high.rolling(10).max().shift(1)
        four["atr_pct"] = four.atr / four.close
        four["atr_q90"] = four.atr_pct.rolling(180, min_periods=180).quantile(0.9).shift(1)
        four["atr_rank"] = four.atr_pct.rolling(180, min_periods=180).rank(pct=True)
        daily["ema50"] = ta.EMA(daily, timeperiod=50)
        daily["ema200"] = ta.EMA(daily, timeperiod=200)
        # merge_informative_pair shifts each bar to the last 1h candle inside it,
        # so a 4h/1d value first appears on the 1h row whose close completes it.
        dataframe = merge_informative_pair(dataframe, four, "1h", "4h", ffill=True)
        dataframe = merge_informative_pair(dataframe, daily, "1h", "1d", ffill=True)
        dataframe["four_close"] = dataframe.date == dataframe.date_4h + pd.Timedelta(hours=3)
        return dataframe

    def entry_start(self) -> pd.Timestamp | None:
        value = (self.config.get("spike") or {}).get("entry_start")
        return pd.Timestamp(value) if value else None

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = None
        common = dataframe.four_close & (dataframe.adx_4h > 20) & (dataframe.atr_pct_4h <= dataframe.atr_q90_4h) & (dataframe.volume > 0)
        start = self.entry_start()
        if start is not None:
            common &= dataframe.date >= start
        long = common & (dataframe.ema50_1d > dataframe.ema200_1d) & (dataframe.close_4h > dataframe.dc_high_4h)
        short = common & (dataframe.ema50_1d < dataframe.ema200_1d) & (dataframe.close_4h < dataframe.dc_low_4h)
        if self.use_blackout:
            from event_impact import blackout_mask, load_calendar
            path = (self.config.get("event_features") or {}).get("path", EVENTS_PATH)
            blocked = blackout_mask(dataframe.date + pd.Timedelta(hours=1), load_calendar(path))
            hit = int(((long | short) & blocked).sum())
            if hit:
                logger.info("blackout blocked %d %s entry signals", hit, metadata.get("pair"))
            long &= ~blocked
            short &= ~blocked
        dataframe.loc[long, ["enter_long", "enter_tag"]] = (1, "safe_long")
        dataframe.loc[short, ["enter_short", "enter_tag"]] = (1, "safe_short")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe.loc[dataframe.four_close & (dataframe.close_4h < dataframe.exit_low_4h), ["exit_long", "exit_tag"]] = (1, "opposite_donchian_10")
        dataframe.loc[dataframe.four_close & (dataframe.close_4h > dataframe.exit_high_4h), ["exit_short", "exit_tag"]] = (1, "opposite_donchian_10")
        return dataframe

    # ---------- per-trade profile ----------
    def profile(self, tag: str | None) -> dict:
        return self.profiles.get(parse_tag(tag).get("tier", "base"), self.profiles["base"])

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return min(self.profile(entry_tag)["leverage"], max_leverage)

    @staticmethod
    def exchange_min_margin(market: dict | None, rate: float, leverage: float) -> float:
        """Smallest margin the exchange accepts (no Freqtrade reserve): max(min amount x price, min cost) / leverage."""
        if not market or rate <= 0 or leverage <= 0:
            return 0.0
        limits = market.get("limits") or {}
        contract = float(market.get("contractSize") or 1.0)
        min_amount = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        notional = max(float(min_amount or 0) * contract * rate, float(min_cost or 0))
        return notional / leverage

    @staticmethod
    def risk_stake(wallet: float, rate: float, atr: float, leverage: float, minimum: float | None, maximum: float, risk: float = 0.01, stop_atr: float = 2.0) -> float:
        """Margin such that the loss at the initial stop equals `risk` of the wallet.

        `minimum` is the REAL exchange minimum margin (exchange_min_margin, no
        Freqtrade reserve). Rounding up to it is allowed only up to 1.5x the
        target risk (spec §7.1); beyond that the trade is skipped. Freqtrade may
        still lift a stake below its own reserve-padded minimum by up to ~10%.
        """
        if not np.isfinite(atr) or atr <= 0 or rate <= 0 or leverage <= 0:
            return 0.0
        target = wallet * risk / ((stop_atr * atr / rate) * leverage)
        required = max(target, minimum or 0)
        if required > target * MIN_SIZE_TOLERANCE or required > maximum:
            return 0.0
        return required

    def _history(self, pair: str, before: datetime) -> DataFrame:
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        # Even when the engine exposes the current candle, its high/low are not yet known.
        return frame.loc[frame.date < pd.Timestamp(before)]

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float, min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        history = self._history(pair, current_time)
        if history.empty:
            return 0.0
        prof = self.profile(entry_tag)
        market = self.dp.market(pair) if self.dp else None
        minimum = self.exchange_min_margin(market, current_rate, leverage) if market else min_stake
        return self.risk_stake(self.wallets.get_total_stake_amount(), current_rate, float(history.iloc[-1].atr_4h), leverage, minimum, max_stake, prof["risk"], prof["stop_atr"])

    def _entry_atr(self, trade, history: DataFrame) -> float | None:
        entry_atr = trade.get_custom_data("entry_atr")
        if entry_atr is None:
            entry_history = history.loc[history.date < pd.Timestamp(trade.open_date_utc)]
            if entry_history.empty:
                return None
            entry_atr = float(entry_history.iloc[-1].atr_4h)
            if not np.isfinite(entry_atr) or entry_atr <= 0:
                return None
            trade.set_custom_data("entry_atr", entry_atr)
        return float(entry_atr)

    @staticmethod
    def _entry_price(trade) -> float:
        """First-fill price. Stops and targets stay anchored to it after any scale-in."""
        price = trade.get_custom_data("entry_open")
        if price is None:
            price = float(trade.open_rate)
            trade.set_custom_data("entry_open", price)
        return float(price)

    def stop_price(self, trade, history: DataFrame, entry_atr: float) -> float:
        prof = self.profile(trade.enter_tag)
        sign = -1 if trade.is_short else 1
        entry = self._entry_price(trade)
        stop = entry - sign * prof["stop_atr"] * entry_atr
        since_entry = history.loc[history.date >= pd.Timestamp(trade.open_date_utc)]
        better = min if trade.is_short else max
        if prof["trail_atr"] and not since_entry.empty:
            atr = float(history.iloc[-1].atr_4h)
            if trade.is_short:
                stop = min(stop, float(since_entry.low.min()) + prof["trail_atr"] * atr)
            else:
                stop = max(stop, float(since_entry.high.max()) - prof["trail_atr"] * atr)
        if self.breakeven_atr and not since_entry.empty:
            best = float(since_entry.low.min()) if trade.is_short else float(since_entry.high.max())
            if sign * (best - entry) >= self.breakeven_atr * entry_atr:
                fee = float(trade.fee_open or 0) + float(trade.fee_close or trade.fee_open or 0)
                stop = better(stop, float(trade.open_rate) * (1 + sign * fee))
        # Ratchet: the stop never moves against the position (also across scale-in fills).
        previous = trade.get_custom_data("stop_ratchet")
        if previous is not None:
            stop = better(stop, float(previous))
        trade.set_custom_data("stop_ratchet", float(stop))
        return stop

    def custom_stoploss(self, pair: str, trade, current_time: datetime, current_rate: float, current_profit: float, after_fill: bool, **kwargs) -> float | None:
        history = self._history(pair, current_time)
        if history.empty:
            return None
        entry_atr = self._entry_atr(trade, history)
        if entry_atr is None:
            return None
        stop = self.stop_price(trade, history, entry_atr)
        return stoploss_from_absolute(stop, current_rate=current_rate, is_short=trade.is_short, leverage=trade.leverage)

    def custom_roi(self, pair: str, trade, current_time: datetime, trade_duration: int, entry_tag: str | None, side: str, **kwargs) -> float | None:
        prof = self.profile(entry_tag)
        if not prof["tp_atr"]:
            return None
        entry_atr = trade.get_custom_data("entry_atr")
        if entry_atr is None:
            history = self._history(pair, current_time)
            entry_atr = self._entry_atr(trade, history) if not history.empty else None
        if entry_atr is None:
            return None
        sign = -1 if trade.is_short else 1
        target = self._entry_price(trade) + sign * prof["tp_atr"] * float(entry_atr)
        # ROI is a leveraged profit ratio after fees; this maps it back to the target price.
        return float(trade.calc_profit_ratio(target))

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float, rate: float, time_in_force: str, exit_reason: str, current_time: datetime, **kwargs) -> bool:
        if exit_reason == "opposite_donchian_10" and not self.profile(trade.enter_tag)["donchian_exit"]:
            return False
        return True
