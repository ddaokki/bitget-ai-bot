"""Korean smoke-test CLI. Orders go through Freqtrade, never through Bitget."""

import argparse
import json
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from bitgetbot.bitget_read import BitgetReader, classify_account, make_exchange, num
from bitgetbot.ftclient import FreqtradeClient, FreqtradeError, FreqtradeUnreachable
from bitgetbot.ledger import append_csv, fetch_krw_per_usdt, fills_to_rows
from bitgetbot.models import Check, ContractSpec, PositionSnapshot
from bitgetbot.privcfg import (
    DRY_DB_URL, LIVE_DB_URL, PRIVATE_PATH, Settings, init_private, load_settings, set_mode,
)
from bitgetbot.stopcheck import MAX_LIQ_FRACTION, evaluate_protection, overall


SYMBOL = "BTCUSDT"
PAIR = "BTC/USDT:USDT"
STATE_PATH = Path(".smoke_state.json")
EXIT_WARNING = "손절 확인 실패. 지금 바로 'python -m bitgetbot.smoke exit'을 실행하세요."
PANEL_EXIT_WARNING = '손절 확인 실패. 지금 바로 패널의 "청산" 버튼을 누르세요.'
LEDGER_RETRY = "청산은 완료됐습니다. 장부 저장에 실패했으니 'python -m bitgetbot.smoke ledger'를 다시 실행하세요."
PANEL_LEDGER_RETRY = '청산은 완료됐습니다. 장부 저장에 실패했으니 패널의 "장부 저장" 버튼을 누르세요.'
NO_FILLS = "조회된 체결이 없습니다. 잠시 후 ledger를 다시 실행하세요."
PANEL_NO_FILLS = '조회된 체결이 없습니다. 잠시 후 패널의 "장부 저장" 버튼을 누르세요.'
RESTART_FAILED = "컨테이너 재시작에 실패했습니다. 패널을 새로 고친 뒤 상태를 확인하세요."
FT_UNREACHABLE = "Freqtrade에 연결할 수 없습니다 (Docker Desktop과 `docker compose up -d` 확인)"
FT_MANUAL_CLOSE = (
    "Freqtrade에 연결할 수 없습니다. Docker Desktop과 컨테이너를 확인하세요. "
    "복구가 안 되면 비트겟 웹(PC)에서 포지션을 직접 시장가 청산하세요."
)
DRY_NOTICE = "dry-run: 거래소 손절 확인은 실전에서만 합니다"
NO_SMOKE_TRADE = "검사할 스모크 거래가 없습니다. enter를 먼저 실행했는지 확인하세요."


@dataclass
class SmokeIO:
    out: Callable[[str], None]
    ask: Callable[[str], str]
    restart: Callable[[], None] | None = None
    # "cli" keeps the terminal text byte-identical; "panel" points to panel buttons instead.
    hint_style: str = "cli"

    @staticmethod
    def console() -> "SmokeIO":
        return SmokeIO(print, input, None)


def _hint(io: SmokeIO, cli: str, panel: str) -> str:
    return panel if io.hint_style == "panel" else cli


def _exit_warning(io: SmokeIO) -> str:
    return _hint(io, EXIT_WARNING, PANEL_EXIT_WARNING)


def _positive(value) -> bool:
    value = num(value)
    return value is not None and math.isfinite(value) and value > 0


def preflight_problems(
    ft_ok: bool, dry_run: bool, account_kind: str, positions: list[PositionSnapshot],
    open_orders: int, ft_open_trades: int, usdt_available: float | None,
) -> list[str]:
    if not ft_ok:
        return [FT_UNREACHABLE]
    problems = []
    if ft_open_trades >= 1:
        problems.append("Freqtrade에 열린 거래가 있습니다. 먼저 정리하세요.")
    if not dry_run:
        if account_kind != "classic":
            problems.append("계정이 classic으로 확인되지 않았습니다. 서브계정 모드를 확인하세요.")
        if positions:
            problems.append("거래소에 기존 포지션이 있습니다. 먼저 정리하세요.")
        if open_orders >= 1:
            problems.append("거래소에 미체결 주문이 있습니다. 먼저 정리하세요.")
        if not _positive(usdt_available) or usdt_available < 10:
            problems.append("사용 가능 USDT를 확인할 수 없거나 10 USDT 미만입니다.")
    return problems


