import pytest

from bitgetbot.postmortem import market

H = 3600_000
OPEN_TS = 1792584000000  # 2026-10-21T12:00:00Z, an hour boundary
CLOSE_TS = OPEN_TS + 5 * H + 30 * 60_000
NOW = CLOSE_TS + 40 * 60_000  # the candle that started at CLOSE_TS - 30min is not complete yet? see test


class Exchange:
    def __init__(self, closes, start, funding_error=False, oi_error=False):
        self.closes, self.start = closes, start
        self.funding_error, self.oi_error = funding_error, oi_error
        self.calls = []

    def publicMixGetV2MixMarketCandles(self, params):
        self.calls.append(("candles", dict(params)))
        assert params["symbol"] == "BTCUSDT" and params["productType"] == "USDT-FUTURES"
        assert params["granularity"] == "1H"
        lo, hi = int(params["startTime"]), int(params["endTime"])
        rows = []
        prev = self.closes[0] if self.closes else 0
        for i, close in enumerate(self.closes):
            ts = self.start + i * H
            if lo <= ts <= hi:
                rows.append([str(ts), str(prev), str(max(prev, close) + 10), str(min(prev, close) - 10),
                             str(close), "1", "1"])
            prev = close
        return {"code": "00000", "data": rows}

    def publicMixGetV2MixMarketHistoryFundRate(self, params):
        if self.funding_error:
            raise RuntimeError("down")
        return {"code": "00000", "data": [
            {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": str(OPEN_TS + 4 * H)},
            {"symbol": "BTCUSDT", "fundingRate": "-0.0002", "fundingTime": str(OPEN_TS - 4 * H)},
            {"symbol": "BTCUSDT", "fundingRate": "0.0003", "fundingTime": str(OPEN_TS - 30 * H)},
        ]}

    def publicMixGetV2MixMarketOpenInterest(self, params):
        if self.oi_error:
            raise RuntimeError("down")
        return {"code": "00000", "data": {"openInterestList": [{"symbol": "BTCUSDT", "size": "35587.37"}],
                                          "ts": str(NOW)}}


def flat_closes(start, end, value=100.0):
    return [value] * ((end - start) // H)


def scenario():
    start = OPEN_TS - 7 * 24 * H
    closes = []
    # quiet baseline: alternate +-0.1%
    for i in range((OPEN_TS - start) // H):
        closes.append(100.0 * (1.001 if i % 2 else 1.0))
    # hold: one big +5% hour at OPEN_TS + 2h, otherwise quiet
    level = closes[-1]
    for i in range(8):
        if i == 2:
            level *= 1.05
        else:
            level *= 1.001 if i % 2 else 0.999
        closes.append(level)
    return start, closes


def test_fetch_market_summary_and_big_moves():
    start, closes = scenario()
    exchange = Exchange(closes, start)
    now = OPEN_TS + 8 * H  # candles up to OPEN_TS + 7h are complete
    result = market.fetch_market(exchange, "BTCUSDT", OPEN_TS, CLOSE_TS, now)
    assert result["status"] == "ok" and result["source"] == "bitget_public"
    assert result["timeframe"] == "1h" and result["symbol"] == "BTCUSDT"
    assert result["candles"][0][0] == start and result["candles"][-1][0] == OPEN_TS + 7 * H
    summary = result["summary"]
    assert summary["max_hold_absret_1h_pct"] == pytest.approx(5.0, rel=1e-6)
    assert summary["baseline_absret_1h_pct"] == pytest.approx(0.1, rel=0.02)
    assert summary["vol_ratio_hold"] > 3
    assert summary["shock_during_hold"] is True
    top = result["big_moves"][0]
    assert top["ts_utc"] == "2026-10-21T14:00:00Z" and top["ret_pct"] == pytest.approx(5.0, rel=1e-6)
    assert len(result["big_moves"]) == 3
    assert [f["rate"] for f in result["funding"]] == [-0.0002, 0.0001]  # in window, oldest first
    assert result["open_interest"]["size"] == pytest.approx(35587.37)
    assert "리포트 시점" in result["open_interest"]["note"]


def test_incomplete_candle_dropped():
    start, closes = scenario()
    now = OPEN_TS + 7 * H + 30 * 60_000  # the OPEN_TS+7h candle is still forming
    result = market.fetch_market(Exchange(closes, start), "BTCUSDT", OPEN_TS, CLOSE_TS, now)
    assert result["candles"][-1][0] == OPEN_TS + 6 * H


def test_long_window_is_fetched_in_chunks():
    start = OPEN_TS - 7 * 24 * H
    closes = flat_closes(start, OPEN_TS + 60 * 24 * H)
    exchange = Exchange(closes, start)
    result = market.fetch_market(exchange, "BTCUSDT", OPEN_TS, OPEN_TS + 50 * 24 * H, OPEN_TS + 55 * 24 * H)
    assert len([c for c in exchange.calls if c[0] == "candles"]) >= 2
    stamps = [c[0] for c in result["candles"]]
    assert stamps == sorted(set(stamps)) and stamps[-1] == OPEN_TS + 55 * 24 * H - H


def test_funding_and_oi_failures_are_null():
    start, closes = scenario()
    result = market.fetch_market(Exchange(closes, start, funding_error=True, oi_error=True),
                                 "BTCUSDT", OPEN_TS, CLOSE_TS, OPEN_TS + 8 * H)
    assert result["funding"] is None and result["open_interest"] is None
    assert result["status"] == "ok"


def test_candle_failure_raises():
    class Broken(Exchange):
        def publicMixGetV2MixMarketCandles(self, params):
            return {"code": "40001", "data": None}
    with pytest.raises(market.MarketError):
        market.fetch_market(Broken([], 0), "BTCUSDT", OPEN_TS, CLOSE_TS, NOW)
    with pytest.raises(market.MarketError):
        market.fetch_market(Exchange([], OPEN_TS), "BTCUSDT", OPEN_TS, CLOSE_TS, NOW)  # no candles


def test_event_reaction_needs_six_hours_after():
    start, closes = scenario()
    result = market.fetch_market(Exchange(closes, start), "BTCUSDT", OPEN_TS, CLOSE_TS, OPEN_TS + 8 * H)
    candles, baseline = result["candles"], result["summary"]["baseline_absret_6h_pct"]
    reaction = market.event_reaction(candles, OPEN_TS + 2 * H + 15 * 60_000, baseline)
    assert reaction["absret_6h_pct"] == pytest.approx(5.0, abs=0.5)
    assert reaction["vol_ratio_6h"] > 3
    assert reaction["pre_2h_absret_pct"] < 0.5
    assert market.event_reaction(candles, OPEN_TS + 5 * H, baseline) is None  # only 3h after
    assert market.event_reaction(candles, start - H, baseline) is None
    assert market.event_reaction(candles, OPEN_TS, None)["vol_ratio_6h"] is None
