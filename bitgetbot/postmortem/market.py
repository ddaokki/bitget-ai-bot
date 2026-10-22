"""BTC (trade symbol) price path around a trade from Bitget's public futures API. No keys, read-only."""

from statistics import fmean

import ccxt

from bitgetbot.postmortem.facts import iso_utc, number

H = 3600_000
BASELINE_MS = 7 * 24 * H  # quiet reference before entry
WINDOW_BEFORE_MS = 24 * H  # what "the trade window" means for big moves and funding
CHUNK_CANDLES = 999  # Bitget returns at most 1000 candles per request
PRODUCT = "USDT-FUTURES"
SHOCK_RATIO = 3.0
SHOCK_MIN_PCT = 1.0
REACTION_HOURS = 6
PRE_HOURS = 2


class MarketError(Exception):
    """Public candles could not be read; the report retries the market part later."""


def public_exchange():
    # Public market data only: no API key, secret or passphrase ever reaches this client.
    return ccxt.bitget({"enableRateLimit": True, "timeout": 15000})


def _ok(response) -> bool:
    return isinstance(response, dict) and response.get("code") == "00000"


def _candles(exchange, symbol: str, start_ms: int, end_ms: int) -> list[list]:
    """Completed 1h candles [ts, open, high, low, close] with start in [start_ms, end_ms]."""
    by_ts = {}
    cursor = start_ms
    while cursor <= end_ms:
        chunk_end = min(end_ms, cursor + CHUNK_CANDLES * H)
        response = exchange.publicMixGetV2MixMarketCandles({
            "symbol": symbol, "productType": PRODUCT, "granularity": "1H",
            "startTime": str(cursor), "endTime": str(chunk_end), "limit": "1000",
        })
        if not _ok(response) or not isinstance(response.get("data"), list):
            raise MarketError("candles")
        for row in response["data"]:
            if not isinstance(row, list) or len(row) < 5:
                continue
            values = [number(value) for value in row[:5]]
            if any(value is None for value in values):
                continue
            ts = int(values[0])
            if start_ms <= ts <= end_ms:
                by_ts[ts] = [ts, *values[1:5]]
        cursor = chunk_end + H
    return [by_ts[ts] for ts in sorted(by_ts)]


def _returns(candles: list[list]) -> list[tuple[int, float]]:
    """(candle start, close-to-close % return) for every candle that has a previous one."""
    return [(cur[0], (cur[4] / prev[4] - 1) * 100) for prev, cur in zip(candles, candles[1:]) if prev[4] > 0]


def _absret_6h(candles: list[list]) -> list[float]:
    span = REACTION_HOURS
    return [abs(candles[i + span - 1][4] / candles[i][1] - 1) * 100
            for i in range(len(candles) - span + 1) if candles[i][1] > 0]


def _mean(values) -> float | None:
    values = list(values)
    return fmean(values) if values else None


def _funding(exchange, symbol: str, start_ms: int, end_ms: int) -> list[dict] | None:
    try:
        response = exchange.publicMixGetV2MixMarketHistoryFundRate({
            "symbol": symbol, "productType": PRODUCT, "pageSize": "100", "pageNo": "1",
        })
        rows = response.get("data") if _ok(response) else None
        if not isinstance(rows, list):
            return None
        found = []
        for row in rows:
            at, rate = number(row.get("fundingTime")), number(row.get("fundingRate"))
            if at is not None and rate is not None and start_ms <= at <= end_ms:
                found.append({"ts_utc": iso_utc(int(at)), "rate": rate})
        return sorted(found, key=lambda item: item["ts_utc"])
    except Exception:
        return None


def _open_interest(exchange, symbol: str) -> dict | None:
    try:
        response = exchange.publicMixGetV2MixMarketOpenInterest({"symbol": symbol, "productType": PRODUCT})
        data = response.get("data") if _ok(response) else None
        rows = data.get("openInterestList") if isinstance(data, dict) else None
        size = number(rows[0].get("size")) if isinstance(rows, list) and rows else None
        at = number(data.get("ts"))
        if size is None or at is None:
            return None
        # Bitget's free API has no open-interest history, so this is the value when the report ran.
        return {"at_utc": iso_utc(int(at)), "size": size, "unit": "contracts (BTC)",
                "note": "리포트 시점 값(청산 시점 아님)"}
    except Exception:
        return None


def fetch_market(exchange, symbol: str, open_ts: int, close_ts: int, now_ms: int) -> dict:
    start = (open_ts - BASELINE_MS) // H * H
    last_complete = (now_ms // H) * H - H  # the newest candle that has fully closed
    candles = _candles(exchange, symbol, start, last_complete)
    if len(candles) < 2:
        raise MarketError("no candles")
    returns = _returns(candles)
    baseline = [abs(r) for ts, r in returns if ts + H <= open_ts]
    hold = [abs(r) for ts, r in returns if open_ts - H < ts < close_ts]
    window_start = open_ts - WINDOW_BEFORE_MS
    window = [c for c in candles if c[0] >= window_start - H]
    hold_candles = [c for c in candles if open_ts - H < c[0] < close_ts]
    base_mean, hold_mean = _mean(baseline), _mean(hold)
    max_hold = max(hold) if hold else None
    moves = sorted(((ts, r) for ts, r in returns if ts >= window_start), key=lambda item: -abs(item[1]))[:3]
    return {
        "status": "ok", "source": "bitget_public", "symbol": symbol, "timeframe": "1h",
        "window": {"from_utc": iso_utc(candles[0][0]), "to_utc": iso_utc(candles[-1][0] + H)},
        "candle_columns": ["ts", "open", "high", "low", "close"],
        "candles": candles,
        "summary": {
            "window_high": max(c[2] for c in window) if window else None,
            "window_low": min(c[3] for c in window) if window else None,
            "hold_high": max(c[2] for c in hold_candles) if hold_candles else None,
            "hold_low": min(c[3] for c in hold_candles) if hold_candles else None,
            "baseline_absret_1h_pct": base_mean,
            "baseline_absret_6h_pct": _mean(_absret_6h([c for c in candles if c[0] + H <= open_ts])),
            "hold_absret_1h_pct": hold_mean,
            "vol_ratio_hold": hold_mean / base_mean if hold_mean is not None and base_mean else None,
            "max_hold_absret_1h_pct": max_hold,
            "shock_during_hold": bool(max_hold is not None and base_mean
                                      and max_hold >= SHOCK_RATIO * base_mean and max_hold >= SHOCK_MIN_PCT),
        },
        "big_moves": [{"ts_utc": iso_utc(ts), "ts": ts, "ret_pct": r} for ts, r in moves],
        "funding": _funding(exchange, symbol, window_start, now_ms),
        "open_interest": _open_interest(exchange, symbol),
    }


def event_reaction(candles: list[list], ts_ms: int, baseline_6h: float | None) -> dict | None:
    """Spec §18: absolute 0-6h move after an event vs normal 6h moves, and the 2h before it."""
    index = next((i for i, c in enumerate(candles) if c[0] <= ts_ms < c[0] + H), None)
    if index is None or index + REACTION_HOURS > len(candles) or index < PRE_HOURS:
        return None
    start = candles[index][1]
    if start <= 0 or candles[index - PRE_HOURS][1] <= 0:
        return None
    after = abs(candles[index + REACTION_HOURS - 1][4] / start - 1) * 100
    before = abs(start / candles[index - PRE_HOURS][1] - 1) * 100
    return {
        "absret_6h_pct": after,
        "vol_ratio_6h": after / baseline_6h if baseline_6h else None,
        "pre_2h_absret_pct": before,
    }
