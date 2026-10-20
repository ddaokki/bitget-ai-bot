"""Plan 2 live-capable safe strategy (spec §7.1, §7.1b, §7.4): BTC long only.

Ported from the research baseline SafeTrendV0 (branch spike/90d-backtest):
4h Donchian-20 breakout in the daily EMA50 > EMA200 trend, ADX > 20, no entry in the
top 10% of ATR%, initial stop 2xATR(4h), 3xATR chandelier on 1h highs, exit on a 4h
close below the prior Donchian-10 low.

Differences from the research code:
- 4h and 1d candles come from the exchange (@informative), because Bitget serves only
  ~990 startup 1h candles, too few for a daily EMA200. Bitget 4h/1d candles are UTC
  aligned, matching the research resampling.
- Long only and 0.5% risk per trade (§7.1b). Entry caps from live_rules (§7.4).
- Manual and smoke entries (tag not starting with "safe_") keep the stake they were
  given and a fixed stop 3% below the open, and are never closed by the exit signal.
- §7.1 blackout reads upcoming release times from user_data/events/upcoming_us_macro.csv.

There is no proven edge: the research found none for the model filters. This rule set
was chosen on the same 2023-10..2026-10 data. See docs/runbooks/live.md.
"""
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import talib.abstract as ta
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute
from pandas import DataFrame

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_rules as rules  # noqa: E402

logger = logging.getLogger(__name__)
ENTRY_TAG = "safe_long"
EXIT_TAG = "opposite_donchian_10"
BLACKOUT_CATEGORIES = {"FOMC", "CPI", "PPI", "NFP"}