def smoke_stake(
    spec: ContractSpec, mark_price: float, leverage: float, buffer: float = 1.3,
) -> float:
    if not all(_positive(value) for value in (
        spec.min_qty, spec.size_step, spec.min_notional_usdt, mark_price, leverage, buffer,
    )):
        raise ValueError("계약 정보·가격·레버리지·버퍼는 유한한 양수여야 합니다")
    stake = (
        max(Decimal(str(spec.min_qty)) * Decimal(str(mark_price)),
            Decimal(str(spec.min_notional_usdt)))
        * Decimal(str(buffer)) / Decimal(str(leverage))
    ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if stake > 10:
        raise ValueError("최소 주문 증거금이 10 USDT를 초과합니다. 주문하지 않습니다")
    return float(stake)


def require_live_confirmation(dry_run: bool, typed: str) -> bool:
    return dry_run or typed.strip() == "LIVE"


def wait_until(
    predicate: Callable[[], bool], timeout_s: float, interval_s: float,
    sleep=time.sleep, clock=time.monotonic,
) -> bool:
    if timeout_s < 0 or interval_s <= 0:
        raise ValueError("대기 시간은 0 이상, 조회 간격은 양수여야 합니다")
    deadline = clock() + timeout_s
    while True:
        if predicate():
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(interval_s, remaining))


def ft_trade_checks(trade: dict, max_liq_fraction: float = MAX_LIQ_FRACTION) -> list[Check]:
    entry = num(trade.get("open_rate"))
    stop = num(trade.get("stop_loss_abs"))
    liq = num(trade.get("liquidation_price"))
    is_short = trade.get("is_short")
    valid = _positive(entry) and _positive(stop) and type(is_short) is bool
    side_ok = (stop > entry if is_short else stop < entry) if valid else None
    inside_ok = (
        abs(entry - stop) <= max_liq_fraction * abs(entry - liq)
        if valid and _positive(liq) else None
    )
    return [
        Check("stop_side", side_ok, "진입가 대비 손절 방향" if valid else "손절 방향 판정 자료 부족"),
        Check("stop_inside_liq", inside_ok, "손절 거리 / 청산 거리 ≤ 0.7" if inside_ok is not None
              else "청산가 또는 손절가를 알 수 없어 판정 불가"),
    ]


def _print_checks(checks: list[Check], *, io: SmokeIO | None = None) -> None:
    io = io if io is not None else SmokeIO.console()
    io.out("검사 | 결과 | 설명")
    for check in checks:
        result = "UNKNOWN" if check.ok is None else "PASS" if check.ok else "FAIL"
        io.out(f"{check.name} | {result} | {check.detail}")
    io.out(f"종합: {overall(checks)}")


def run_check(
    reader, symbol: str, timeout_s: float = 120, interval_s: float = 5,
    sleep=time.sleep, clock=time.monotonic, out=None, *, io: SmokeIO | None = None,
) -> int:
    io = io if io is not None else SmokeIO.console()
    if out is not None:
        io = SmokeIO(out, io.ask, io.restart, io.hint_style)

    def protected():
        try:
            position = reader.fetch_position(symbol)
            plans = reader.fetch_stop_orders(symbol)
            io.out("손절 후보 전체: 주문 번호 | 계획 유형 | 발동 가격 | 발동 기준")
            candidates = [plan for plan in plans if plan.symbol == symbol]
            for plan in candidates:
                io.out(f"{plan.order_id} | {plan.plan_type} | {plan.trigger_price} | {plan.trigger_type}")
            if not candidates:
                io.out("손절 후보 없음")
            if position is None:
                checks = [Check("position_exists", False, "거래소 포지션이 없습니다")]
            else:
                checks = evaluate_protection(position, plans)
                if (position.side not in {"long", "short"}
                        or not _positive(position.entry_price) or not _positive(position.size)
                        or (position.mark_price is not None and not _positive(position.mark_price))
                        or (position.liq_price is not None and not _positive(position.liq_price))
                        or any(not _positive(plan.trigger_price) for plan in candidates)):
                    checks.append(Check("snapshot_valid", None, "가격 또는 포지션 자료가 유효하지 않습니다"))
            _print_checks(checks, io=io)
            return overall(checks) == "PASS"
        except Exception:
            io.out("조회 실패: 손절 보호를 확인할 수 없습니다. 오류 원문은 비밀값 보호를 위해 생략합니다.")
            return False

    if wait_until(protected, timeout_s, interval_s, sleep, clock):
        return 0
    io.out(_exit_warning(io))
    return 1


