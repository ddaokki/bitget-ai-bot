"""Bounded local operations and credential-free control-panel snapshots."""

import json
import math
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from bitgetbot.bitget_read import classify_account
from bitgetbot.privcfg import Settings
from bitgetbot.smoke import STATE_PATH, SYMBOL


Runner = Callable[[list[str], int], tuple[int, str]]
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "freqtrade/docker-compose.yml"]


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 0 on non-Windows


def default_runner(argv: list[str], timeout_s: int) -> tuple[int, str]:
    """Run argv without a shell, keeping process diagnostics out of panel results."""
    try:
        result = subprocess.run(
            argv, cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            # The panel runs under pythonw (no console); without this flag every docker/
            # tailscale status poll pops up and closes a console window on Windows.
            creationflags=NO_WINDOW,
        )
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""


def _run(run: Runner, argv: list[str], timeout_s: int) -> tuple[int, str]:
    try:
        return run(argv, timeout_s)
    except Exception:
        return 127, ""


def docker_running(run: Runner = default_runner) -> bool:
    return _run(run, ["docker", "info"], 10)[0] == 0


def compose_up(run: Runner = default_runner) -> bool:
    return _run(run, [*COMPOSE, "up", "-d"], 120)[0] == 0


def compose_restart(run: Runner = default_runner) -> bool:
    return _run(run, [*COMPOSE, "restart", "freqtrade"], 90)[0] == 0


def freqtrade_container_running(run: Runner = default_runner) -> bool:
    code, output = _run(run, [*COMPOSE, "ps", "--format", "json"], 10)
    if code != 0:
        return False
    try:
        rows = json.loads(output)
        rows = rows if isinstance(rows, list) else [rows]
    except (ValueError, TypeError):
        try:
            rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        except (ValueError, TypeError, AttributeError):
            return False
    return any(isinstance(row, dict) and row.get("Service") == "freqtrade"
               and row.get("State") == "running" for row in rows)


def _resolve_tailscale() -> str | None:
    binary = shutil.which("tailscale")
    if binary:
        return binary
    fallback = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    return str(fallback) if fallback.is_file() else None


TAILSCALE = _resolve_tailscale()


def _tailscale_json(run: Runner, *args: str) -> dict | None:
    code, output = _run(run, [TAILSCALE, *args], 15)
    if code == 0:
        try:
            value = json.loads(output)
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError, RecursionError):
            pass
    return None


def _funnel_enabled(value) -> bool | None:
    """Distinguish confirmed disabled config from failed or malformed observations."""
    if not isinstance(value, dict):
        return None
    pending = [value]
    unknown = False
    while pending:
        config = pending.pop()
        if "AllowFunnel" in config:
            allowed = config["AllowFunnel"]
            if not isinstance(allowed, dict):
                unknown = True
            elif any(item is True for item in allowed.values()):
                return True
            elif any(type(item) is not bool for item in allowed.values()):
                unknown = True
        if any(name in config and not isinstance(config[name], dict) for name in ("Web", "TCP")):
            unknown = True
        pending.extend(item for item in config.values() if isinstance(item, dict))
    return None if unknown else False


def tailscale_status(run: Runner = default_runner) -> dict:
    result = {"installed": TAILSCALE is not None, "logged_in": False,
              "dns_name": None, "serving": False, "funnel": False}
    if TAILSCALE is None:
        return result
    status = _tailscale_json(run, "status", "--json") or {}
    result["logged_in"] = status.get("BackendState") == "Running"
    own = status.get("Self")
    dns = own.get("DNSName") if isinstance(own, dict) else None
    if isinstance(dns, str):
        dns = dns.removesuffix(".")
        if re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.[a-zA-Z0-9-]+\.ts\.net", dns):
            result["dns_name"] = dns
    serve = _tailscale_json(run, "serve", "status", "--json")
    result["serving"] = isinstance(serve, dict) and any(
        isinstance(serve.get(name), dict) and bool(serve[name]) for name in ("Web", "TCP")
    )
    funnel_states = [_funnel_enabled(serve),
                     _funnel_enabled(_tailscale_json(run, "funnel", "status", "--json"))]
    result["funnel"] = (True if any(state is True for state in funnel_states)
                        else None if any(state is None for state in funnel_states) else False)
    return result


