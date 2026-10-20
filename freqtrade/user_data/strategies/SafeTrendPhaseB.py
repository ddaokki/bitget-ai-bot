"""Phase B research strategies (historical only; bot_start refuses dry-run/live).

SafeTrendAIV1          AIV0 indicators + 4 impact-weighted event features. The
                       strategy only provides placeholder columns; the FreqAI
                       model SpikeCalibratedLGBM fills them inside every training
                       window with weights measured on that window only (§18).
SafeTrendTieredV1      AIV1 + §7.1a tiers (fixed thresholds) + §7.1 blackout.
SafeTrendTieredV1Plus  TieredV1 + §5 portfolio caps + §7.5 breakeven stop and
                       scale-in (profit only, stop >= breakeven, tier high,
                       max 2 per position, max 4 per day, never averaging down).
SafeTrendTieredV1TP2   EXPLORATORY: TieredV1 with a fixed 2xATR take-profit on
                       every tier. Idea came from phase A results on the same
                       data, so its result cannot be used for promotion.
"""
from datetime import datetime

import pandas as pd
from event_impact import WEIGHTED_FEATURES, blackout_mask, load_calendar
from pandas import DataFrame
from SafeTrendAITiered import THRESHOLDS, TIER_PROFILES, TieredMixin
from SafeTrendAIV0 import SafeTrendAIV0
from SafeTrendV0 import EVENTS_PATH, parse_tag

HIGH_P = dict(THRESHOLDS)["high"]


class SafeTrendAIV1(SafeTrendAIV0):
    def bot_start(self, **kwargs):
        super().bot_start(**kwargs)
        spike = (self.config.get("freqai") or {}).get("spike_model") or {}
        if self.config.get("freqaimodel") != "SpikeCalibratedLGBM" or not spike.get("event_weights"):
            raise RuntimeError(f"{type(self).__name__} needs --freqaimodel SpikeCalibratedLGBM with freqai.spike_model.event_weights=true")

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        dataframe = super().feature_engineering_standard(dataframe, metadata, **kwargs)
        # Placeholders only. SpikeCalibratedLGBM overwrites them per training window.
        for column in WEIGHTED_FEATURES:
            dataframe[f"%-{column}"] = 0.0
        return dataframe


class SafeTrendTieredV1(TieredMixin, SafeTrendAIV1):
    use_blackout = True