def _wait(predicate, timeout_s: float, interval_s: float = 5) -> bool:
    # Only GET-based observation predicates are retried. Never retry order POSTs.
    def observed():
        try:
            return predicate()
        except FreqtradeUnreachable:
            return False

    return wait_until(observed, timeout_s, interval_s, time.sleep, time.monotonic)


def _mode_matches(settings: Settings, config: dict, *, io: SmokeIO | None = None) -> bool:
    io = io if io is not None else SmokeIO.console()
    if type(config.get("dry_run")) is not bool or config["dry_run"] != settings.dry_run:
        io.out("문제: 로컬 설정과 실행 중인 Freqtrade 모드가 다르거나 확인되지 않았습니다. 주문하지 않습니다.")
        return False
    return True


def _preflight(settings: Settings, reader, ft, *, io: SmokeIO | None = None) -> tuple[bool, dict]:
    io = io if io is not None else SmokeIO.console()
    io.out("Freqtrade 시작을 기다리는 중입니다 (최대 3분)…")
    try:
        if not _wait(ft.ping, 180, 3):
            io.out(FT_UNREACHABLE)
            return False, {}
        config = ft.show_config()
        trades = ft.status()
    except (FreqtradeUnreachable, FreqtradeError):
        io.out(FT_UNREACHABLE)
        return False, {}
    if not _mode_matches(settings, config, io=io):
        return False, config
    expected_db = DRY_DB_URL if settings.dry_run else LIVE_DB_URL
    if settings.db_url != expected_db:
        io.out("문제: 설정 모드와 db_url이 일치하지 않습니다. set-mode로 모드와 DB를 함께 설정하세요.")
        return False, config
    account_kind, positions, orders, balance = "unknown", [], 0, None
    plan_orders = 0
    if not settings.dry_run:
        account_kind = classify_account(reader.account_mode_report())
        io.out(f"계정 모드: {account_kind}")
        positions = reader.fetch_positions()
        orders = reader.fetch_open_order_count()
        plan_orders = reader.fetch_plan_order_count()
        try:
            balance = reader.fetch_usdt_available()
        except ValueError:
            balance = None
    problems = preflight_problems(
        True, settings.dry_run, account_kind, positions, orders, len(trades), balance,
    )
    if plan_orders >= 1:
        problems.append(f"미체결 예약(트리거·손절) 주문이 {plan_orders}개 있습니다")
    try:
        stake = smoke_stake(reader.fetch_contract(SYMBOL), reader.fetch_mark_price(SYMBOL), 2)
        io.out(f"예상 증거금: {stake:.2f} USDT (상한 10 USDT)")
    except ValueError:
        problems.append("계약·표시가격을 확인할 수 없거나 최소 주문 증거금이 10 USDT를 초과합니다.")
    for problem in problems:
        io.out(f"문제: {problem}")
    if not problems:
        io.out("사전 점검 통과: 문제 0개")
    return not problems, config


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or type(state.get("started_ms")) is not int
            or state["started_ms"] <= 0):
        raise ValueError("스모크 상태 파일이 유효하지 않습니다")
    trade_id = state.get("trade_id")
    if trade_id is not None and (not str(trade_id).isdigit() or int(trade_id) <= 0):
        raise ValueError("저장된 거래 번호가 유효하지 않습니다")
    return state