def enable_phone_access(run: Runner = default_runner) -> tuple[bool, str]:
    status = tailscale_status(run)
    if not status["installed"]:
        return False, "Tailscale을 설치하세요."
    if status["funnel"] is True:
        return False, "Funnel이 켜져 있어 휴대폰 접속을 켤 수 없습니다. 먼저 Funnel을 끄세요."
    if status["funnel"] is not False:
        return False, "Funnel이 꺼져 있는지 확인할 수 없어 휴대폰 접속을 켤 수 없습니다. Tailscale 설정을 확인하세요."
    if not status["logged_in"]:
        return False, "Tailscale에 로그인하세요."
    if _run(run, [TAILSCALE, "serve", "--bg", "8090"], 30)[0] == 0:
        status = tailscale_status(run)
        if (status["installed"] and status["logged_in"] and status["serving"]
                and status["funnel"] is False and status["dns_name"]):
            return True, f"휴대폰 접속 주소: https://{status['dns_name']}"
    # A failed command or unsafe recheck may still have changed the mapping.
    if _run(run, [TAILSCALE, "serve", "reset"], 30)[0] == 0:
        return False, "안전한 휴대폰 접속을 확인하지 못해 공개 설정을 초기화했습니다. Tailscale 설정을 확인하세요."
    return False, "휴대폰 접속 확인과 공개 설정 초기화에 실패했습니다. Tailscale Serve/Funnel 설정을 직접 확인하고 끄세요."


def observe(read: Callable):
    try:
        return read()
    except Exception:
        # Never return exception names, messages, bodies, or upstream response objects.
        return None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


