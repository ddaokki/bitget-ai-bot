"""Read-only summary for the robo-briefing trading widget: contract v1 (design spec §10.5).

Nothing here places orders or changes bot state: the only calls are Freqtrade GETs, Bitget
read-only snapshots and the local equity log. Every upstream value is allow-listed into plain
numbers or fixed panel text; exceptions are swallowed so upstream text never reaches a response.
"""

import json
import math
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bitgetbot.panel import charts
from bitgetbot.panel.ops import is_strategy_tag, observe
from bitgetbot.privcfg import Settings

CACHE_S = 3.0           # build at most once per 3 s however many widgets poll (each every 5 s)
# The widget gives up after 3 s. Short local timeouts keep a hung Freqtrade API or a stuck
# Docker Desktop from outliving that, so the board still receives the critical alerts.
FT_TIMEOUT_S = 1.5
DOCKER_TIMEOUT_S = 2
SPARK_DAYS = 7
SPARK_POINTS = 48
SYMBOL = re.compile(r"[A-Z0-9]{1,20}USDT")
STATES = {"running": "running", "paused": "paused", "stopped": "stopped", "reload_config": "paused"}
REASONS = {
    "paused": "새 진입 멈춤 · 열린 포지션은 계속 관리",
    "stopped": "거래 봇 중지됨",
    "reload_config": "거래 봇 설정 다시 읽는 중",
}
BOT_UNREACHABLE = "거래 봇에 연결할 수 없습니다"
DOCKER_DOWN = "Docker가 꺼져 있습니다"
BOT_UNKNOWN = "거래 봇 상태를 확인할 수 없습니다"
MODE_MISMATCH = "거래 봇 모드가 저장된 모드와 다릅니다"
POSITIONS_UNKNOWN = "실전 포지션을 확인할 수 없습니다"
NO_STOP = "{symbol} 거래소 손절 주문이 없습니다"
STOP_UNKNOWN = "{symbol} 거래소 손절을 확인할 수 없습니다"
EQUITY_UNKNOWN = "자산을 읽지 못했습니다"
FX_UNKNOWN = "환율을 받지 못해 원화를 표시하지 못합니다"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _positive(value) -> float | None:
    result = charts.number(value)
    return result if result is not None and result > 0 else None