class SafeTrendTieredV1Plus(SafeTrendTieredV1):
    position_adjustment_enable = True
    max_entry_position_adjustment = 2      # §7.5: at most 2 adds per position
    breakeven_atr = 1.0                    # §7.5 code rule 1
    max_positions = 4                      # §5
    max_same_side = 3                      # §5
    total_risk_cap = 0.04                  # §5: open loss-at-stop <= 4% of safe funds
    daily_add_cap = 4                      # §7.5
    add_fraction = 0.5                     # [Claude, pre-registered] each add = 50% of the first fill
    high_risk = TIER_PROFILES["high"]["risk"]

    def bot_start(self, **kwargs):
        super().bot_start(**kwargs)
        self._adds_per_day: dict = {}
        self._calendar = load_calendar((self.config.get("event_features") or {}).get("path", EVENTS_PATH))

    @staticmethod
    def _open_trades():
        from freqtrade.persistence import Trade
        return Trade.get_trades_proxy(is_open=True)

    @staticmethod
    def trade_risk(trade) -> float:
        """Loss if the position's current stop is hit (0 when the stop locks in profit)."""
        stop = trade.get_custom_data("stop_ratchet")
        if stop is None:
            stop = trade.stop_loss
        sign = -1 if trade.is_short else 1
        return max(0.0, sign * (float(trade.open_rate) - float(stop)) * float(trade.amount))

    def portfolio_allows(self, open_trades, is_short: bool, tier: str | None, new_risk: float, wallet: float) -> bool:
        n = len(open_trades)
        if n >= self.max_positions:
            return False
        if n >= 2 and tier != "high":
            return False
        if sum(1 for t in open_trades if bool(t.is_short) == is_short) >= self.max_same_side:
            return False
        return sum(self.trade_risk(t) for t in open_trades) + new_risk <= self.total_risk_cap * wallet + 1e-9

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, current_time: datetime, entry_tag: str | None, side: str, **kwargs) -> bool:
        history = self._history(pair, current_time)
        if history.empty:
            return False
        prof = self.profile(entry_tag)
        new_risk = float(amount) * prof["stop_atr"] * float(history.iloc[-1].atr_4h)
        others = [t for t in self._open_trades() if t.pair != pair]
        return self.portfolio_allows(others, side == "short", parse_tag(entry_tag).get("tier"), new_risk, self.wallets.get_total_stake_amount())

    @staticmethod
    def add_amount(position_amount: float, open_rate: float, stop: float, rate: float, is_short: bool, budget: float, cap_amount: float) -> float:
        """Largest add so the WHOLE position loses at most `budget` at `stop`.

        loss_after = add * dist - locked, with locked = profit already secured by the stop.
        """
        sign = -1 if is_short else 1
        dist = sign * (rate - stop)
        if dist <= 0 or budget <= 0:
            return 0.0
        locked = sign * (stop - open_rate) * position_amount
        return max(0.0, min(cap_amount, (budget + locked) / dist))

    def adjust_trade_position(self, trade, current_time: datetime, current_rate: float, current_profit: float, min_stake: float | None, max_stake: float, current_entry_rate: float, current_exit_rate: float, current_entry_profit: float, current_exit_profit: float, **kwargs):
        if trade.has_open_orders or trade.nr_of_successful_entries > self.max_entry_position_adjustment:
            return None
        history = self._history(trade.pair, current_time)
        if history.empty:
            return None
        last = history.iloc[-1]
        # Decide only right after a 4h close, when a fresh p exists.
        if not bool(last.four_close) or last.date + pd.Timedelta(hours=1) != pd.Timestamp(current_time):
            return None
        p = last.get("ai_p")
        if p is None or not (p >= HIGH_P):
            return None
        if (last.ema50_1d < last.ema200_1d) != bool(trade.is_short):
            return None
        if blackout_mask(pd.Series([pd.Timestamp(current_time)]), self._calendar)[0]:
            return None
        sign = -1 if trade.is_short else 1
        if sign * (current_rate - trade.open_rate) <= 0:            # profit only, never average down
            return None
        stop = trade.get_custom_data("stop_ratchet")
        fee = float(trade.fee_open or 0) + float(trade.fee_close or trade.fee_open or 0)
        breakeven = float(trade.open_rate) * (1 + sign * fee)
        if stop is None or sign * (float(stop) - breakeven) < 0:      # stop must be at/above breakeven
            return None
        day = pd.Timestamp(current_time).date()
        if self._adds_per_day.get(day, 0) >= self.daily_add_cap:
            return None
        wallet = self.wallets.get_total_stake_amount()
        others = sum(self.trade_risk(t) for t in self._open_trades() if t.id != trade.id)
        budget = min(self.high_risk * wallet, self.total_risk_cap * wallet - others)
        fills = trade.select_filled_orders(trade.entry_side)
        first_amount = float(fills[0].safe_filled) if fills else float(trade.amount)
        amount = self.add_amount(float(trade.amount), float(trade.open_rate), float(stop), current_rate, trade.is_short, budget, self.add_fraction * first_amount)
        stake = amount * current_rate / float(trade.leverage)
        minimum = self.exchange_min_margin(self.dp.market(trade.pair), current_rate, float(trade.leverage))
        if amount <= 0 or stake < minimum:
            return None
        self._adds_per_day[day] = self._adds_per_day.get(day, 0) + 1
        return min(stake, max_stake), "scale_in"


class SafeTrendTieredV1TP2(SafeTrendTieredV1):
    """EXPLORATORY (already-seen idea): fixed 2xATR take-profit on all tiers."""
    profiles = {name: {**prof, "tp_atr": 2.0} for name, prof in TIER_PROFILES.items()}