def _save_state(state: dict) -> None:
    # Write before the order too: an ambiguous POST must not lose the ledger start time.
    STATE_PATH.write_text(json.dumps(state) + "\n", encoding="utf-8")


def _state_mode_matches(
    settings: Settings, state: dict, *, exiting: bool = False, io: SmokeIO | None = None,
) -> bool:
    io = io if io is not None else SmokeIO.console()
    if state and state.get("mode") != ("dry" if settings.dry_run else "live"):
        next_step = ("Freqtrade 청산을 계속하고 비트겟 웹(PC)에서 포지션을 직접 확인하세요."
                     if exiting else _hint(io, "먼저 exit으로 정리하세요.", '먼저 패널의 "청산" 버튼을 누르세요.'))
        io.out("경고: 스모크 상태 파일의 모드가 현재 모드와 다르거나 확인되지 않았습니다. " + next_step)
        return False
    return True


def _same_trade(trade: dict, trade_id) -> bool:
    return trade_id is not None and str(trade.get("trade_id")) == str(trade_id)


def _enter(settings: Settings, reader, ft, *, io: SmokeIO | None = None) -> int:
    io = io if io is not None else SmokeIO.console()
    if STATE_PATH.exists():
        io.out(_hint(io, "이전 스모크 상태 파일이 남아 있습니다. 먼저 exit으로 정리하세요",
                     '이전 스모크 상태 파일이 남아 있습니다. 먼저 패널의 "청산" 버튼을 누르세요.'))
        return 1
    ready, config = _preflight(settings, reader, ft, io=io)
    if not ready:
        return 1
    typed = "" if settings.dry_run else io.ask("실전 주문을 냅니다. 계속하려면 LIVE 입력:")
    if not require_live_confirmation(settings.dry_run, typed):
        io.out("취소했습니다. 주문하지 않았습니다.")
        return 1
    # Re-read public data immediately before entry; never exceed the stake cap.
    stake = smoke_stake(reader.fetch_contract(SYMBOL), reader.fetch_mark_price(SYMBOL), 2)
    if config.get("state") == "stopped":
        ft.start()
        if not _wait(lambda: ft.show_config().get("state") == "running", 10, 1):
            io.out("10초 내 Freqtrade 실행 상태가 running이 되지 않았습니다. 주문하지 않습니다.")
            return 1
    elif config.get("state") != "running":
        io.out("Freqtrade 실행 상태를 확인할 수 없습니다. 주문하지 않습니다.")
        return 1
    current_config = ft.show_config()
    if not _mode_matches(settings, current_config, io=io):
        return 1
    if current_config.get("state") != "running":
        io.out("Freqtrade 실행 상태가 running이 아닙니다. 주문하지 않습니다.")
        return 1
    state = {"started_ms": time.time_ns() // 1_000_000, "trade_id": None,
             "mode": "dry" if settings.dry_run else "live"}
    _save_state(state)
    try:
        result = ft.force_enter(PAIR, "long", stake, 2, "market", "smoke")
        response_id = result.get("trade_id")

        def visible():
            trades = ft.status()
            candidates = [trade for trade in trades if trade.get("pair") == PAIR
                          and (_same_trade(trade, response_id) if response_id is not None
                               else trade.get("enter_tag") == "smoke")]
            if len(candidates) != 1 or candidates[0].get("trade_id") is None:
                return False
            state["trade_id"] = candidates[0]["trade_id"]
            return True

        if not _wait(visible, 60):
            io.out("60초 내 거래를 확인하지 못했습니다. enter를 반복하지 마세요.")
            io.out(_exit_warning(io))
            return 1
        _save_state(state)
        io.out(f"진입 확인: 거래 번호 {state['trade_id']}, 증거금 {stake:.2f} USDT")
        return 0
    except (Exception, KeyboardInterrupt):
        # The saved attempt marks the boundary after which the POST may have reached Freqtrade.
        io.out("진입 확인 실패: 비밀값 보호를 위해 오류 원문은 표시하지 않습니다.")
        io.out(_exit_warning(io))
        return 1


