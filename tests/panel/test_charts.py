"""Chart helpers: equity history file, KRW cache and BTC candles. No network, no Docker."""

import json

import pytest

from bitgetbot.panel import charts

SECRET = "SECRET-SENTINEL"
HOUR = 3_600_000


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def log(tmp_path):
    clock = Clock()
    return charts.EquityLog(tmp_path / ".panel" / "equity.jsonl", clock), clock


def test_equity_log_records_at_most_every_ten_minutes_per_mode(log):
    equity, clock = log
    assert equity.maybe_record("dry", lambda: 100.0) is True
    assert equity.maybe_record("dry", lambda: pytest.fail("not due")) is False
    assert equity.maybe_record("live", lambda: 50.0) is True
    clock.now += 599
    assert equity.maybe_record("dry", lambda: 101.0) is False
    clock.now += 1
    assert equity.maybe_record("dry", lambda: 102.0) is True
    assert equity.history("dry") == [[1_000_000, 100.0], [1_000_600, 102.0]]
    assert equity.history("live") == [[1_000_000, 50.0]]
    assert equity.maybe_record(SECRET, lambda: 1.0) is False


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1.0])
def test_failed_or_invalid_read_retries_after_a_minute(log, value):
    equity, clock = log

    def read():
        if value is None:
            raise RuntimeError(SECRET)
        return value

    assert equity.maybe_record("live", read) is False
    assert not equity.path.exists()
    clock.now += 59
    assert equity.maybe_record("live", lambda: pytest.fail("retry waits")) is False
    clock.now += 1
    assert equity.maybe_record("live", lambda: 10.0) is True


def test_history_skips_garbage_old_and_other_mode_lines(log):
    equity, clock = log
    equity.path.parent.mkdir(parents=True)
    lines = [
        "not json " + SECRET, "[]", json.dumps({"ts": "x", "mode": "dry", "equity_usdt": 1}),
        json.dumps({"ts": 5, "mode": SECRET, "equity_usdt": 1}),
        json.dumps({"ts": int(clock.now - 31 * 86400), "mode": "dry", "equity_usdt": 1}),
        json.dumps({"ts": int(clock.now - 10), "mode": "dry", "equity_usdt": SECRET}),
        json.dumps({"ts": int(clock.now - 20), "mode": "dry", "equity_usdt": 7.5, "extra": SECRET}),
        json.dumps({"ts": int(clock.now - 30), "mode": "live", "equity_usdt": 9}),
    ]
    equity.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert equity.history("dry") == [[int(clock.now - 20), 7.5]]
    assert SECRET not in repr(equity.history("dry"))


def test_history_is_thinned_but_keeps_first_and_last(log):
    equity, clock = log
    equity.path.parent.mkdir(parents=True)
    rows = [json.dumps({"ts": int(clock.now) - index, "mode": "dry", "equity_usdt": index}) for index in range(2000)]
    equity.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    points = equity.history("dry")
    assert len(points) <= charts.MAX_POINTS + 1
    assert points[0] == [int(clock.now) - 1999, 1999.0]
    assert points[-1] == [int(clock.now), 0.0]
    assert points == sorted(points)


def test_large_file_is_pruned_to_thirty_days(log, monkeypatch):
    equity, clock = log
    monkeypatch.setattr(charts, "PRUNE_BYTES", 200)
    equity.path.parent.mkdir(parents=True)
    old = [json.dumps({"ts": int(clock.now - 40 * 86400), "mode": "dry", "equity_usdt": 1})] * 5
    equity.path.write_text("\n".join(old) + "\n", encoding="utf-8")
    assert equity.maybe_record("dry", lambda: 3.0)
    rows = [json.loads(line) for line in equity.path.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"ts": int(clock.now), "mode": "dry", "equity_usdt": 3.0}]
    assert list(equity.path.parent.iterdir()) == [equity.path]


def test_prune_failure_still_waits_the_full_interval(log, monkeypatch):
    equity, clock = log
    monkeypatch.setattr(charts, "PRUNE_BYTES", 0)

    def broken(now):
        raise OSError(SECRET)

    monkeypatch.setattr(equity, "_prune", broken)
    assert equity.maybe_record("dry", lambda: 1.0) is True
    clock.now += charts.SNAPSHOT_RETRY_S
    assert equity.maybe_record("dry", lambda: pytest.fail("snapshot repeated after a prune failure")) is False
    clock.now += charts.SNAPSHOT_INTERVAL_S
    assert equity.maybe_record("dry", lambda: 2.0) is True
    assert len(equity.path.read_text(encoding="utf-8").splitlines()) == 2


def test_concurrent_polls_read_and_write_once(log):
    import threading

    equity, _ = log
    inside, release, reads = threading.Event(), threading.Event(), []

    def slow_read():
        reads.append(1)
        inside.set()
        assert release.wait(5)
        return 1.0

    results = []
    first = threading.Thread(target=lambda: results.append(equity.maybe_record("dry", slow_read)))
    first.start()
    assert inside.wait(5)
    # Another poll arrives while the first read is still in flight.
    results.append(equity.maybe_record("dry", slow_read))
    release.set()
    first.join(5)
    assert sorted(results) == [False, True] and len(reads) == 1
    assert len(equity.path.read_text(encoding="utf-8").splitlines()) == 1


def test_write_failure_is_silent(tmp_path):
    clock = Clock()
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    equity = charts.EquityLog(blocker / "equity.jsonl", clock)
    assert equity.maybe_record("dry", lambda: 1.0) is False
    assert equity.history("dry") == []