class SafeTrendLive(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    # Bitget serves 200 candles per call and Freqtrade allows 5 calls, per timeframe.
    startup_candle_count = 990
    minimal_roi = {"0": 100}
    stoploss = -0.50              # net only; custom_stoploss sets the real stop
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    process_only_new_candles = True
    position_adjustment_enable = False

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 4},
            {"method": "StoplossGuard", "lookback_period_candles": 24, "trade_limit": 2,
             "stop_duration_candles": 24, "only_per_pair": False},
            {"method": "MaxDrawdown", "lookback_period_candles": 720, "trade_limit": 3,
             "stop_duration_candles": 168, "max_allowed_drawdown": 0.10},
        ]

    def bot_start(self, **kwargs) -> None:
        self._releases = self._load_releases()
        self._calendar_warned = None

    def _load_releases(self) -> list[datetime]:
        path = Path(self.config.get("user_data_dir", "/freqtrade/user_data")) / "events" / "upcoming_us_macro.csv"
        try:
            frame = pd.read_csv(path)
        except (OSError, ValueError, pd.errors.EmptyDataError):
            logger.warning("No upcoming macro calendar at %s: the §7.1 blackout is off.", path)
            return []
        frame = frame.loc[frame.category.astype(str).str.upper().isin(BLACKOUT_CATEGORIES)]
        times = pd.to_datetime(frame.ts_utc, utc=True, errors="coerce").dropna()
        if times.empty:
            logger.warning("Upcoming macro calendar %s has no rows: the §7.1 blackout is off.", path)
        return [t.to_pydatetime() for t in times.sort_values()]

    # ---------------------------------------------------------------- indicators
    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        # Channels exclude the current 4h bar: a breakout is a close beyond the prior 20 bars.
        dataframe["dc_high"] = dataframe.high.rolling(20).max().shift(1)
        dataframe["dc_low"] = dataframe.low.rolling(20).min().shift(1)
        dataframe["exit_low"] = dataframe.low.rolling(10).min().shift(1)
        dataframe["exit_high"] = dataframe.high.rolling(10).max().shift(1)
        dataframe["atr_pct"] = dataframe.atr / dataframe.close
        dataframe["atr_q90"] = dataframe.atr_pct.rolling(180, min_periods=180).quantile(0.9).shift(1)
        return dataframe

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # @informative shifts each 4h/1d bar onto the last 1h candle inside it, so a 4h value
        # first appears on the 1h row whose close completes that 4h bar.
        dataframe["four_close"] = dataframe.date == dataframe.date_4h + pd.Timedelta(hours=3)
        return dataframe

    def _blackout_mask(self, dates: pd.Series) -> np.ndarray:
        releases = getattr(self, "_releases", [])
        if not releases:
            return np.zeros(len(dates), dtype=bool)
        entry = (pd.to_datetime(dates, utc=True) + pd.Timedelta(hours=1)).to_numpy(dtype="datetime64[ns]").astype("int64")
        stamps = np.array([pd.Timestamp(r).value for r in releases], dtype="int64")
        span = int(2 * 3_600_000_000_000)
        lo = np.searchsorted(stamps, entry - span, side="left")
        hi = np.searchsorted(stamps, entry + span, side="right")
        return hi > lo

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = None
        long = (
            dataframe.four_close
            & (dataframe.adx_4h > 20)
            & (dataframe.atr_pct_4h <= dataframe.atr_q90_4h)
            & (dataframe.volume > 0)
            & (dataframe.ema50_1d > dataframe.ema200_1d)
            & (dataframe.close_4h > dataframe.dc_high_4h)
        )
        long &= ~self._blackout_mask(dataframe.date)
        dataframe.loc[long, ["enter_long", "enter_tag"]] = (1, ENTRY_TAG)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe.loc[dataframe.four_close & (dataframe.close_4h < dataframe.exit_low_4h), ["exit_long", "exit_tag"]] = (1, EXIT_TAG)
        return dataframe

    # ---------------------------------------------------------------- per trade
    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float,
                 max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return min(rules.LEVERAGE, max_leverage)

    def _history(self, pair: str, before: datetime) -> DataFrame:
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        # Only closed candles: the current candle's high/low are not known yet.
        return frame.loc[frame.date < pd.Timestamp(before)]

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        if not rules.is_strategy_tag(entry_tag):
            return proposed_stake          # manual / smoke entry: keep the stake the user gave
        history = self._history(pair, current_time)
        if history.empty:
            return 0.0
        market = self.dp.market(pair) if self.dp else None
        limits = (market or {}).get("limits") or {}
        minimum = rules.exchange_min_margin((limits.get("amount") or {}).get("min"), (limits.get("cost") or {}).get("min"),
                                            float((market or {}).get("contractSize") or 1.0), current_rate, leverage)
        return rules.risk_stake(self.wallets.get_total_stake_amount(), current_rate, float(history.iloc[-1].atr_4h),
                                leverage, minimum, max_stake)

    def _realized_since(self, start: datetime) -> float:
        from freqtrade.persistence import Trade
        return float(sum(float(t.close_profit_abs or 0) for t in Trade.get_trades_proxy(is_open=False, close_date=start)))

    def _strategy_entries_since(self, start: datetime) -> int:
        from freqtrade.persistence import Trade
        return sum(1 for t in Trade.get_trades_proxy(open_date=start) if rules.is_strategy_tag(t.enter_tag))

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str,
                            current_time: datetime, entry_tag: str | None, side: str, **kwargs) -> bool:
        if not rules.is_strategy_tag(entry_tag):
            return True
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        ok, reason = rules.entry_allowed(self._strategy_entries_since(rules.day_start(now)),
                                         self._realized_since(rules.day_start(now)),
                                         self._realized_since(rules.week_start(now)),
                                         self.wallets.get_total_stake_amount())
        if not ok:
            logger.info("Entry blocked for %s: %s", pair, reason)
        return ok

    def _entry_atr(self, trade, history: DataFrame) -> float | None:
        value = trade.get_custom_data("entry_atr")
        if value is None:
            before = history.loc[history.date < pd.Timestamp(trade.open_date_utc)]
            if before.empty:
                return None
            value = float(before.iloc[-1].atr_4h)
            if not np.isfinite(value) or value <= 0:
                return None
            trade.set_custom_data("entry_atr", value)
        return float(value)

    def custom_stoploss(self, pair: str, trade, current_time: datetime, current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> float | None:
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
        since = history.loc[history.date >= pd.Timestamp(trade.open_date_utc)]
        highest = float(since.high.max()) if not since.empty else None
        previous = trade.get_custom_data("stop_ratchet")
        stop = rules.chandelier_stop(float(entry), entry_atr, highest, float(history.iloc[-1].atr_4h),
                                     None if previous is None else float(previous))
        trade.set_custom_data("stop_ratchet", float(stop))
        return stoploss_from_absolute(stop, current_rate=current_rate, is_short=False, leverage=trade.leverage)

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float, rate: float, time_in_force: str,
                           exit_reason: str, current_time: datetime, **kwargs) -> bool:
        # The strategy's exit signal never closes a manual or smoke position.
        if exit_reason == EXIT_TAG and not rules.is_strategy_tag(trade.enter_tag):
            return False
        return True

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        # Warn once a day when the upcoming-release calendar is about to run out.
        releases = getattr(self, "_releases", [])
        day = current_time.date()
        if self.config.get("runmode") and str(getattr(self.config["runmode"], "value", self.config["runmode"])) in {"live", "dry_run"}:
            if getattr(self, "_calendar_warned", None) != day and (not releases or max(releases) < current_time + timedelta(days=30)):
                logger.warning("Upcoming macro calendar ends within 30 days: update user_data/events/upcoming_us_macro.csv.")
                self._calendar_warned = day