def _check(settings: Settings, reader, ft, *, io: SmokeIO | None = None) -> int:
    io = io if io is not None else SmokeIO.console()
    state = _load_state()
    if not state:
        io.out(NO_SMOKE_TRADE)
        return 1
    if not _state_mode_matches(settings, state, io=io):
        return 1
    if not _mode_matches(settings, ft.show_config(), io=io):
        io.out(_exit_warning(io))
        return 1
    trades = [trade for trade in ft.status() if trade.get("pair") == PAIR
              and (not state.get("trade_id") or _same_trade(trade, state["trade_id"]))]
    if not trades:
        io.out(NO_SMOKE_TRADE)
        return 1
    if len(trades) != 1:
        io.out("검사할 스모크 거래를 하나로 확인할 수 없습니다.")
        io.out(_exit_warning(io))
        return 1
    if not settings.dry_run:
        return run_check(reader, SYMBOL, 120, 5, time.sleep, time.monotonic, io=io)
    io.out(DRY_NOTICE)
    checks = ft_trade_checks(trades[0])
    _print_checks(checks, io=io)
    result = overall(checks)
    if result == "FAIL":
        io.out(_exit_warning(io))
        return 1
    if result == "UNKNOWN":
        io.out("경고: 청산가를 알 수 없거나 판정 자료가 부족합니다. 모의 점검만 계속하며 실전에서는 PASS가 필요합니다.")
    return 0


def _restart_check(settings: Settings, reader, ft, *, io: SmokeIO | None = None) -> int:
    io = io if io is not None else SmokeIO.console()
    state = _load_state()
    if not state:
        io.out(NO_SMOKE_TRADE)
        return 1
    if not _state_mode_matches(settings, state, io=io):
        return 1
    trade_id = state.get("trade_id")
    if trade_id is None:
        io.out("저장된 거래 번호가 없습니다.")
        io.out(_exit_warning(io))
        return 1
    config = ft.show_config()
    if config.get("state") != "running":
        io.out("재시작 점검 전 Freqtrade 실행 상태가 running이어야 합니다.")
        return 1
    if not _mode_matches(settings, config, io=io):
        io.out(_exit_warning(io))
        return 1
    unconfirmed = "재시작이 확인되지 않았습니다. 컨테이너를 재시작한 뒤 다시 실행하세요."
    startup_before = ft.health().get("bot_startup_ts")
    if type(startup_before) is not int:
        io.out(unconfirmed)
        return 1
    if not any(_same_trade(trade, trade_id) and trade.get("pair") == PAIR for trade in ft.status()):
        io.out(NO_SMOKE_TRADE)
        return 1
    if io.restart is not None:
        io.out("컨테이너를 재시작합니다…")
        try:
            io.restart()
        except Exception:
            # A restart failure is not a stop-check failure; the trade is still protected by its stop.
            io.out(RESTART_FAILED)
            return 1
    else:
        io.out("다음 명령을 직접 실행하세요: docker compose -f freqtrade/docker-compose.yml restart freqtrade")
        io.ask("재시작 명령 실행 후 Enter를 누르세요: ")
    if not _wait(ft.ping, 90):
        io.out(FT_MANUAL_CLOSE)
        return 1
    config = ft.show_config()
    if config.get("state") != "stopped":
        io.out(unconfirmed)
        return 1
    if not _mode_matches(settings, config, io=io):
        io.out(_exit_warning(io))
        return 1
    ft.start()
    if not _wait(lambda: ft.show_config().get("state") == "running", 10, 1):
        io.out(unconfirmed)
        return 1

    def startup_advanced():
        startup_after = ft.health().get("bot_startup_ts")
        return type(startup_after) is int and startup_after > startup_before

    if not _wait(startup_advanced, 15, 1):
        io.out(unconfirmed)
        return 1
    time.sleep(30)
    if not any(_same_trade(trade, trade_id) and trade.get("pair") == PAIR for trade in ft.status()):
        io.out(NO_SMOKE_TRADE)
        return 1
    io.out(f"재시작 후 같은 거래 번호 확인: {trade_id}")
    return _check(settings, reader, ft, io=io)


