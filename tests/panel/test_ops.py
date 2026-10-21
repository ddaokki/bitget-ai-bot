import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bitgetbot.models import PlanOrder, PositionSnapshot
from bitgetbot.privcfg import Settings


SECRET = "SECRET-SENTINEL"
ERROR = "upstream exception must never escape"


@pytest.fixture
def ops(monkeypatch, tmp_path):
    from bitgetbot.panel import ops

    def forbidden(*args, **kwargs):
        pytest.fail("Tests must not run real commands")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(ops, "TAILSCALE", "tailscale")
    monkeypatch.setattr(ops, "STATE_PATH", tmp_path / "smoke.json")
    return ops


class Runner:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, argv, timeout):
        assert type(timeout) is int and timeout > 0
        self.calls.append(list(argv))
        expected, response = self.steps.pop(0)
        assert argv == expected
        if isinstance(response, Exception):
            raise response
        return response

    def done(self):
        assert not self.steps


@pytest.mark.parametrize("name,argv", [
    ("docker_running", ["docker", "info"]),
    ("compose_up", ["docker", "compose", "-f", "freqtrade/docker-compose.yml", "up", "-d"]),
    ("compose_restart", ["docker", "compose", "-f", "freqtrade/docker-compose.yml", "restart", "freqtrade"]),
])
@pytest.mark.parametrize("code", [0, 1, 124])
def test_docker_commands_use_fixed_argv_and_exit_code(ops, name, argv, code):
    runner = Runner([(argv, (code, SECRET))])
    assert getattr(ops, name)(run=runner) is (code == 0)
    runner.done()


@pytest.mark.parametrize("output,want", [
    ('[{"Service":"freqtrade","State":"running"}]', True),
    ('{"Service":"freqtrade","State":"running"}', True),
    ('{"Service":"other","State":"running"}\n{"Service":"freqtrade","State":"running"}', True),
    ('[{"Service":"freqtrade","State":"exited"}]', False),
    ('[{"Service":"other","State":"running"}]', False),
    ('[{"Name":"freqtrade","State":"running"}]', False),
    ('[]', False), ('null', False), ('[null, 1]', False),
    ('not json ' + SECRET, False),
])
def test_container_status_parses_compose_json(ops, output, want):
    argv = ["docker", "compose", "-f", "freqtrade/docker-compose.yml", "ps", "--format", "json"]
    runner = Runner([(argv, (0, output))])
    assert ops.freqtrade_container_running(run=runner) is want
    runner.done()


def test_container_status_rejects_nonzero_exit(ops):
    argv = ["docker", "compose", "-f", "freqtrade/docker-compose.yml", "ps", "--format", "json"]
    assert not ops.freqtrade_container_running(Runner([(argv, (1, '{"Service":"freqtrade","State":"running"}'))]))


@pytest.mark.parametrize("error,code", [(subprocess.TimeoutExpired("unused", 3), 124), (OSError(SECRET), 127)])
def test_default_runner_hides_process_failures(ops, monkeypatch, error, code):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert ops.default_runner(["unused"], 3) == (code, "")


