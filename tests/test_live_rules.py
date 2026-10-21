"""Pure live-strategy rules (no Freqtrade import), loaded from the strategy folder by path."""
import importlib.util
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "freqtrade" / "user_data" / "strategies" / "live_rules.py"
_SPEC = importlib.util.spec_from_file_location("live_rules", _PATH)
live_rules = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live_rules)


def test_risk_stake_targets_half_percent_of_wallet_at_the_initial_stop():
    stake = live_rules.risk_stake(114, 80000, 1000, 2, 5, 100)
    assert stake == pytest.approx(114 * 0.005 / ((2 * 1000 / 80000) * 2))


def test_risk_stake_skips_when_minimum_exceeds_one_and_a_half_times_target():
    target = 114 * 0.005 / ((2 * 1000 / 80000) * 2)
    assert live_rules.risk_stake(114, 80000, 1000, 2, target * 1.51, 100) == 0.0


def test_risk_stake_rounds_up_to_minimum_within_tolerance():
    target = 114 * 0.005 / ((2 * 1000 / 80000) * 2)
    assert live_rules.risk_stake(114, 80000, 1000, 2, target * 1.4, 100) == pytest.approx(target * 1.4)


@pytest.mark.parametrize("atr", [float("nan"), 0.0, -1.0])
def test_risk_stake_skips_without_a_valid_atr(atr):
    assert live_rules.risk_stake(114, 80000, atr, 2, 5, 100) == 0.0


def test_risk_stake_skips_when_above_maximum():
    assert live_rules.risk_stake(114, 80000, 1000, 2, 5, 1.0) == 0.0


def test_exchange_min_margin_uses_the_larger_of_amount_and_cost():
    assert live_rules.exchange_min_margin(0.0001, 5, 1.0, 80000, 2) == pytest.approx(8 / 2)
    assert live_rules.exchange_min_margin(0.0001, 10, 1.0, 80000, 2) == pytest.approx(10 / 2)
    assert live_rules.exchange_min_margin(None, None, 1.0, 80000, 2) == 0.0


def test_chandelier_starts_at_two_atr_below_entry():
    assert live_rules.chandelier_stop(100.0, 2.0, None, 2.0, None) == pytest.approx(96.0)


def test_chandelier_trails_three_atr_below_the_highest_high():
    assert live_rules.chandelier_stop(100.0, 2.0, 120.0, 2.0, None) == pytest.approx(114.0)


def test_chandelier_never_moves_down():
    assert live_rules.chandelier_stop(100.0, 2.0, 101.0, 2.0, 99.0) == pytest.approx(99.0)


@pytest.mark.parametrize(
    "entries, today, week, allowed",
    [(3, 0.0, 0.0, False), (2, -2.99, 0.0, True), (0, -3.0, 0.0, False), (0, 0.0, -6.0, False), (0, 5.0, -5.99, True)],
)
def test_entry_caps(entries, today, week, allowed):
    ok, reason = live_rules.entry_allowed(entries, today, week, 100.0)
    assert ok is allowed
    assert (reason == "") is allowed


@pytest.mark.parametrize("tag, expected", [("safe_long", True), ("smoke", False), ("force_entry", False), (None, False), ("", False)])
def test_strategy_tag(tag, expected):
    assert live_rules.is_strategy_tag(tag) is expected


def test_blackout_window_is_two_hours_inclusive():
    release = datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc)
    assert live_rules.blackout(release - timedelta(hours=2), [release])
    assert live_rules.blackout(release + timedelta(hours=2), [release])
    assert not live_rules.blackout(release + timedelta(hours=2, minutes=1), [release])
    assert not live_rules.blackout(release, [])


def test_manual_stop_is_three_percent_below_open():
    assert live_rules.MANUAL_STOP_PRICE_PCT == pytest.approx(0.03)
    assert not math.isnan(live_rules.MANUAL_STOP_PRICE_PCT)


DAY = 86_400_000


def test_daily_windows_cover_history_in_90_day_steps_ending_today():
    now = 1_760_000_000_000 + 5 * 3_600_000          # some time of day
    sinces = live_rules.daily_since_list(now)
    today = now - now % DAY
    assert sinces[0] == today - 600 * DAY
    assert all(b - a == 90 * DAY for a, b in zip(sinces, sinces[1:]))
    assert sinces[-1] <= today < sinces[-1] + 90 * DAY


def test_completed_daily_rows_dedupes_sorts_and_drops_open_day():
    now = 10 * DAY + 3_600_000
    batches = [[[2 * DAY, 1, 2, 0.5, 1.5, 10], [DAY, 1, 2, 0.5, 1.5, 10]], [[2 * DAY, 9, 9, 9, 9, 9], [10 * DAY, 1, 1, 1, 1, 1]], None]
    rows = live_rules.completed_daily_rows(batches, now)
    assert [r[0] for r in rows] == [DAY, 2 * DAY]          # today's open candle (10*DAY) dropped
    assert rows[1][1] == 9                                 # later batch wins for a duplicate day


def test_daily_rows_usable_needs_400_contiguous_days():
    rows = [[i * DAY, 1, 1, 1, 1, 1] for i in range(400)]
    assert live_rules.daily_rows_usable(rows)
    assert not live_rules.daily_rows_usable(rows[:399])
    assert not live_rules.daily_rows_usable(rows[:200] + rows[201:] + [[400 * DAY, 1, 1, 1, 1, 1]])


def test_manual_targets_bind_each_price_to_one_trade():
    text = ('{"take_profit": {"2": {"price": 86000, "open_ts": 1792584003000, "dry_run": false},'
            ' "3": {"price": 85000.5, "open_ts": 1, "dry_run": true},'
            ' "4": {"price": -1, "open_ts": 1, "dry_run": true}, "5": {"price": 1, "open_ts": "1", "dry_run": true},'
            ' "6": {"price": 1, "open_ts": 1, "dry_run": 0}, "7": 86000, "x": {"price": 1, "open_ts": 1, "dry_run": true},'
            ' "8": {"price": 1e999, "open_ts": 1, "dry_run": true},'
            ' "9": {"price": 100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000, "open_ts": 1, "dry_run": true}}}')
    targets = live_rules.parse_manual_targets(text)
    assert targets == {2: {"price": 86000.0, "open_ts": 1792584003000, "dry_run": False},
                       3: {"price": 85000.5, "open_ts": 1, "dry_run": True}}
    assert live_rules.target_for(targets, 2, 1792584003000, False) == 86000.0
    # Same id in the other mode's database, or a later trade reusing the id: no target.
    assert live_rules.target_for(targets, 2, 1792584003000, True) is None
    assert live_rules.target_for(targets, 2, 1792584003001, False) is None
    assert live_rules.target_for(targets, 99, 1, False) is None


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"take_profit": []}', '{"other": {}}'])
def test_manual_targets_ignore_malformed_files(text):
    assert live_rules.parse_manual_targets(text) == {}


@pytest.mark.parametrize("is_short, rate, target, hit", [
    (False, 86000.0, 86000.0, True), (False, 85999.9, 86000.0, False),
    (True, 80000.0, 80000.0, True), (True, 80000.1, 80000.0, False),
    (False, 90000.0, None, False), (False, float("nan"), 86000.0, False),
])
def test_take_profit_hit_on_touch_in_the_trade_direction(is_short, rate, target, hit):
    assert live_rules.take_profit_hit(is_short, rate, target) is hit


def test_user_take_profit_constants():
    assert live_rules.MANUAL_TARGETS_FILE == "manual_targets.json"
    assert live_rules.USER_TP_EXIT == "user_take_profit"