def _ledger(settings: Settings, reader, *, io: SmokeIO | None = None) -> int:
    io = io if io is not None else SmokeIO.console()
    if settings.dry_run:
        io.out("모의(DRY-RUN): 거래소 체결 장부 내보내기를 생략합니다.")
        return 0
    state = _load_state()
    if not state:
        io.out("장부 시작 시각이 없습니다. .smoke_state.json을 확인하세요.")
        return 1
    if not _state_mode_matches(settings, state, io=io):
        return 1
    fills = reader.fetch_fills(SYMBOL, state["started_ms"])
    if not fills:
        io.out(_hint(io, NO_FILLS, PANEL_NO_FILLS))
        return 1
    rate, at = fetch_krw_per_usdt()
    rows = fills_to_rows(fills, rate, at)
    date = datetime.fromtimestamp(state["started_ms"] / 1000, timezone.utc).strftime("%Y%m%d")
    path = Path("ledger") / f"smoke-{date}.csv"
    count = append_csv(rows, path)
    io.out(f"장부 저장: {path.as_posix()}, 새 체결 {count}개")
    if state.get("close_confirmed") is True:
        STATE_PATH.unlink(missing_ok=True)
    return 0


def _exit(settings: Settings, reader, ft, *, io: SmokeIO | None = None) -> int:
    io = io if io is not None else SmokeIO.console()
    config = ft.show_config()
    state = _load_state()
    state_matches = _state_mode_matches(settings, state, exiting=True, io=io)
    mode_matches = (type(config.get("dry_run")) is bool
                    and config["dry_run"] == settings.dry_run and state_matches)
    if not mode_matches:
        io.out("경고: 모드가 다르거나 확인되지 않았습니다. Freqtrade의 열린 거래를 청산하고 비트겟 웹(PC)에서 포지션을 직접 확인하세요.")
    # A trade ID from another mode's database must not select a trade in this database.
    ft.force_exit((state.get("trade_id") if mode_matches else None) or "all", "market")

    def closed():
        try:
            ft_empty = not ft.status()
            exchange_empty = (True if settings.dry_run or not mode_matches
                              else reader.fetch_position(SYMBOL) is None)
            return ft_empty and exchange_empty
        except Exception:
            # Observation only: retry transient GET errors, never the force_exit POST.
            io.out("청산 조회 실패: 제한 시간 안에서 다시 확인합니다. 오류 원문은 표시하지 않습니다.")
            return False

    if not _wait(closed, 60):
        io.out("60초 내 청산을 확인하지 못했습니다. 비트겟 웹(PC)에서 포지션을 직접 확인하고 시장가 청산하세요.")
        return 1
    io.out("청산 확인: Freqtrade 열린 거래 0개" + (
        ", 거래소 포지션 없음" if not settings.dry_run and mode_matches else ""
    ))
    if not mode_matches:
        io.out("모드 불일치로 거래소 청산 여부는 확인하지 않았습니다. 비트겟 웹(PC)에서 직접 확인하세요.")
        return 1
    if not settings.dry_run and not state:
        io.out("청산은 완료됐습니다. 장부 시작 시각이 없어 장부를 저장하지 않았습니다. 비트겟 웹(PC)에서 체결 내역을 확인하세요.")
        return 1
    try:
        if state:
            state["close_confirmed"] = True
            _save_state(state)
        if _ledger(settings, reader, io=io) != 0:
            raise ValueError("장부 저장 실패")
    except (Exception, KeyboardInterrupt):
        # Keep the saved start time so the standalone ledger command remains usable.
        io.out(_hint(io, LEDGER_RETRY, PANEL_LEDGER_RETRY))
        return 1
    STATE_PATH.unlink(missing_ok=True)
    return 0


