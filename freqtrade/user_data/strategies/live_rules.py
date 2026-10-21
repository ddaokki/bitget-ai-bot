"""Pure rules for SafeTrendLive: sizing, stops, entry caps, blackout (spec §7.1, §7.1b, §7.4).

No Freqtrade import, so tests run in the repo venv. All money values are in USDT.
"""
from __future__ import annotations

import json
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
MANUAL_TARGETS_FILE = "manual_targets.json"  # user_data/<file>, written by the control panel
USER_TP_EXIT = "user_take_profit"            # exit reason when a user take-profit is touched


def _finite_positive(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
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


# ------------------------------------------------------------------ daily candles
# Bitget USDT-M returns at most 90 daily candles per request, but Freqtrade's Bitget
# module assumes 1000 for the last 4 years and asks once, so live/dry-run would see only
# ~89 days, too few for EMA200. The strategy pages through 90-day windows itself.
DAY_MS = 86_400_000
DAILY_HISTORY_DAYS = 600
DAILY_WINDOW_DAYS = 90
DAILY_MIN_ROWS = 400


def daily_since_list(now_ms: int, days: int = DAILY_HISTORY_DAYS, window: int = DAILY_WINDOW_DAYS) -> list[int]:
    """UTC-midnight start times of consecutive `window`-day requests covering the last `days` days, oldest first."""
    today = now_ms - now_ms % DAY_MS
    start = today - days * DAY_MS
    return list(range(start, today + 1, window * DAY_MS))


def completed_daily_rows(batches: list, now_ms: int) -> list[list]:
    """Merge ccxt OHLCV batches: one row per UTC day, sorted, only days that have closed."""
    rows = {}
    for batch in batches:
        for row in batch or []:
            if len(row) >= 6 and int(row[0]) % DAY_MS == 0 and int(row[0]) + DAY_MS <= now_ms:
                rows[int(row[0])] = list(row[:6])
    return [rows[key] for key in sorted(rows)]


def daily_rows_usable(rows: list[list]) -> bool:
    """Enough contiguous closed days for a daily EMA200."""
    if len(rows) < DAILY_MIN_ROWS:
        return False
    return all(int(b[0]) - int(a[0]) == DAY_MS for a, b in zip(rows, rows[1:]))


def parse_manual_targets(text: str) -> dict[int, dict]:
    """{trade id: {"price", "open_ts", "dry_run"}} from the panel's file. Anything malformed is dropped.

    A target names the exact trade it was set on (open time in ms and mode), because the dry and
    live databases both number trades from 1 and a target must never move to another trade.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    targets = data.get("take_profit") if isinstance(data, dict) else None
    if not isinstance(targets, dict):
        return {}
    result = {}
    for key, entry in targets.items():
        try:
            trade_id = int(key)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isinstance(entry, dict):
            continue
        price, open_ts, dry_run = entry.get("price"), entry.get("open_ts"), entry.get("dry_run")
        if type(price) in (int, float) and _finite_positive(price) and type(open_ts) is int and open_ts > 0 \
                and type(dry_run) is bool:
            result[trade_id] = {"price": float(price), "open_ts": open_ts, "dry_run": dry_run}
    return result


def target_for(targets: dict[int, dict], trade_id: int, open_ts: int, dry_run: bool) -> float | None:
    """The take-profit price for this exact trade (same id, open time and mode), else None."""
    entry = targets.get(trade_id)
    if entry is None or entry["open_ts"] != open_ts or entry["dry_run"] is not dry_run:
        return None
    return entry["price"]


def take_profit_hit(is_short: bool, rate: float, target: float | None) -> bool:
    """True once the price touches the user's take-profit in the trade's favour."""
    if target is None or not _finite_positive(rate):
        return False
    return rate <= target if is_short else rate >= target
