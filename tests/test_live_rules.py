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
