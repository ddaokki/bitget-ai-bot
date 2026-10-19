"""Evaluate exchange stop-loss protection without exchange access."""

from typing import Literal

from bitgetbot.models import Check, PlanOrder, PositionSnapshot


MAX_LIQ_FRACTION = 0.7


def evaluate_protection(
    pos: PositionSnapshot,
    plans: list[PlanOrder],
    max_liq_fraction: float = MAX_LIQ_FRACTION,
) -> list[Check]:
    checks = [Check(
        "margin_isolated",
        pos.margin_mode == "isolated",
        f"마진 모드: {pos.margin_mode}",
    )]
    candidates = [
        plan for plan in plans
        if plan.symbol == pos.symbol and plan.plan_type in {"pos_loss", "loss_plan"}
    ]
    if not candidates:
        checks.append(Check("stop_exists", False, "같은 심볼의 손절 주문 없음"))
        checks.extend(
            Check(name, None, "손절 주문이 없어 판정 불가")
            for name in (
                "stop_trigger_mark", "stop_side", "stop_inside_liq", "stop_covers_size",
            )
        )
        return checks

    stop = min(candidates, key=lambda plan: abs(pos.entry_price - plan.trigger_price))
    reference_price = pos.mark_price if pos.mark_price is not None else pos.entry_price
    correct_side = (
        stop.trigger_price < reference_price
        if pos.side == "long"
        else stop.trigger_price > reference_price
    )
    checks.extend([
        Check("stop_exists", True, f"손절 주문: {stop.order_id}"),
        Check(
            "stop_trigger_mark",
            stop.trigger_type == "mark_price",
            f"손절 발동 기준: {stop.trigger_type}",
        ),
        Check(
            "stop_side",
            correct_side,
            f"방향: {pos.side}, 손절가: {stop.trigger_price}, 기준가: {reference_price}",
        ),
    ])
    if pos.liq_price is None:
        checks.append(Check("stop_inside_liq", None, "청산가를 알 수 없어 판정 불가"))
    else:
        stop_distance = abs(pos.entry_price - stop.trigger_price)
        max_distance = max_liq_fraction * abs(pos.entry_price - pos.liq_price)
        checks.append(Check(
            "stop_inside_liq",
            stop_distance <= max_distance,
            f"손절 거리: {stop_distance}, 허용 거리: {max_distance}",
        ))

    covers_size = stop.plan_type == "pos_loss" or (
        stop.size is not None and stop.size >= pos.size
    )
    checks.append(Check(
        "stop_covers_size",
        covers_size,
        "포지션 전체 손절" if stop.plan_type == "pos_loss"
        else f"손절 수량: {stop.size}, 포지션 수량: {pos.size}",
    ))
    return checks


def overall(checks: list[Check]) -> Literal["PASS", "FAIL", "UNKNOWN"]:
    if any(check.ok is False for check in checks):
        return "FAIL"
    if any(check.ok is None for check in checks):
        return "UNKNOWN"
    return "PASS"
