"""Read-only chart data for the panel: account equity history and BTC 1h closes.

Nothing here places orders. Every upstream value is allow-listed into plain numbers, and
callers wrap reads so exceptions (which may carry upstream text) never reach a response.
"""

import json
import math
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from bitgetbot.panel.ops import TRADE_PAGE, is_strategy_tag

BTC_PAIR = "BTC/USDT:USDT"
BTC_TIMEFRAME = "1h"
BTC_CANDLES = 72
HOUR_MS = 3_600_000
SNAPSHOT_INTERVAL_S = 600     # at most one equity snapshot per mode every 10 minutes
SNAPSHOT_RETRY_S = 60         # a failed read is retried sooner, but not on every 5 s poll
HISTORY_DAYS = 30
MAX_POINTS = 720              # the phone chart never needs more points than this
PRUNE_BYTES = 1_000_000
FX_TTL_S = 300
FX_RETRY_S = 60


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _positive(value) -> float | None:
    result = number(value)
    return result if result is not None and result > 0 else None


def current_equity(dry_run: bool, reader, ft) -> float | None:
    """Live: Bitget USDT-futures equity incl. unrealized PnL. Dry: Freqtrade /balance total."""
    if dry_run:
        balance = ft.balance()
        value = number(balance.get("total")) if isinstance(balance, dict) else None
    else:
        value = number(reader.fetch_usdt_equity())
    return value if value is not None and value >= 0 else None


class EquityLog:
    """Append-only JSON lines {ts, mode, equity_usdt}; one series per mode."""

    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def due(self, mode: str) -> bool:
        return self.clock() >= self._next.get(mode, float("-inf"))

    def maybe_record(self, mode: str, read: Callable[[], float | None]) -> bool:
        """Record one snapshot when due. Never raises; returns whether a line was written."""
        if mode not in {"dry", "live"} or not self.due(mode):
            return False
        now = self.clock()
        # Claim the slot first so concurrent polls do not read the exchange twice.
        self._next[mode] = now + SNAPSHOT_RETRY_S
        try:
            value = read()
        except Exception:
            value = None
        if value is None or not math.isfinite(value) or value < 0:
            return False
        line = json.dumps({"ts": int(now), "mode": mode, "equity_usdt": value}) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as log:
                    log.write(line)
                if self.path.stat().st_size > PRUNE_BYTES:
                    self._prune(now)
        except Exception:
            return False
        self._next[mode] = now + SNAPSHOT_INTERVAL_S
        return True

    def _rows(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return []
        rows = []
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(row, dict) or row.get("mode") not in {"dry", "live"}:
                continue
            ts, value = row.get("ts"), number(row.get("equity_usdt"))
            if type(ts) is int and value is not None and value >= 0:
                rows.append({"ts": ts, "mode": row["mode"], "equity_usdt": value})
        return rows

    def _prune(self, now: float) -> None:
        cutoff = now - HISTORY_DAYS * 86400
        keep = [row for row in self._rows() if row["ts"] >= cutoff]
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.writelines(json.dumps(row) + "\n" for row in keep)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def history(self, mode: str) -> list[list[float]]:
        """[[ts seconds, equity_usdt], ...] for the last 30 days, oldest first, thinned for drawing."""
        cutoff = self.clock() - HISTORY_DAYS * 86400
        with self._lock:
            rows = self._rows()
        points = sorted([row["ts"], row["equity_usdt"]] for row in rows
                        if row["mode"] == mode and row["ts"] >= cutoff)
        if len(points) > MAX_POINTS:
            step = math.ceil(len(points) / MAX_POINTS)
            points = points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
        return points


class FxCache:
    """Upbit KRW per USDT, cached for 5 minutes; None when unavailable."""

    def __init__(self, fetch: Callable[[], tuple[float, str]], clock: Callable[[], float]):
        self.fetch = fetch
        self.clock = clock
        self._lock = threading.Lock()
        self._value: float | None = None
        self._fetched_at = float("-inf")
        self._until = float("-inf")

    def rate(self) -> float | None:
        with self._lock:
            now = self.clock()
            if now < self._until:
                return self._value
            try:
                value = _positive(self.fetch()[0])
            except Exception:
                value = None
            if value is None:
                # Keep a recent good rate rather than hiding KRW on a brief Upbit hiccup.
                self._until = now + FX_RETRY_S
                if now - self._fetched_at > FX_TTL_S * 3:
                    self._value = None
            else:
                self._value, self._fetched_at, self._until = value, now, now + FX_TTL_S
            return self._value


def asset_payload(mode: str, equity: float | None, history: list[list[float]], krw: float | None) -> dict:
    # Change is measured from the first recorded point to now (or to the last point if now is unknown).
    series = [value for _, value in history] + ([equity] if equity is not None else [])
    first = series[0] if history else None
    change = series[-1] - first if first is not None and len(series) > 1 else None
    return {
        "mode": mode, "equity_usdt": equity, "krw_per_usdt": krw,
        "equity_krw": equity * krw if equity is not None and krw is not None else None,
        "history": history, "since": history[0][0] if history else None,
        "change_usdt": change,
        "change_pct": change / first * 100 if change is not None and first else None,
    }


def _closes(candles) -> list[list[float]]:
    if not isinstance(candles, dict):
        raise ValueError("candles")
    columns, data = candles.get("columns"), candles.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise ValueError("candles")
    try:
        ts_index, close_index = columns.index("__date_ts"), columns.index("close")
    except ValueError:
        raise ValueError("candles") from None
    points = []
    for row in data[-BTC_CANDLES:]:
        if not isinstance(row, list) or len(row) <= max(ts_index, close_index):
            continue
        ts, close = row[ts_index], _positive(row[close_index])
        if type(ts) is int and ts > 0 and close is not None:
            points.append([ts, close])
    return points


def btc_payload(ft) -> dict:
    """72 x 1h closes from the running bot, plus the open strategy trade's entry/stop and entries."""
    closes = _closes(ft.pair_candles(BTC_PAIR, BTC_TIMEFRAME, BTC_CANDLES))
    start = closes[0][0] if closes else None
    end = closes[-1][0] + HOUR_MS if closes else None
    entry = stop = None
    entries = []

    def in_window(ts) -> bool:
        return type(ts) is int and start is not None and start <= ts <= end

    open_trades = ft.status()
    for row in open_trades if isinstance(open_trades, list) else []:
        if not isinstance(row, dict) or row.get("pair") != BTC_PAIR or not is_strategy_tag(row.get("enter_tag")):
            continue
        if entry is None:
            entry, stop = _positive(row.get("open_rate")), _positive(row.get("stop_loss_abs"))
        if in_window(row.get("open_timestamp")):
            entries.append(row["open_timestamp"])
    history = ft.trades(TRADE_PAGE)
    rows = history.get("trades") if isinstance(history, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if (isinstance(row, dict) and row.get("pair") == BTC_PAIR and is_strategy_tag(row.get("enter_tag"))
                and in_window(row.get("open_timestamp"))):
            entries.append(row["open_timestamp"])
    return {"pair": "BTCUSDT", "timeframe": BTC_TIMEFRAME, "closes": closes,
            "entry": entry, "stop": stop, "entries": sorted(set(entries))}
