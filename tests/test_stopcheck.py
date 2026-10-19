from dataclasses import replace

import pytest

from bitgetbot.models import Check, PlanOrder, PositionSnapshot
from bitgetbot.stopcheck import evaluate_protection, overall


LONG = PositionSnapshot("BTCUSDT", "long", 0.0001, 111240.5, 112050.0, 56300.0, 2, "isolated")
STOP_OK = PlanOrder("BTCUSDT", "pos_loss", 108900.0, "mark_price", None, "1")


def by_name(checks):
    return {check.name: check for check in checks}


def test_long_with_mark_pos_loss_inside_liq_passes():
    checks = evaluate_protection(LONG, [STOP_OK])
    assert overall(checks) == "PASS"
    assert len(checks) == 6
    assert set(by_name(checks)) == {
        "margin_isolated", "stop_exists", "stop_trigger_mark",
        "stop_side", "stop_inside_liq", "stop_covers_size",
    }


def test_missing_stop_fails():
    checks = by_name(evaluate_protection(LONG, []))
    assert checks["stop_exists"].ok is False
    assert all(checks[name].ok is None for name in (
        "stop_trigger_mark", "stop_side", "stop_inside_liq", "stop_covers_size",
    ))
    assert overall(list(checks.values())) == "FAIL"


def test_fill_price_trigger_fails():
    plan = replace(STOP_OK, trigger_type="fill_price")
    assert by_name(evaluate_protection(LONG, [plan]))["stop_trigger_mark"].ok is False


def test_stop_beyond_70pct_of_liq_distance_fails():
    plan = replace(STOP_OK, trigger_price=70000.0)
    assert by_name(evaluate_protection(LONG, [plan]))["stop_inside_liq"].ok is False


def test_short_stop_must_be_above_entry_and_inside_liq():
    short = PositionSnapshot("ETHUSDT", "short", 0.01, 3000.0, 2990.0, 3140.0, 20, "isolated")
    ok = PlanOrder("ETHUSDT", "pos_loss", 3090.0, "mark_price", None, "2")
    bad = replace(ok, trigger_price=3110.0, order_id="3")
    wrong_side = replace(ok, trigger_price=2900.0, order_id="4")
    assert overall(evaluate_protection(short, [ok])) == "PASS"
    assert by_name(evaluate_protection(short, [bad]))["stop_inside_liq"].ok is False
    assert by_name(evaluate_protection(short, [wrong_side]))["stop_side"].ok is False


def test_unknown_liq_gives_unknown_not_crash():
    pos = replace(LONG, liq_price=None)
    checks = evaluate_protection(pos, [STOP_OK])
    assert by_name(checks)["stop_inside_liq"].ok is None
    assert overall(checks) == "UNKNOWN"


def test_cross_margin_fails():
    pos = replace(LONG, margin_mode="crossed")
    assert by_name(evaluate_protection(pos, [STOP_OK]))["margin_isolated"].ok is False


def test_partial_loss_plan_does_not_cover_size():
    plan = replace(STOP_OK, plan_type="loss_plan", size=0.00005, order_id="5")
    assert by_name(evaluate_protection(LONG, [plan]))["stop_covers_size"].ok is False


def test_plans_for_other_symbols_are_ignored():
    other = replace(STOP_OK, symbol="ETHUSDT", order_id="6")
    assert by_name(evaluate_protection(LONG, [other]))["stop_exists"].ok is False


def test_non_stop_plans_are_ignored():
    plan = replace(STOP_OK, plan_type="pos_profit")
    assert by_name(evaluate_protection(LONG, [plan]))["stop_exists"].ok is False


@pytest.mark.parametrize("reverse", [False, True])
def test_nearest_entry_stop_is_selected_even_when_it_fails(reverse):
    nearest = replace(STOP_OK, trigger_price=110000.0, trigger_type="fill_price", order_id="7")
    plans = [STOP_OK, nearest]
    if reverse:
        plans.reverse()
    checks = by_name(evaluate_protection(LONG, plans))
    assert checks["stop_trigger_mark"].ok is False
    assert overall(list(checks.values())) == "FAIL"


@pytest.mark.parametrize("side,entry,mark,stop,liq", [
    ("long", 100.0, 120.0, 110.0, 50.0),
    ("short", 100.0, 80.0, 90.0, 150.0),
])
def test_trailing_stop_beyond_entry_uses_current_mark(side, entry, mark, stop, liq):
    pos = replace(LONG, side=side, entry_price=entry, mark_price=mark, liq_price=liq)
    assert overall(evaluate_protection(pos, [replace(STOP_OK, trigger_price=stop)])) == "PASS"


@pytest.mark.parametrize("side,stop,expected", [
    ("long", 90.0, True), ("long", 110.0, False),
    ("short", 110.0, True), ("short", 90.0, False),
])
def test_missing_mark_falls_back_to_entry(side, stop, expected):
    pos = replace(LONG, side=side, entry_price=100.0, mark_price=None)
    checks = by_name(evaluate_protection(pos, [replace(STOP_OK, trigger_price=stop)]))
    assert checks["stop_side"].ok is expected


@pytest.mark.parametrize("side", ["long", "short"])
def test_stop_equal_to_mark_is_wrong_side(side):
    pos = replace(LONG, side=side)
    plan = replace(STOP_OK, trigger_price=112050.0)
    assert by_name(evaluate_protection(pos, [plan]))["stop_side"].ok is False


def test_liquidation_distance_boundary_is_inclusive_and_configurable():
    pos = replace(LONG, entry_price=100.0, mark_price=100.0, liq_price=0.0)
    plan = replace(STOP_OK, trigger_price=30.0)
    assert overall(evaluate_protection(pos, [plan])) == "PASS"
    checks = by_name(evaluate_protection(pos, [plan], max_liq_fraction=0.6))
    assert checks["stop_inside_liq"].ok is False


@pytest.mark.parametrize("plan_type,size,expected", [
    ("pos_loss", 0.0, True),
    ("loss_plan", None, False),
    ("loss_plan", 0.0001, True),
    ("loss_plan", 0.0002, True),
])
def test_stop_coverage_depends_on_plan_type_and_size(plan_type, size, expected):
    plan = replace(STOP_OK, plan_type=plan_type, size=size)
    assert by_name(evaluate_protection(LONG, [plan]))["stop_covers_size"].ok is expected


@pytest.mark.parametrize("values,expected", [
    ([True, True], "PASS"),
    ([True, None], "UNKNOWN"),
    ([None, False], "FAIL"),
    ([False, None], "FAIL"),
    ([], "PASS"),
])
def test_overall_prioritizes_failure_over_unknown(values, expected):
    checks = [Check(str(index), value, "검사") for index, value in enumerate(values)]
    assert overall(checks) == expected
