import json
import subprocess
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


@pytest.mark.parametrize("status,serve,funnel", [
    ({}, {}, {}),
    ({"BackendState": "NeedsLogin", "Self": None}, {"Web": {}}, {"AllowFunnel": {"pc:443": False}}),
    ([], [], []),
    ({"BackendState": True, "Self": {"DNSName": SECRET}}, {"TCP": None}, {"AllowFunnel": {"pc:443": "true"}}),
])
def test_tailscale_status_handles_missing_and_malformed_fields(ops, status, serve, funnel):
    result = ops.tailscale_status(Runner(ts_steps(status, serve, funnel)))
    assert result == {"installed": True, "logged_in": False, "dns_name": None, "serving": False, "funnel": False}


@pytest.mark.parametrize("payload", ["not json " + SECRET, "null", "true"])
def test_tailscale_status_hides_command_output(ops, payload):
    runner = Runner([(argv, (0, payload)) for argv, _ in ts_steps()])
    assert ops.tailscale_status(runner) == {
        "installed": True, "logged_in": False, "dns_name": None, "serving": False, "funnel": False,
    }


def test_tailscale_status_ignores_failed_command_json(ops):
    runner = Runner([(argv, (1, output)) for argv, (_, output) in ts_steps()])
    assert ops.tailscale_status(runner) == {
        "installed": True, "logged_in": False, "dns_name": None, "serving": False, "funnel": False,
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
    runner = Runner(ts_steps(serve={}) + [(["tailscale", "serve", "--bg", "8090"], (0, SECRET))] + after)
    ok, message = ops.enable_phone_access(runner)
    assert ok is False
    assert SECRET not in message
    assert "evil.invalid" not in message
    runner.done()


def test_phone_access_hides_serve_failure(ops):
    runner = Runner(ts_steps(serve={}) + [(["tailscale", "serve", "--bg", "8090"], (1, SECRET + ERROR))])
    ok, message = ops.enable_phone_access(runner)
    assert not ok
    assert SECRET not in message and ERROR not in message
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


def test_collect_dry_status_never_calls_private_reader(ops, settings):
    reader = Reader(error=AssertionError("private read forbidden"))
    result = ops.collect_status(settings, reader, FT())
    assert result == {
        "mode": "dry", "freqtrade": {"reachable": True, "state": "running", "dry_run": True},
        "account": None, "positions": [{"symbol": "BTCUSDT", "side": "short", "size": 0.002,
                                        "entry": 100, "mark": 99, "liq": 150, "leverage": 2}],
        "stops": None, "smoke_state": {"exists": False, "trade_id": None, "mode": None, "close_confirmed": False},
    }
    assert reader.calls == []
    assert SECRET not in repr(result)


def test_collect_live_status_uses_reader_and_only_public_fields(ops, settings):
    reader = Reader()
    result = ops.collect_status(replace(settings, dry_run=False), reader, FT())
    assert result["mode"] == "live"
    assert result["account"] == {"kind": "classic", "usdt_available": 25.5}
    assert result["positions"] == [{"symbol": "BTCUSDT", "side": "long", "size": 0.001,
                                   "entry": 100, "mark": 101, "liq": 60, "leverage": 2}]
    assert result["stops"] == [{"plan_type": "pos_loss", "trigger_price": 90, "trigger_type": "mark_price"}]
    assert reader.calls == ["account", "balance", "positions", "stops"]
    assert SECRET not in repr(result)


@pytest.mark.parametrize("dry", [True, False])
def test_collect_hides_all_exceptions_and_secrets(ops, settings, dry, capsys):
    from bitgetbot.ftclient import FreqtradeError

    failure = FreqtradeError(500, SECRET + ERROR)
    result = ops.collect_status(replace(settings, dry_run=dry), Reader(failure), FT(failure))
    assert result["freqtrade"] == {"reachable": False, "state": None, "dry_run": None}
    assert result["account"] is None and result["positions"] is None and result["stops"] is None
    assert SECRET not in repr(result) and ERROR not in repr(result)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_collect_isolates_failed_fields(ops, settings):
    ft = FT()
    ft.show_config = lambda: (_ for _ in ()).throw(ValueError(SECRET + ERROR))
    result = ops.collect_status(settings, Reader(), ft)
    assert result["freqtrade"] == {"reachable": True, "state": None, "dry_run": None}
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
    assert result["freqtrade"] == {"reachable": True, "state": None, "dry_run": None}
    assert result["positions"] == [{name: None for name in ("symbol", "side", "size", "entry", "mark", "liq", "leverage")}]
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
