"""Pure rules for SafeTrendLive: sizing, stops, entry caps, blackout (spec §7.1, §7.1b, §7.4).

No Freqtrade import, so tests run in the repo venv. All money values are in USDT.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

RISK = 0.005                 # §7.1b: 0.5% of the wallet lost at the initial stop
LEVERAGE = 2.0
STOP_ATR = 2.0               # initial stop distance in 4h-ATR units
TRAIL_ATR = 3.0              # chandelier distance in 4h-ATR units
MIN_SIZE_TOLERANCE = 1.5     # §7.1: rounding up to the exchange minimum is allowed up to 1.5x target risk
MAX_ENTRIES_PER_DAY = 3      # §7.4: strategy entries per UTC day
DAILY_LOSS_CAP = 0.03        # §7.4: no new entry after -3% realized today (UTC)
WEEKLY_LOSS_CAP = 0.06       # §7.4: no new entry after -6% realized this ISO week
MANUAL_STOP_PRICE_PCT = 0.03  # manual/smoke trades: stop 3% below the open price
STRATEGY_TAG_PREFIX = "safe_"


def _finite_positive(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def exchange_min_margin(min_amount: float | None, min_cost: float | None, contract_size: float, rate: float, leverage: float) -> float:
    """Smallest margin the exchange accepts: max(min amount x price, min cost) / leverage."""
    if rate <= 0 or leverage <= 0:
        return 0.0
    notional = max(float(min_amount or 0) * float(contract_size or 1.0) * rate, float(min_cost or 0))
    return notional / leverage


def risk_stake(wallet: float, rate: float, atr: float, leverage: float, minimum: float, maximum: float,
               risk: float = RISK, stop_atr: float = STOP_ATR) -> float:
    """Margin whose loss at the initial stop is `risk` of the wallet. 0.0 means skip the trade."""
    if not (_finite_positive(atr) and _finite_positive(rate) and _finite_positive(leverage) and _finite_positive(wallet)):
        return 0.0
    target = wallet * risk / ((stop_atr * atr / rate) * leverage)
    required = max(target, float(minimum or 0))
    if required > target * MIN_SIZE_TOLERANCE or required > maximum:
        return 0.0
    return required


def chandelier_stop(entry: float, entry_atr: float, highest_since_entry: float | None, current_atr: float,
                    previous: float | None, stop_atr: float = STOP_ATR, trail_atr: float = TRAIL_ATR) -> float:
    """Long stop: entry - 2xATR, raised to highest high - 3xATR, never below the previous stop."""
    stop = entry - stop_atr * entry_atr
    if highest_since_entry is not None and _finite_positive(current_atr):
        stop = max(stop, highest_since_entry - trail_atr * current_atr)
    if previous is not None and math.isfinite(previous):
        stop = max(stop, previous)
    return stop


def entry_allowed(today_entries: int, today_realized: float, week_realized: float, wallet: float) -> tuple[bool, str]:
    """§7.4 entry caps. Realized values are signed P&L sums (losses negative)."""
    if today_entries >= MAX_ENTRIES_PER_DAY:
        return False, f"daily entry cap {MAX_ENTRIES_PER_DAY} reached"
    if wallet > 0 and today_realized <= -DAILY_LOSS_CAP * wallet:
        return False, f"daily loss cap {DAILY_LOSS_CAP:.0%} reached"
    if wallet > 0 and week_realized <= -WEEKLY_LOSS_CAP * wallet:
        return False, f"weekly loss cap {WEEKLY_LOSS_CAP:.0%} reached"
    return True, ""


def is_strategy_tag(tag: str | None) -> bool:
    """True for entries the strategy made itself; manual/smoke/force entries keep their own stake and stop."""
    return bool(tag) and str(tag).startswith(STRATEGY_TAG_PREFIX)


def blackout(entry_time: datetime, release_times: list[datetime], hours: float = 2.0) -> bool:
    """True when entry_time is within +-hours (inclusive) of any scheduled release."""
    span = timedelta(hours=hours)
    return any(abs(entry_time - release) <= span for release in release_times)


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime) -> datetime:
    return day_start(now) - timedelta(days=now.weekday())