def test_fx_cache_five_minutes_and_hides_stale_rate():
    clock, calls = Clock(), []
    answers = [(1400.0, "t"), RuntimeError(SECRET), RuntimeError(SECRET), (0, "t")]

    def fetch():
        calls.append(clock.now)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    fx = charts.FxCache(fetch, clock)
    assert fx.rate() == 1400.0
    clock.now += 299
    assert fx.rate() == 1400.0 and len(calls) == 1
    clock.now += 1
    assert fx.rate() == 1400.0             # brief Upbit failure keeps the recent rate
    clock.now += 60
    assert fx.rate() == 1400.0 and len(calls) == 3
    clock.now += 900
    assert fx.rate() is None               # invalid rate and the last good one is too old
    assert len(calls) == 4


@pytest.mark.parametrize("dry,balance,equity,want", [
    (True, {"total": 105.5}, None, 105.5),
    (True, {"total": SECRET}, None, None),
    (True, [SECRET], None, None),
    (True, {"total": -1}, None, None),
    (False, None, 114.25, 114.25),
    (False, None, float("nan"), None),
])
def test_current_equity_by_mode(dry, balance, equity, want):
    class FT:
        def balance(self):
            return balance

    class Reader:
        def fetch_usdt_equity(self):
            if dry:
                pytest.fail("dry mode never reads the exchange account")
            return equity

    assert charts.current_equity(dry, Reader(), FT()) == want


def test_asset_payload_change_from_first_point():
    payload = charts.asset_payload("live", 110.0, [[1, 100.0], [2, 105.0]], 1400.0)
    assert payload == {"mode": "live", "equity_usdt": 110.0, "krw_per_usdt": 1400.0,
                       "equity_krw": 154000.0, "history": [[1, 100.0], [2, 105.0]], "since": 1,
                       "change_usdt": 10.0, "change_pct": 10.0}
    assert charts.asset_payload("dry", None, [[1, 100.0], [2, 90.0]], None)["change_usdt"] == -10.0
    empty = charts.asset_payload("dry", 50.0, [], 1400.0)
    assert empty["change_usdt"] is None and empty["since"] is None and empty["equity_krw"] == 70000.0
    assert charts.asset_payload("dry", None, [[1, 100.0]], None)["change_usdt"] is None
    assert charts.asset_payload("dry", 1.0, [[1, 0.0]], None)["change_pct"] is None


class CandleFT:
    def __init__(self, candles, status=None, trades=None):
        self.candles, self.open, self.history = candles, status or [], trades or {"trades": []}

    def pair_candles(self, pair, timeframe, limit):
        assert (pair, timeframe, limit) == ("BTC/USDT:USDT", "1h", 72)
        return self.candles

    def status(self):
        return self.open

    def trades(self, limit=500):
        return self.history


@pytest.mark.parametrize("candles", [
    None, [SECRET], {"columns": SECRET, "data": []}, {"columns": ["date"], "data": []},
])
def test_malformed_candles_raise_for_the_caller_to_hide(candles):
    with pytest.raises(ValueError):
        charts.btc_payload(CandleFT(candles))


def test_candle_rows_are_allowlisted():
    candles = {"columns": ["__date_ts", "close"], "data": [
        [HOUR, 60000], [2 * HOUR, SECRET], [3 * HOUR, -1], [True, 1], "row", [4 * HOUR],
        [5 * HOUR, "61000.5"],
    ]}
    payload = charts.btc_payload(CandleFT(candles, status=SECRET, trades=[SECRET]))
    assert payload == {"pair": "BTCUSDT", "timeframe": "1h", "closes": [[HOUR, 60000.0], [5 * HOUR, 61000.5]],
                       "entry": None, "stop": None, "entries": []}


def test_open_strategy_trade_gives_entry_stop_and_marker():
    candles = {"columns": ["__date_ts", "close"], "data": [[HOUR * index, 100 + index] for index in range(1, 11)]}
    status = [
        {"pair": "ETH/USDT:USDT", "enter_tag": "safe_long", "open_rate": 1, "stop_loss_abs": 1, "open_timestamp": 2 * HOUR},
        {"pair": "BTC/USDT:USDT", "enter_tag": "safe_long", "open_rate": "105.5", "stop_loss_abs": SECRET,
         "open_timestamp": 5 * HOUR},
    ]
    trades = {"trades": [{"pair": "BTC/USDT:USDT", "enter_tag": "safe_long", "open_timestamp": 5 * HOUR},
                         {"pair": "BTC/USDT:USDT", "enter_tag": "safe_long", "open_timestamp": 11 * HOUR},
                         {"pair": "BTC/USDT:USDT", "enter_tag": "safe_long", "open_timestamp": 13 * HOUR},
                         SECRET]}
    payload = charts.btc_payload(CandleFT(candles, status, trades))
    assert payload["entry"] == 105.5 and payload["stop"] is None
    assert payload["entries"] == [5 * HOUR, 11 * HOUR]


def test_entry_just_after_the_newest_candle_closes_still_gets_a_marker():
    # The strategy enters right after the last analysed candle closes: about last + 1h + seconds.
    candles = {"columns": ["__date_ts", "close"], "data": [[HOUR * index, 100 + index] for index in range(1, 11)]}
    now_entry = 10 * HOUR + HOUR + 5000
    status = [{"pair": "BTC/USDT:USDT", "enter_tag": "safe_long", "open_rate": 110, "stop_loss_abs": 100,
               "open_timestamp": now_entry}]
    trades = {"trades": [{"pair": "BTC/USDT:USDT", "enter_tag": "safe_long", "open_timestamp": 12 * HOUR + 1}]}
    payload = charts.btc_payload(CandleFT(candles, status, trades))
    assert payload["entries"] == [now_entry]