def test_default_runner_uses_repo_root_utf8_and_timeout(ops, monkeypatch):
    def run(argv, **kwargs):
        assert argv == ["unused", "argument with spaces"]
        assert kwargs == {
            "cwd": Path(__file__).resolve().parents[2], "capture_output": True,
            "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 7,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        return SimpleNamespace(returncode=2, stdout="output", stderr=SECRET)

    monkeypatch.setattr(subprocess, "run", run)
    assert ops.default_runner(["unused", "argument with spaces"], 7) == (2, "output")


@pytest.mark.parametrize("on_path,fallback,want", [
    ("custom-tailscale", False, "custom-tailscale"),
    (None, True, r"C:\Program Files\Tailscale\tailscale.exe"),
    (None, False, None),
])
def test_tailscale_resolution_uses_path_then_windows_fallback(ops, monkeypatch, on_path, fallback, want):
    monkeypatch.setattr(ops.shutil, "which", lambda name: on_path if name == "tailscale" else None)
    monkeypatch.setattr(Path, "is_file", lambda path: fallback)
    assert ops._resolve_tailscale() == want


def ts_steps(status=None, serve=None, funnel=None):
    return [
        (["tailscale", "status", "--json"], (0, json.dumps(status if status is not None else {
            "BackendState": "Running", "Self": {"DNSName": "pc.tail1234.ts.net."},
        }))),
        (["tailscale", "serve", "status", "--json"], (0, json.dumps(serve if serve is not None else {"Web": {"pc:443": {}}}))),
        (["tailscale", "funnel", "status", "--json"], (0, json.dumps(funnel if funnel is not None else {}))),
    ]


def test_tailscale_status_returns_only_safe_fields(ops):
    runner = Runner(ts_steps(funnel={"AllowFunnel": {"pc:443": True}, "secret": SECRET}))
    assert ops.tailscale_status(runner) == {
        "installed": True, "logged_in": True, "dns_name": "pc.tail1234.ts.net",
        "serving": True, "funnel": True,
    }
    runner.done()


@pytest.mark.parametrize("status,serve,funnel,want_funnel", [
    ({}, {}, {}, False),
    ({"BackendState": "NeedsLogin", "Self": None}, {"Web": {}}, {"AllowFunnel": {"pc:443": False}}, False),
    ([], [], [], None),
    ({"BackendState": True, "Self": {"DNSName": SECRET}}, {"TCP": None}, {"AllowFunnel": {"pc:443": "true"}}, None),
])
def test_tailscale_status_handles_missing_and_malformed_fields(ops, status, serve, funnel, want_funnel):
    result = ops.tailscale_status(Runner(ts_steps(status, serve, funnel)))
    assert result == {"installed": True, "logged_in": False, "dns_name": None, "serving": False, "funnel": want_funnel}


@pytest.mark.parametrize("payload", ["not json " + SECRET, "null", "true"])
def test_tailscale_status_hides_command_output(ops, payload):
    runner = Runner([(argv, (0, payload)) for argv, _ in ts_steps()])
    assert ops.tailscale_status(runner) == {
        "installed": True, "logged_in": False, "dns_name": None, "serving": False, "funnel": None,
    }


def test_tailscale_status_ignores_failed_command_json(ops):
    runner = Runner([(argv, (1, output)) for argv, (_, output) in ts_steps()])
    assert ops.tailscale_status(runner) == {
        "installed": True, "logged_in": False, "dns_name": None, "serving": False, "funnel": None,
    }


def test_tailscale_status_detects_tcp_and_nested_funnel(ops):
    result = ops.tailscale_status(Runner(ts_steps(serve={"TCP": {"443": {}}}, funnel={
        "Services": {"svc:panel": {"AllowFunnel": {"pc:443": True}}},
    })))
    assert result["serving"] is True
    assert result["funnel"] is True


def test_tailscale_missing_runs_nothing(ops, monkeypatch):
    monkeypatch.setattr(ops, "TAILSCALE", None)
    runner = Runner([])
    assert ops.tailscale_status(runner) == {
        "installed": False, "logged_in": False, "dns_name": None, "serving": False, "funnel": False,
    }
    assert ops.enable_phone_access(runner)[0] is False
    assert runner.calls == []


def test_phone_access_refuses_funnel_without_serving(ops):
    runner = Runner(ts_steps(funnel={"AllowFunnel": {"pc:443": True}}))
    ok, message = ops.enable_phone_access(runner)
    assert ok is False
    assert "Funnel" in message
    assert SECRET not in message
    runner.done()


def test_phone_access_serves_8090_and_verifies_url(ops):
    runner = Runner(ts_steps(serve={}) + [(["tailscale", "serve", "--bg", "8090"], (0, SECRET))] + ts_steps())
    ok, message = ops.enable_phone_access(runner)
    assert ok is True
    assert "https://pc.tail1234.ts.net" in message
    assert "ts.net." not in message
    assert SECRET not in message
    runner.done()


@pytest.mark.parametrize("after", [
    ts_steps(serve={}),
    ts_steps(funnel={"AllowFunnel": {"pc:443": True}}),
    ts_steps(status={"BackendState": "Running", "Self": {"DNSName": "https://evil.invalid/"}}),
])
def test_phone_access_requires_safe_recheck(ops, after):
    runner = Runner(ts_steps(serve={}) + [(["tailscale", "serve", "--bg", "8090"], (0, SECRET))]
                    + after + [(["tailscale", "serve", "reset"], (0, SECRET))])
    ok, message = ops.enable_phone_access(runner)
    assert ok is False
    assert SECRET not in message
    assert "evil.invalid" not in message
    runner.done()


def test_phone_access_hides_serve_failure(ops):
    runner = Runner(ts_steps(serve={}) + [
        (["tailscale", "serve", "--bg", "8090"], (1, SECRET + ERROR)),
        (["tailscale", "serve", "reset"], (0, SECRET)),
    ])
    ok, message = ops.enable_phone_access(runner)
    assert not ok
    assert SECRET not in message and ERROR not in message
    runner.done()


@pytest.mark.parametrize("command_index", [1, 2], ids=["serve-status", "funnel-status"])
@pytest.mark.parametrize("failure", [
    (1, SECRET + ERROR), (124, ""), (0, "not json " + SECRET),
    (0, "[]"), (0, "null"), (0, "true"),
    (0, '{"AllowFunnel":{"pc:443":"true"}}'),
    OSError(SECRET + ERROR),
])
def test_phone_access_refuses_unknown_funnel_without_serve(ops, command_index, failure):
    steps = ts_steps(serve={})
    steps[command_index] = (steps[command_index][0], failure)
    status = ops.tailscale_status(Runner(steps))
    assert status["funnel"] is None
    runner = Runner(steps)
    ok, message = ops.enable_phone_access(runner)
    assert ok is False
    assert runner.calls == [argv for argv, _ in steps]
    assert "Funnel" in message
    assert SECRET not in message and ERROR not in message
    runner.done()


def test_phone_access_refuses_funnel_present_only_in_serve_config(ops):
    steps = ts_steps(serve={"Web": {"pc:443": {}}, "AllowFunnel": {"pc:443": True}})
    assert ops.tailscale_status(Runner(steps))["funnel"] is True
    runner = Runner(steps)
    ok, message = ops.enable_phone_access(runner)
    assert ok is False
    assert runner.calls == [argv for argv, _ in steps]
    assert "Funnel" in message
    runner.done()


@pytest.mark.parametrize("command_index", [1, 2], ids=["serve-status", "funnel-status"])
def test_known_public_funnel_takes_precedence_over_unknown_command(ops, command_index):
    public = {"AllowFunnel": {"pc:443": True}}
    steps = ts_steps(serve=public, funnel=public)
    steps[command_index] = (steps[command_index][0], (1, SECRET))
    assert ops.tailscale_status(Runner(steps))["funnel"] is True


@pytest.mark.parametrize("command_index", [1, 2], ids=["serve-status", "funnel-status"])
@pytest.mark.parametrize("reset_code", [0, 1])
def test_phone_access_rolls_back_unknown_postcheck(ops, command_index, reset_code):
    after = ts_steps()
    after[command_index] = (after[command_index][0], (1, SECRET + ERROR))
    steps = (ts_steps(serve={}) + [(["tailscale", "serve", "--bg", "8090"], (0, SECRET))]
             + after + [(["tailscale", "serve", "reset"], (reset_code, SECRET + ERROR))])
    runner = Runner(steps)
    ok, message = ops.enable_phone_access(runner)
    assert ok is False
    assert runner.calls == [argv for argv, _ in steps]
    assert SECRET not in message and ERROR not in message
    if reset_code:
        assert "초기화" in message and "실패" in message
    runner.done()


@pytest.fixture
def settings():
    return Settings(True, "http://127.0.0.1:8080", SECRET, SECRET, SECRET, SECRET, SECRET)


class Reader:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def result(self, name, result):
        self.calls.append(name)
        if self.error:
            raise self.error
        return result

    def account_mode_report(self):
        return self.result("account", {"v2_ok": True, "v3_ok": False, "v3_error": "40000", "secret": SECRET})

    def fetch_usdt_available(self):
        return self.result("balance", 25.5)

    def fetch_positions(self):
        return self.result("positions", [PositionSnapshot("BTCUSDT", "long", 0.001, 100, 101, 60, 2, "isolated")])

    def fetch_stop_orders(self, symbol):
        assert symbol == "BTCUSDT"
        return self.result("stops", [PlanOrder(symbol, "pos_loss", 90, "mark_price", 0.001, SECRET)])


class FT:
    def __init__(self, error=None):
        self.error = error

    def ping(self):
        if self.error:
            raise self.error
        return True

    def show_config(self):
        return {"state": "running", "dry_run": True, "api_server": {"password": SECRET}}

    def status(self):
        return [{"pair": "BTC/USDT:USDT", "is_short": True, "amount": 0.002,
                 "open_rate": 100, "current_rate": 99, "liquidation_price": 150,
                 "leverage": 2, "stop_loss_abs": 110, "secret": SECRET}]

    def profit(self):
        return {"closed_trade_count": 13, "winning_trades": 8, "losing_trades": 5,
                "winrate": 8 / 13, "profit_closed_ratio_mean": 0.008,
                "profit_closed_percent_mean": 0.8, "best_pair": SECRET, "bot_start_date": SECRET}

    def trades(self, limit=500):
        assert limit == 500
        now = time.time() * 1000
        rows = [(1, 0.02, "safe_long"), (2, 0.0, "safe_long"), (3, -0.01, "safe_long"),
                (40, 0.05, "safe_long"), (1, -0.0001, "smoke"), (5, 0.03, None)]
        return {"trades": [{"close_timestamp": int(now - days * DAY_MS), "profit_ratio": ratio,
                            "enter_tag": tag, "is_open": False, "pair": SECRET} for days, ratio, tag in rows],
                "trades_count": len(rows), "offset": 0, "total_trades": len(rows)}


DAY_MS = 86_400_000
# Strategy ("safe_") trades only: the smoke and untagged manual trades are counted apart.
STATS = {"closed_trades": 4, "wins": 3, "losses": 1, "win_rate": pytest.approx(0.75),
         "avg_profit_pct": pytest.approx(1.5), "win_rate_30d": pytest.approx(2 / 3),
         "closed_trades_30d": 3, "other_trades": 2, "partial": False}


def test_collect_dry_status_never_calls_private_reader(ops, settings):
    reader = Reader(error=AssertionError("private read forbidden"))
    result = ops.collect_status(settings, reader, FT())
    assert result.pop("stats") == STATS
    assert result == {
        "mode": "dry", "freqtrade": {"reachable": True, "state": "running", "dry_run": True,
                                      "take_profit": False},
        "account": None, "positions": [{"symbol": "BTCUSDT", "side": "short", "size": 0.002,
                                        "entry": 100, "mark": 99, "liq": 150, "leverage": 2,
                                        "margin": None, "margin_mode": None}],
        "stops": None, "exit_plans": {}, "trades": [],
        "smoke_state": {"exists": False, "trade_id": None, "mode": None, "close_confirmed": False},
    }
    assert reader.calls == []
    assert SECRET not in repr(result)


def test_collect_live_status_uses_reader_and_only_public_fields(ops, settings):
    reader = Reader()
    result = ops.collect_status(replace(settings, dry_run=False), reader, FT())
    assert result["mode"] == "live"
    assert result["account"] == {"kind": "classic", "usdt_available": 25.5}
    assert result["positions"] == [{"symbol": "BTCUSDT", "side": "long", "size": 0.001,
                                   "entry": 100, "mark": 101, "liq": 60, "leverage": 2,
                                   "margin": None, "margin_mode": "isolated"}]
    assert result["stops"] == [{"plan_type": "pos_loss", "trigger_price": 90, "trigger_type": "mark_price"}]
    assert reader.calls == ["account", "balance", "positions", "stops"]
    assert SECRET not in repr(result)


@pytest.mark.parametrize("dry", [True, False])
def test_collect_hides_all_exceptions_and_secrets(ops, settings, dry, capsys):
    from bitgetbot.ftclient import FreqtradeError

    failure = FreqtradeError(500, SECRET + ERROR)
    result = ops.collect_status(replace(settings, dry_run=dry), Reader(failure), FT(failure))
    assert result["freqtrade"] == {"reachable": False, "state": None, "dry_run": None, "take_profit": False}
    assert result["account"] is None and result["positions"] is None and result["stops"] is None
    assert result["stats"] is None
    assert SECRET not in repr(result) and ERROR not in repr(result)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_collect_isolates_failed_fields(ops, settings):
    ft = FT()
    ft.show_config = lambda: (_ for _ in ()).throw(ValueError(SECRET + ERROR))
    result = ops.collect_status(settings, Reader(), ft)
    assert result["freqtrade"] == {"reachable": True, "state": None, "dry_run": None, "take_profit": False}
    assert len(result["positions"]) == 1


def test_collect_unreachable_still_collects_live_exchange(ops, settings):
    ft = FT()
    ft.ping = lambda: False
    result = ops.collect_status(replace(settings, dry_run=False), Reader(), ft)
    assert result["freqtrade"]["reachable"] is False
    assert result["account"]["kind"] == "classic"
    assert len(result["positions"]) == 1


def test_collect_sanitizes_malformed_values(ops, settings):
    ft = FT()
    ft.show_config = lambda: {"state": SECRET, "dry_run": SECRET}
    ft.status = lambda: [{"pair": SECRET, "is_short": SECRET, "amount": SECRET,
                         "open_rate": SECRET, "current_rate": float("nan"),
                         "liquidation_price": float("inf"), "leverage": {"secret": SECRET}}]
    result = ops.collect_status(settings, Reader(), ft)
    assert result["freqtrade"] == {"reachable": True, "state": None, "dry_run": None, "take_profit": False}
    assert result["positions"] == [{name: None for name in
                                    ("symbol", "side", "size", "entry", "mark", "liq", "leverage", "margin", "margin_mode")}]
    assert SECRET not in repr(result)


@pytest.mark.parametrize("content,want", [
    ('{"trade_id":7,"mode":"live","close_confirmed":true,"secret":"SECRET-SENTINEL"}',
     {"exists": True, "trade_id": 7, "mode": "live", "close_confirmed": True}),
    ('{"trade_id":"8","mode":"dry"}',
     {"exists": True, "trade_id": "8", "mode": "dry", "close_confirmed": False}),
    ('{"trade_id":null,"mode":"dry"}',
     {"exists": True, "trade_id": None, "mode": "dry", "close_confirmed": False}),
    ('broken ' + SECRET, {"exists": True, "trade_id": None, "mode": None, "close_confirmed": False}),
    ('[]', {"exists": True, "trade_id": None, "mode": None, "close_confirmed": False}),
    ('{"trade_id":"SECRET-SENTINEL","mode":"live","close_confirmed":true}',
     {"exists": True, "trade_id": None, "mode": None, "close_confirmed": False}),
    ('{"trade_id":true,"mode":"dry"}',
     {"exists": True, "trade_id": None, "mode": None, "close_confirmed": False}),
    ('{"trade_id":1,"mode":"SECRET-SENTINEL","close_confirmed":true}',
     {"exists": True, "trade_id": None, "mode": None, "close_confirmed": False}),
])
def test_smoke_state_is_defensive_and_allowlisted(ops, settings, content, want):
    ops.STATE_PATH.write_text(content, encoding="utf-8")
    result = ops.collect_status(settings, Reader(), FT())
    assert result["smoke_state"] == want
    assert SECRET not in repr(result)


def test_smoke_state_unreadable_never_raises(ops, settings):
    ops.STATE_PATH.mkdir()
    assert ops.collect_status(settings, Reader(), FT())["smoke_state"] == {
        "exists": True, "trade_id": None, "mode": None, "close_confirmed": False,
    }


@pytest.mark.parametrize("dry", [True, False])
def test_collect_stats_for_current_mode_from_allowlisted_fields(ops, settings, dry):
    reader = Reader(error=AssertionError("stats never use the exchange reader")) if dry else Reader()
    result = ops.collect_status(replace(settings, dry_run=dry), reader, FT())
    assert result["mode"] == ("dry" if dry else "live")
    assert result["stats"] == STATS
    assert set(result["stats"]) == set(STATS)
    assert SECRET not in repr(result)


def test_smoke_test_loss_is_not_a_strategy_loss(ops, settings):
    """The live smoke-test trade (enter_tag "smoke") must not show up as "0승 1패"."""
    ft = FT()
    ft.profit = lambda: {"closed_trade_count": 1, "winning_trades": 0, "losing_trades": 1, "winrate": 0}
    ft.trades = lambda limit=500: {"trades": [{
        "close_timestamp": int(time.time() * 1000 - DAY_MS), "profit_ratio": -0.0009,
        "profit_abs": -0.0086, "enter_tag": "smoke", "is_open": False}], "total_trades": 1}
    assert ops.collect_status(settings, Reader(), ft)["stats"] == {
        "closed_trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_profit_pct": None,
        "win_rate_30d": None, "closed_trades_30d": 0, "other_trades": 1, "partial": False,
    }


@pytest.mark.parametrize("tag", [None, "", "smoke", "manual", "SAFE_long", " safe_long", 7])
def test_only_safe_prefixed_tags_are_strategy_trades(ops, tag):
    assert ops.is_strategy_tag(tag) is False
    assert ops.is_strategy_tag("safe_long") is True


def test_stats_without_closed_trades_skip_trade_history(ops, settings):
    ft = FT()
    ft.profit = lambda: {"closed_trade_count": 0, "winning_trades": 0, "losing_trades": 0,
                         "winrate": 0, "profit_closed_ratio_mean": 0.0}
    ft.trades = lambda limit=500: pytest.fail("no closed trades means no history request")
    assert ops.collect_status(settings, Reader(), ft)["stats"] == {
        "closed_trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_profit_pct": None,
        "win_rate_30d": None, "closed_trades_30d": 0, "other_trades": 0, "partial": False,
    }


def test_stats_ignore_aggregate_win_fields(ops, settings):
    ft = FT()
    ft.profit = lambda: {"closed_trade_count": 6, "winning_trades": True, "losing_trades": -1,
                         "winrate": 1.5, "profit_closed_ratio_mean": SECRET,
                         "profit_closed_percent_mean": float("inf")}
    stats = ops.collect_status(settings, Reader(), ft)["stats"]
    assert stats == STATS
    assert SECRET not in repr(stats)


@pytest.mark.parametrize("profit", [
    {"closed_trade_count": SECRET}, {"closed_trade_count": -1}, {"closed_trade_count": True},
    {}, [SECRET], None,
])
def test_stats_without_valid_closed_count_are_none(ops, settings, profit):
    ft = FT()
    ft.profit = lambda: profit
    assert ops.collect_status(settings, Reader(), ft)["stats"] is None


@pytest.mark.parametrize("history", [
    {"trades": [{"close_timestamp": "recent", "profit_ratio": 0.1}], "total_trades": 1},
    {"trades": [{"close_timestamp": None, "profit_ratio": 0.1}], "total_trades": 1},
    {"trades": [{"close_timestamp": 1, "profit_ratio": SECRET}], "total_trades": 1},
    {"trades": [SECRET], "total_trades": 1},
    {"trades": SECRET, "total_trades": 1},
    {"trades": [], "total_trades": SECRET},
    [SECRET],
])
def test_unclear_history_hides_stats_rather_than_mixing_in_manual_trades(ops, settings, history):
    ft = FT()
    ft.trades = lambda limit=500: history
    result = ops.collect_status(settings, Reader(), ft)
    assert result["stats"] is None
    assert SECRET not in repr(result)


def test_truncated_recent_page_is_partial_and_hides_30d_numbers(ops, settings):
    ft = FT()
    now = time.time() * 1000
    ft.trades = lambda limit=500: {
        "trades": [{"close_timestamp": int(now - DAY_MS), "profit_ratio": 0.01, "enter_tag": "safe_long"}] * 2,
        "trades_count": 2, "offset": 0, "total_trades": 13,
    }
    stats = ops.collect_status(settings, Reader(), ft)["stats"]
    assert stats["closed_trades"] == 2 and stats["partial"] is True
    assert stats["win_rate_30d"] is None and stats["closed_trades_30d"] is None


def test_open_trades_in_history_are_skipped(ops, settings):
    ft = FT()
    now = time.time() * 1000
    ft.trades = lambda limit=500: {"trades": [
        {"is_open": True, "enter_tag": "safe_long", "close_timestamp": None, "profit_ratio": None},
        {"close_timestamp": int(now - DAY_MS), "profit_ratio": -0.02, "enter_tag": "safe_long"},
    ], "total_trades": 2}
    stats = ops.collect_status(settings, Reader(), ft)["stats"]
    assert (stats["closed_trades"], stats["wins"], stats["losses"]) == (1, 0, 1)
    assert stats["avg_profit_pct"] == pytest.approx(-2.0)


def test_recent_window_with_no_trades_has_zero_count(ops, settings):
    ft = FT()
    old = int(time.time() * 1000 - 45 * DAY_MS)
    ft.trades = lambda limit=500: {"trades": [{"close_timestamp": old, "profit_ratio": None,
                                               "enter_tag": "safe_long"}],
                                   "trades_count": 1, "offset": 0, "total_trades": 13}
    stats = ops.collect_status(settings, Reader(), ft)["stats"]
    assert stats["closed_trades_30d"] == 0 and stats["win_rate_30d"] is None
    assert stats["closed_trades"] == 1 and stats["wins"] == 1


@pytest.mark.parametrize("dry", [True, False])
@pytest.mark.parametrize("failing", ["profit", "trades"])
def test_stats_errors_are_none_without_details(ops, settings, dry, failing, capsys):
    from bitgetbot.ftclient import FreqtradeError

    ft = FT()

    def fail(*args, **kwargs):
        raise FreqtradeError(500, SECRET + ERROR)

    setattr(ft, failing, fail)
    result = ops.collect_status(replace(settings, dry_run=dry), Reader(), ft)
    assert result["stats"] is None
    assert result["freqtrade"]["reachable"] is True
    assert SECRET not in repr(result) and ERROR not in repr(result)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_unreachable_freqtrade_skips_stats_requests(ops, settings):
    ft = FT()
    ft.ping = lambda: False
    ft.profit = lambda: pytest.fail("unreachable Freqtrade must not be asked for stats")
    assert ops.collect_status(settings, Reader(), ft)["stats"] is None


def test_default_runner_never_opens_a_console_window(monkeypatch):
    import subprocess as sp
    from bitgetbot.panel import ops as module
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return sp.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.default_runner(["docker", "info"], 5) == (0, "ok")
    assert seen["creationflags"] == getattr(sp, "CREATE_NO_WINDOW", 0)


class PlanFT(FT):
    """Freqtrade with one strategy long, one smoke long and one strategy short."""

    def __init__(self, candles=None):
        super().__init__()
        self.candle_calls = []
        self.candles = candles if candles is not None else {
            "columns": ["date", "date_4h", "low_4h", "atr_4h", "exit_low_4h", "secret"],
            "data": [[f"h{i}", f"2026-10-{19 + i // 6:02d}T{(i % 6) * 4:02d}:00:00Z", low, 775.9, 81163.0, SECRET]
                     for i, low in enumerate([80000.0, 81900, 82000, 82100, 82200, 82300, 82400,
                                              82500, 82600, 82700, 81500.0])],
        }

    def status(self):
        return [
            {"pair": "BTC/USDT:USDT", "is_short": False, "enter_tag": "safe_long", "open_rate": 83472.1,
             "stop_loss_abs": 81920.3, "secret": SECRET},
            {"pair": "ETH/USDT:USDT", "is_short": False, "enter_tag": "smoke", "stop_loss_abs": 2000},
            {"pair": "SOL/USDT:USDT", "is_short": True, "enter_tag": "safe_short", "stop_loss_abs": 200},
        ]

    def pair_candles(self, pair, timeframe, limit):
        self.candle_calls.append((pair, timeframe, limit))
        return self.candles


def test_exit_plans_cover_strategy_longs_only_with_the_next_exit_window(ops, settings):
    ft = PlanFT()
    plans = ops.collect_status(settings, Reader(), ft)["exit_plans"]
    # The next 4h close is checked against the last 10 completed 4h lows: the oldest bar (80000)
    # has rolled off and the newest (81500) is in, unlike the strategy's shifted exit_low_4h (81163).
    assert plans == {"BTCUSDT": {"exit_line": 81500.0, "trail_distance": pytest.approx(3 * 775.9),
                                 "bot_stop": 81920.3}}
    assert ft.candle_calls == [("BTC/USDT:USDT", "1h", ops.PLAN_CANDLES)]
    assert ops.PLAN_CANDLES >= 4 * ops.EXIT_BARS + 4
    assert SECRET not in repr(plans)


def test_exit_line_needs_ten_completed_4h_candles(ops, settings):
    candles = PlanFT().candles
    short = {"columns": candles["columns"], "data": candles["data"][-9:]}
    assert ops.collect_status(settings, Reader(), PlanFT(short))["exit_plans"]["BTCUSDT"]["exit_line"] is None


@pytest.mark.parametrize("candles", [{}, {"columns": ["atr_4h"], "data": []},
                                     {"columns": ["atr_4h", "date_4h", "low_4h"], "data": [[float("nan"), "x", -5]]}])
def test_exit_plans_leave_unreadable_levels_empty(ops, settings, candles):
    plans = ops.collect_status(settings, Reader(), PlanFT(candles))["exit_plans"]
    assert plans == {"BTCUSDT": {"exit_line": None, "trail_distance": None, "bot_stop": 81920.3}}


def test_exit_plans_are_none_when_candles_fail_or_freqtrade_is_down(ops, settings):
    class Broken(PlanFT):
        def pair_candles(self, pair, timeframe, limit):
            raise RuntimeError(SECRET)

    assert ops.collect_status(settings, Reader(), Broken())["exit_plans"] is None
    assert ops.collect_status(settings, Reader(), FT(RuntimeError("down")))["exit_plans"] is None


def test_exit_plan_constants_match_the_strategy_rules(ops):
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "freqtrade" / "user_data" / "strategies" / "live_rules.py"
    spec = importlib.util.spec_from_file_location("live_rules_for_panel", path)
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    assert ops.STRATEGY_TAG_PREFIX == rules.STRATEGY_TAG_PREFIX
    assert ops.TRAIL_ATR == rules.TRAIL_ATR
    strategy = (path.parent / "SafeTrendLive.py").read_text(encoding="utf-8")
    assert f'dataframe["exit_low"] = dataframe.low.rolling({ops.EXIT_BARS}).min().shift(1)' in strategy
    assert 'timeframe = "1h"' in strategy and ops.STRATEGY_TIMEFRAME == "1h"