def clean(value):
    """Non-finite floats become null anywhere in the payload (the contract shows them as `–`)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def _times(value, factor) -> float | None:
    return value * factor if value is not None and factor is not None else None


def _pct(gain: float | None, equity: float | None) -> float | None:
    """Gain relative to equity at the start of the period, i.e. equity now minus that gain."""
    if gain is None or equity is None:
        return None
    base = equity - gain
    return gain / base * 100 if base > 0 else None


def _symbol(pair) -> str | None:
    if not isinstance(pair, str):
        return None
    symbol = pair.split(":")[0].replace("/", "")
    return symbol if SYMBOL.fullmatch(symbol) else None


def _side(is_short) -> str | None:
    return ("short" if is_short else "long") if type(is_short) is bool else None


def _mode(tag) -> str:
    return "safe" if is_strategy_tag(tag) else "manual"


def _today_profit(daily, today: str) -> float | None:
    rows = daily.get("data") if isinstance(daily, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("date")) == today:
            return charts.number(row.get("abs_profit"))
    return None


def _thin(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    picks = sorted({round(index * (len(points) - 1) / (limit - 1)) for index in range(limit)})
    return [points[index] for index in picks]


def _dry_position(row: dict) -> dict:
    ratio = charts.number(row.get("profit_ratio"))
    return {
        "symbol": _symbol(row.get("pair")) or "UNKNOWN", "side": _side(row.get("is_short")),
        "mode": _mode(row.get("enter_tag")), "leverage": _positive(row.get("leverage")),
        "entry": _positive(row.get("open_rate")), "mark": _positive(row.get("current_rate")),
        "stopLoss": _positive(row.get("stop_loss_abs")), "takeProfit": None,
        "liqPrice": _positive(row.get("liquidation_price")), "marginUsdt": _positive(row.get("stake_amount")),
        "uPnlUsdt": charts.number(row.get("profit_abs")), "uPnlPct": ratio * 100 if ratio is not None else None,
    }


def _live_position(row, tag, stop: float | None) -> dict:
    side = row.side if row.side in {"long", "short"} else None
    size, entry, mark = _positive(row.size), _positive(row.entry_price), _positive(row.mark_price)
    leverage = _positive(row.leverage)
    margin = _positive(row.margin)
    if margin is None and None not in (size, entry, leverage):
        margin = entry * size / leverage
    pnl = charts.number(row.unrealized_pnl)
    if pnl is None and None not in (size, entry, mark, side):
        pnl = (mark - entry) * size * (1 if side == "long" else -1)
    return {
        "symbol": row.symbol if isinstance(row.symbol, str) and SYMBOL.fullmatch(row.symbol) else "UNKNOWN",
        "side": side, "mode": _mode(tag), "leverage": leverage, "entry": entry, "mark": mark,
        "stopLoss": stop, "takeProfit": None, "liqPrice": _positive(row.liq_price),
        "marginUsdt": margin, "uPnlUsdt": pnl,
        "uPnlPct": pnl / margin * 100 if pnl is not None and margin else None,
    }


def _stop_price(reader, symbol: str, side: str | None) -> tuple[float | None, bool]:
    """(protective exchange stop trigger, whether the stop orders could be read).

    With several stops the one furthest from the market is reported: the lowest for a long
    (the strategy only goes long), the highest for a short.
    """
    orders = observe(lambda: reader.fetch_stop_orders(symbol))
    if not isinstance(orders, list):
        return None, False
    triggers = [price for price in (_positive(getattr(order, "trigger_price", None)) for order in orders)
                if price is not None]
    if not triggers:
        return None, True
    return (max(triggers) if side == "short" else min(triggers)), True


@dataclass
class Sources:
    settings: Settings
    reader: Any            # Bitget read-only reader; unused in dry mode
    ft: Any                # Freqtrade client; only GET methods are called
    fx: charts.FxCache
    equity_log: charts.EquityLog
    docker_running: Callable[[], bool]
    clock: Callable[[], float]


def build(src: Sources) -> dict:
    """Contract v1 content without v/seq/updatedAt (the cache adds those)."""
    dry = src.settings.dry_run
    log_mode = "dry" if dry else "live"
    ft = src.ft
    alerts: list[dict] = []

    def alert(level: str, text: str) -> None:
        alerts.append({"level": level, "text": text})

    reachable = observe(ft.ping) is True
    config = observe(ft.show_config) if reachable else None
    ft_state = config.get("state") if isinstance(config, dict) else None
    ft_dry = config.get("dry_run") if isinstance(config, dict) else None
    if not reachable:
        state = "stopped"
        reason = BOT_UNREACHABLE
        if observe(src.docker_running) is not True:
            reason = DOCKER_DOWN
            alert("critical", DOCKER_DOWN)
        alert("critical", BOT_UNREACHABLE)
    elif ft_state not in STATES or type(ft_dry) is not bool:
        state, reason = "stopped", BOT_UNKNOWN
        alert("critical", BOT_UNKNOWN)
    elif ft_dry is not dry:
        state, reason = STATES[ft_state], MODE_MISMATCH
        alert("critical", MODE_MISMATCH)
    else:
        state, reason = STATES[ft_state], REASONS.get(ft_state)
    # Freqtrade data counts only when the bot runs in the saved mode (separate databases per mode).
    ft_ok = reachable and type(ft_dry) is bool and ft_dry is dry

    if dry:
        equity = observe(lambda: charts.current_equity(True, None, ft)) if ft_ok else None
    else:
        equity = observe(lambda: charts.current_equity(False, src.reader, ft))
    if equity is not None:
        observe(lambda: src.equity_log.maybe_record(log_mode, lambda: equity))
    else:
        alert("warn", EQUITY_UNKNOWN)
    fx = observe(src.fx.rate_at)
    rate, fx_at = fx if isinstance(fx, tuple) and len(fx) == 2 else (None, None)
    rate = _positive(rate)
    if rate is None:
        alert("warn", FX_UNKNOWN)

    now = src.clock()
    today_usdt = total_usdt = None
    open_trades = None
    if ft_ok:
        today_usdt = _today_profit(observe(lambda: ft.daily(1)), iso(now)[:10])
        profit = observe(ft.profit)
        total_usdt = charts.number(profit.get("profit_closed_coin")) if isinstance(profit, dict) else None
        trades = observe(ft.status)
        open_trades = [row for row in trades if isinstance(row, dict)] if isinstance(trades, list) else None

    if dry:
        positions = [_dry_position(row) for row in open_trades or []]
    else:
        rows = observe(src.reader.fetch_positions)
        positions = []
        if not isinstance(rows, list):
            alert("critical", POSITIONS_UNKNOWN)
        for row in rows if isinstance(rows, list) else []:
            symbol = getattr(row, "symbol", None)
            side = getattr(row, "side", None)
            tag = next((trade.get("enter_tag") for trade in open_trades or []
                        if _symbol(trade.get("pair")) == symbol and _side(trade.get("is_short")) == side), None)
            stop, known = _stop_price(src.reader, symbol, side) if isinstance(symbol, str) else (None, False)
            position = observe(lambda: _live_position(row, tag, stop))
            if position is None:
                alert("critical", POSITIONS_UNKNOWN)
                continue
            positions.append(position)
            if stop is None:
                alert("critical", (NO_STOP if known else STOP_UNKNOWN).format(symbol=position["symbol"]))

    history = observe(lambda: src.equity_log.history(log_mode))
    cutoff = now - SPARK_DAYS * 86400
    recent = [point for point in history if point[0] >= cutoff] if isinstance(history, list) else []
    spark = [{"t": iso(ts), "krw": value * rate} for ts, value in _thin(recent, SPARK_POINTS)] if rate else []

    alerts.sort(key=lambda item: item["level"] != "critical")
    return {
        "mode": "dry_run" if dry else "live",
        "bot": {"state": state, "reason": reason,
                "aggressive": {"armed": False, "budgetKrw": 0, "lossThisWeekKrw": 0}},
        "equity": {"krw": _times(equity, rate), "usdt": equity, "krwPerUsdt": rate,
                   "fxAt": iso(fx_at) if rate is not None and fx_at is not None else None},
        "pnl": {
            "todayKrw": _times(today_usdt, rate), "todayUsdt": today_usdt, "todayPct": _pct(today_usdt, equity),
            "totalKrw": _times(total_usdt, rate), "totalUsdt": total_usdt, "totalPct": _pct(total_usdt, equity),
        },
        "spark": spark,
        "positions": positions,
        "pending": [],
        "ai": None,
        "alerts": alerts,
    }


class SummaryCache:
    """Builds at most once per CACHE_S and numbers each distinct content with a new seq.

    seq starts from the wall-clock second, so after a panel restart it is still larger than any
    seq handed out before (builds are at least 3 s apart): an old ETag can never match new data.
    Nothing is persisted.
    """

    def __init__(self, build: Callable[[], dict], clock: Callable[[], float], interval: float = CACHE_S):
        self._build = build
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._seq = int(clock())
        self._key: str | None = None
        self._summary: dict | None = None
        self._at = float("-inf")
        self._failed = False

    def get(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._at <= now < self._at + self._interval:
                if self._failed:
                    raise RuntimeError("summary unavailable")
                return self._summary
            # The window starts when a build finishes: a slow build is still reused for 3 s, and
            # requests that waited on the lock meanwhile get its result instead of rebuilding.
            try:
                content = clean(self._build())
                key = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False)
            except Exception:
                self._failed = True
                self._at = self._clock()
                raise RuntimeError("summary unavailable") from None
            self._at = self._clock()
            if key != self._key:
                self._seq += 1
                self._key = key
            self._failed = False
            self._summary = {"v": 1, "seq": self._seq, "updatedAt": iso(self._at), **content}
            return self._summary