def run_command(command: str, io: SmokeIO) -> int:
    try:
        settings = load_settings()
        if type(settings.dry_run) is not bool:
            io.out("dry_run 설정은 true 또는 false여야 합니다.")
            return 1
        io.out("=== 모드: 모의(DRY-RUN) ===" if settings.dry_run else "=== 모드: 실전(LIVE) ===")
        # Dry-run constructs a credential-free adapter and never calls its private methods.
        credentials = ("", "", "") if settings.dry_run else (settings.key, settings.secret, settings.password)
        reader = BitgetReader(make_exchange(*credentials))
        if command == "ledger":
            return _ledger(settings, reader, io=io)
        ft = FreqtradeClient(settings.api_url, settings.api_user, settings.api_pass)
        if command == "preflight":
            return 0 if _preflight(settings, reader, ft, io=io)[0] else 1
        handlers = {"enter": _enter, "check": _check, "restart-check": _restart_check, "exit": _exit}
        return handlers[command](settings, reader, ft, io=io)
    except (Exception, KeyboardInterrupt) as error:
        # Upstream exceptions and configs may include credentials. Never print their repr/body.
        if (isinstance(error, FreqtradeUnreachable)
                and command in {"check", "restart-check"} and STATE_PATH.exists()):
            io.out(FT_MANUAL_CLOSE)
            return 1
        io.out("명령 실패: 설정·연결·응답을 확인하세요. 비밀값 보호를 위해 오류 원문은 표시하지 않습니다.")
        if command in {"check", "restart-check"}:
            io.out(_exit_warning(io))
        if command == "exit":
            io.out("비트겟 웹(PC)에서 서브계정 선물 포지션을 직접 확인하고 시장가 청산하세요.")
        return 1


class _KoreanHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix="사용법: ")


class _KoreanParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, add_help=False, formatter_class=_KoreanHelpFormatter)
        self.add_argument_group("옵션").add_argument(
            "-h", "--help", action="help", help="도움말 표시 후 종료",
        )

    def error(self, message):
        self.print_usage(sys.stderr)
        # Do not echo arbitrary input: users may accidentally paste a credential.
        self.exit(2, "오류: 명령 또는 인수를 확인하세요. --help로 도움말을 볼 수 있습니다.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _KoreanParser(
        prog="python -m bitgetbot.smoke", description="비트겟 선물 스모크 테스트 (주문은 Freqtrade만 사용)",
    )
    commands = parser.add_subparsers(dest="command", required=True, title="명령")
    for name, help_text in (
        ("init", "비밀 설정 파일 생성"), ("preflight", "주문 전 안전 점검"),
        ("enter", "최소 롱 주문 진입"), ("check", "손절 보호 확인"),
        ("restart-check", "사용자 재시작 후 같은 거래와 손절 확인"),
        ("exit", "시장가 청산 후 장부 저장"), ("ledger", "체결 장부 저장"),
    ):
        commands.add_parser(name, help=help_text, description=help_text)
    mode_parser = commands.add_parser("set-mode", help="모드와 거래 DB 설정", description="모드와 거래 DB 설정")
    mode_parser.add_argument_group("인수").add_argument(
        "mode", choices=("dry", "live"), help="dry: 모의, live: 실전",
    )
    args = parser.parse_args(argv)
    command = args.command
    try:
        if command == "set-mode":
            set_mode(args.mode)
            print("모드 설정 완료: " + ("모의(DRY-RUN)" if args.mode == "dry" else "실전(LIVE)"))
            print("변경 적용에 필요합니다: docker compose -f freqtrade/docker-compose.yml restart freqtrade")
            return 0
        if command == "init":
            print(f"{PRIVATE_PATH}: " + ("생성 완료" if init_private() else "이미 있음"))
            print("exchange.key/secret/password를 채우세요. 모드는 set-mode dry 또는 set-mode live로 설정하세요.")
            print("자동 생성된 api_server 인증값은 유지하세요. 비밀값을 화면·로그·깃에 남기지 마세요.")
            return 0
    except (Exception, KeyboardInterrupt):
        print("명령 실패: 설정·연결·응답을 확인하세요. 비밀값 보호를 위해 오류 원문은 표시하지 않습니다.")
        return 1
    return run_command(command, SmokeIO.console())


if __name__ == "__main__":
    raise SystemExit(main())