def _choice(value, choices: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in choices else None


def _position(symbol, side, size, entry, mark, liq, leverage) -> dict:
    return {
        "symbol": symbol if isinstance(symbol, str) and re.fullmatch(r"[A-Z0-9]{1,20}USDT", symbol) else None,
        "side": _choice(side, ("long", "short")), "size": _number(size),
        "entry": _number(entry), "mark": _number(mark), "liq": _number(liq),
        "leverage": _number(leverage),
    }


def _dry_positions(ft) -> list[dict]:
    return [
        _position(
            row.get("pair", "").split(":")[0].replace("/", ""),
            ("short" if row["is_short"] else "long") if type(row.get("is_short")) is bool else None,
            row.get("amount"), row.get("open_rate"), row.get("current_rate"),
            row.get("liquidation_price"), row.get("leverage"),
        )
        for row in ft.status()
    ]


def _live_positions(reader) -> list[dict]:
    return [_position(row.symbol, row.side, row.size, row.entry_price, row.mark_price,
                      row.liq_price, row.leverage) for row in reader.fetch_positions()]


def _stops(reader) -> list[dict]:
    return [{"plan_type": _choice(row.plan_type, ("pos_loss", "loss_plan")),
             "trigger_price": _number(row.trigger_price),
             "trigger_type": _choice(row.trigger_type, ("mark_price", "fill_price"))}
            for row in reader.fetch_stop_orders(SYMBOL)]


def _account(reader) -> dict:
    return {"kind": classify_account(reader.account_mode_report()),
            "usdt_available": _number(reader.fetch_usdt_available())}


def _smoke_state() -> dict:
    result = {"exists": True, "trade_id": None, "mode": None, "close_confirmed": False}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return result
        trade_id = state.get("trade_id")
        valid_id = (trade_id is None or (type(trade_id) is int and trade_id > 0)
                    or (isinstance(trade_id, str) and trade_id.isascii()
                        and trade_id.isdigit() and int(trade_id) > 0))
        mode = state.get("mode")
        closed = state.get("close_confirmed", False)
        if (not valid_id or (mode is not None and _choice(mode, ("dry", "live")) is None)
                or type(closed) is not bool):
            return result
        result.update(trade_id=trade_id, mode=mode, close_confirmed=closed)
    except FileNotFoundError:
        result["exists"] = False
    except Exception:
        pass
    return result


def smoke_state_exists() -> bool:
    """Fail closed: an unreadable or invalid state file still counts as an open smoke trade."""
    return _smoke_state()["exists"]


DAY_MS = 86_400_000
RECENT_DAYS = 30


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


STRATEGY_TAG_PREFIX = "safe_"  # same rule as freqtrade/user_data/strategies/live_rules.py
TRADE_PAGE = 500


def is_strategy_tag(tag) -> bool:
    """Strategy entries carry a "safe_" tag; manual and smoke-test entries do not."""
    return isinstance(tag, str) and tag.startswith(STRATEGY_TAG_PREFIX)


def _closed_rows(history) -> tuple[list[tuple[int, float, bool]], bool] | None:
    """(close ms, profit ratio, is strategy) for each closed trade, and whether the page is partial."""
    rows = history.get("trades") if isinstance(history, dict) else None
    total = history.get("total_trades") if isinstance(history, dict) else None
    if not isinstance(rows, list) or _count(total) is None:
        return None
    closed = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        if row.get("is_open") is True:
            continue
        closed_at = row.get("close_timestamp")
        # Freqtrade counts a closed trade with no profit ratio as 0, which is a win.
        profit = 0.0 if row.get("profit_ratio") is None else _number(row.get("profit_ratio"))
        if type(closed_at) is not int or profit is None:
            return None
        closed.append((closed_at, profit, is_strategy_tag(row.get("enter_tag"))))
    return closed, total > len(rows)


def _stats(ft) -> dict | None:
    """Current-mode strategy statistics. Manual/smoke trades are counted apart, never as wins or losses."""
    profit = ft.profit()
    if not isinstance(profit, dict):
        return None
    closed_all = _count(profit.get("closed_trade_count"))
    if closed_all is None:
        return None
    result = {"closed_trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_profit_pct": None,
              "win_rate_30d": None, "closed_trades_30d": 0, "other_trades": 0, "partial": False}
    if closed_all == 0:
        return result
    # The aggregate mixes strategy and manual trades, so count from the trade list instead.
    found = _closed_rows(ft.trades(TRADE_PAGE))
    if found is None:
        return None
    closed, partial = found
    cutoff = time.time() * 1000 - RECENT_DAYS * DAY_MS
    profits = [profit for _, profit, strategy in closed if strategy]
    recent = [profit for closed_at, profit, strategy in closed if strategy and closed_at >= cutoff]
    wins = sum(1 for profit in profits if profit >= 0)
    result.update(
        closed_trades=len(profits), wins=wins, losses=len(profits) - wins,
        win_rate=wins / len(profits) if profits else None,
        avg_profit_pct=_number(sum(profits) / len(profits) * 100) if profits else None,
        other_trades=sum(1 for *_, strategy in closed if not strategy), partial=partial,
        closed_trades_30d=len(recent),
        win_rate_30d=sum(1 for profit in recent if profit >= 0) / len(recent) if recent else None,
    )
    # A partial newest page may not reach back 30 days.
    oldest = min((closed_at for closed_at, *_ in closed), default=None)
    if partial and (oldest is None or oldest >= cutoff):
        result.update(closed_trades_30d=None, win_rate_30d=None)
    return result


def collect_status(settings: Settings, reader, ft) -> dict:
    """Collect independent snapshots; dry mode never touches private exchange APIs."""
    reachable = observe(ft.ping) is True
    freqtrade = {"reachable": reachable, "state": None, "dry_run": None}
    if reachable:
        config = observe(ft.show_config)
        if isinstance(config, dict):
            freqtrade["state"] = _choice(config.get("state"), ("running", "paused", "stopped", "reload_config"))
            dry_run = config.get("dry_run")
            freqtrade["dry_run"] = dry_run if type(dry_run) is bool else None
    return {
        "mode": "dry" if settings.dry_run else "live", "freqtrade": freqtrade,
        "account": None if settings.dry_run else observe(lambda: _account(reader)),
        "positions": (observe(lambda: _dry_positions(ft)) if reachable else None)
        if settings.dry_run else observe(lambda: _live_positions(reader)),
        "stops": None if settings.dry_run else observe(lambda: _stops(reader)),
        "smoke_state": _smoke_state(),
        # Dry and live use separate Freqtrade databases, so these are current-mode stats.
        "stats": observe(lambda: _stats(ft)) if reachable else None,
    }
