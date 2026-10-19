import json
from dataclasses import replace

import pytest

from bitgetbot import smoke
from bitgetbot.ftclient import FreqtradeUnreachable
from bitgetbot.models import ContractSpec, PlanOrder, PositionSnapshot
from bitgetbot.privcfg import Settings
from bitgetbot.smoke import (
    ft_trade_checks, preflight_problems, require_live_confirmation,
    run_check, smoke_stake, wait_until,
)
from bitgetbot.stopcheck import overall


POS = PositionSnapshot("BTCUSDT", "long", 0.0001, 111240.5, 112050.0, 56300.0, 2, "isolated")
SPEC = ContractSpec("BTCUSDT", 0.0001, 0.0001, 5.0)
TRADE = {"trade_id": 7, "pair": "BTC/USDT:USDT", "enter_tag": "smoke",
         "open_rate": 111240.5, "stop_loss_abs": 107900.0,
         "liquidation_price": 56300.0, "is_short": False}
EXIT_WARNING = "지금 바로 'python -m bitgetbot.smoke exit'"


def test_preflight_ok_live_clean_account():
    assert preflight_problems(True, False, "classic", [], 0, 0, 50.0) == []


def test_preflight_rejects_existing_exposure():
    assert len(preflight_problems(True, False, "classic", [POS], 1, 1, 50.0)) == 3


def test_preflight_rejects_non_classic_and_low_balance():
    assert len(preflight_problems(True, False, "unknown", [], 0, 0, 5.0)) == 2


def test_preflight_dry_run_ignores_exchange_state():
    assert preflight_problems(True, True, "unknown", [POS], 1, 0, None) == []


@pytest.mark.parametrize("balance", [None, float("nan"), float("inf"), -1])
def test_preflight_unknown_balance_is_problem(balance):
    assert len(preflight_problems(True, False, "classic", [], 0, 0, balance)) == 1


def test_preflight_reports_ft_unreachable():
    assert preflight_problems(False, False, "classic", [], 0, 0, 50.0) == [
        "Freqtrade에 연결할 수 없습니다 (Docker Desktop과 `docker compose up -d` 확인)",
    ]


def test_live_requires_typed_confirmation():
    assert require_live_confirmation(True, "") is True
    assert require_live_confirmation(False, "yes") is False
    assert require_live_confirmation(False, " LIVE ") is True


def test_smoke_stake_meets_minimum_and_cap():
    assert smoke_stake(SPEC, 112050.0, 2) == 7.29
    with pytest.raises(ValueError):
        smoke_stake(SPEC, 200000.0, 1)
    assert smoke_stake(ContractSpec("BTCUSDT", 0.0001, 0.0001, 10), 10000, 2) == 6.5
    assert smoke_stake(ContractSpec("BTCUSDT", 0.0001, 0.0001, 20), 10000, 2, 1) == 10


@pytest.mark.parametrize("price,leverage,buffer", [(0, 2, 1.3), (float("nan"), 2, 1.3),
                                                     (112050, 0, 1.3), (112050, 2, -1)])
def test_smoke_stake_rejects_invalid_inputs(price, leverage, buffer):
    with pytest.raises(ValueError):
        smoke_stake(SPEC, price, leverage, buffer)


def fake_clock():
    t = [0.0]
    return t, lambda s: t.__setitem__(0, t[0] + s), lambda: t[0]


def test_wait_until_times_out_with_fake_clock():
    t, sleep, clock = fake_clock()
    assert wait_until(lambda: False, 10, 5, sleep=sleep, clock=clock) is False
    assert t[0] == 10


def test_wait_until_stops_at_deadline_without_oversleep():
    t, sleep, clock = fake_clock()
    assert wait_until(lambda: t[0] >= 6, 10, 3, sleep=sleep, clock=clock) is True
    assert t[0] == 6
    assert wait_until(lambda: False, 2, 5, sleep=sleep, clock=clock) is False
    assert t[0] == 8


def test_ft_trade_checks_dry_run():
    assert overall(ft_trade_checks(TRADE)) == "PASS"
    assert {check.name for check in ft_trade_checks(TRADE)} == {"stop_side", "stop_inside_liq"}
    assert overall(ft_trade_checks({**TRADE, "is_short": True, "stop_loss_abs": 115000,
                                    "liquidation_price": 160000})) == "PASS"


@pytest.mark.parametrize("fields", [{"stop_loss_abs": 120000}, {"stop_loss_abs": 60000},
                                    {"liquidation_price": None}, {"stop_loss_abs": None},
                                    {"open_rate": "nan"}, {"stop_loss_abs": 0},
                                    {"liquidation_price": 0}, {"is_short": None}])
def test_ft_trade_checks_never_pass_invalid_or_unsafe_values(fields):
    assert overall(ft_trade_checks({**TRADE, **fields})) != "PASS"


class FakeReader:
    def __init__(self):
        self.calls = []
        self.positions = []
        self.position = POS
        self.plans = [PlanOrder("BTCUSDT", "pos_loss", 107900, "mark_price", None, "s1")]
        self.balance = 50.0
        self.orders = 0

    def fetch_contract(self, symbol):
        self.calls.append("contract")
        assert symbol == "BTCUSDT"
        return SPEC

    def fetch_mark_price(self, symbol):
        self.calls.append("mark")
        assert symbol == "BTCUSDT"
        return 112050.0

    def account_mode_report(self):
        self.calls.append("account")
        return {"v2_ok": True, "v3_ok": False}

    def fetch_positions(self):
        self.calls.append("positions")
        return self.positions

    def fetch_position(self, symbol):
        self.calls.append("position")
        return self.position

    def fetch_stop_orders(self, symbol):
        self.calls.append("stops")
        return self.plans

    def fetch_open_order_count(self):
        self.calls.append("orders")
        return self.orders

    def fetch_usdt_available(self):
        self.calls.append("balance")
        if self.balance is None:
            raise ValueError("USDT 계정 잔액을 찾을 수 없습니다")
        return self.balance

    def fetch_fills(self, symbol, start_ms):
        self.calls.append(("fills", symbol, start_ms))
        return []


def test_check_fails_when_no_stop_order(capsys):
    reader = FakeReader()
    reader.plans = []
    t, sleep, clock = fake_clock()
    assert run_check(reader, "BTCUSDT", 120, 5, sleep, clock, print) == 1
    assert t[0] == 120
    output = capsys.readouterr().out
    assert EXIT_WARNING in output
    assert "stop_exists" in output and "FAIL" in output


def test_check_lists_all_symbol_candidates(capsys):
    reader = FakeReader()
    reader.plans += [PlanOrder("BTCUSDT", "loss_plan", 105000, "fill_price", 0.0001, "s2")]
    t, sleep, clock = fake_clock()
    assert run_check(reader, "BTCUSDT", 120, 5, sleep, clock, print) == 0
    output = capsys.readouterr().out
    assert all(text in output for text in ("s1", "pos_loss", "107900", "mark_price",
                                           "s2", "loss_plan", "105000", "fill_price"))
    assert t[0] == 0


def test_check_missing_position_is_not_pass(capsys):
    reader = FakeReader()
    reader.position = None
    t, sleep, clock = fake_clock()
    assert run_check(reader, "BTCUSDT", 10, 5, sleep, clock, print) == 1
    assert EXIT_WARNING in capsys.readouterr().out


class FakeFT:
    def __init__(self):
        self.dry_run = True
        self.state = "stopped"
        self.trades = []
        self.calls = []
        self.reachable = True

    def ping(self):
        self.calls.append("ping")
        return self.reachable

    def show_config(self):
        self.calls.append("config")
        return {"dry_run": self.dry_run, "state": self.state, "secret": "SECRET-SENTINEL"}

    def status(self):
        self.calls.append("status")
        return self.trades

    def start(self):
        self.calls.append("start")
        self.state = "running"
        return {"status": "starting"}

    def force_enter(self, *args):
        self.calls.append(("enter", args))
        self.trades = [dict(TRADE)]
        return dict(TRADE)

    def force_exit(self, *args):
        self.calls.append(("exit", args))
        self.trades = []
        return {"result": "Created exit order"}


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings(True, "http://127.0.0.1:8080", "user", "SECRET-SENTINEL",
                        "SECRET-SENTINEL", "SECRET-SENTINEL", "SECRET-SENTINEL")
    reader, ft = FakeReader(), FakeFT()
    keys = []
    monkeypatch.setattr(smoke, "load_settings", lambda: settings)
    monkeypatch.setattr(smoke, "make_exchange", lambda *args: keys.append(args))
    monkeypatch.setattr(smoke, "BitgetReader", lambda _: reader)
    monkeypatch.setattr(smoke, "FreqtradeClient", lambda *args: ft)
    t, sleep, clock = fake_clock()
    monkeypatch.setattr(smoke.time, "sleep", sleep)
    monkeypatch.setattr(smoke.time, "monotonic", clock)
    return settings, reader, ft, keys, t


def test_cli_dry_run_roundtrip_uses_no_private_bitget(cli, capsys, tmp_path, monkeypatch):
    _, reader, ft, keys, _ = cli
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("dry-run asked for LIVE"))
    assert smoke.main(["preflight"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "=== 모드: 모의(DRY-RUN) ==="
    assert smoke.main(["enter"]) == 0
    state = json.loads((tmp_path / ".smoke_state.json").read_text())
    assert state["trade_id"] == 7 and state["started_ms"] > 0
    assert ("enter", ("BTC/USDT:USDT", "long", 7.29, 2, "market", "smoke")) in ft.calls
    assert ft.calls.index("start") < ft.calls.index(("enter", ("BTC/USDT:USDT", "long", 7.29, 2, "market", "smoke")))
    assert smoke.main(["check"]) == 0
    assert smoke.main(["exit"]) == 0
    assert ("exit", (7, "market")) in ft.calls
    assert ft.trades == []
    assert "dry-run: 거래소 손절 확인은 실전에서만 합니다" in capsys.readouterr().out
    assert set(reader.calls) <= {"contract", "mark"}
    assert all(k == ("", "", "") for k in keys)


def test_cli_live_balance_missing_is_preflight_problem(cli, monkeypatch, capsys):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.balance = None
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False))
    assert smoke.main(["preflight"]) == 1
    output = capsys.readouterr().out
    assert output.splitlines()[0] == "=== 모드: 실전(LIVE) ==="
    assert "USDT" in output
    assert "SECRET-SENTINEL" not in output


def test_cli_enter_rechecks_preflight_before_confirmation(cli, monkeypatch):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.positions = [POS]
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must reject exposure first"))
    assert smoke.main(["enter"]) == 1
    assert "start" not in ft.calls
    assert not any(isinstance(call, tuple) for call in ft.calls)


def test_cli_live_confirmation_refusal_never_orders(cli, monkeypatch):
    settings, _, ft, _, _ = cli
    ft.dry_run = False
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False))
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert smoke.main(["enter"]) == 1
    assert "start" not in ft.calls
    assert not any(isinstance(call, tuple) for call in ft.calls)


@pytest.mark.parametrize("cmd", ["preflight", "enter", "exit"])
def test_cli_mode_mismatch_blocks_order_commands(cli, cmd, capsys):
    _, _, ft, _, _ = cli
    ft.dry_run = False
    assert smoke.main([cmd]) == 1
    assert not any(isinstance(call, tuple) for call in ft.calls)
    assert "모드" in capsys.readouterr().out


def test_cli_unreachable_short_circuits_exchange_reads(cli, capsys):
    _, reader, ft, _, _ = cli
    ft.reachable = False
    assert smoke.main(["preflight"]) == 1
    assert reader.calls == []
    assert "Freqtrade에 연결할 수 없습니다" in capsys.readouterr().out


def test_cli_restart_waits_for_user_and_preserves_trade_id(cli, monkeypatch, tmp_path, capsys):
    _, reader, ft, _, t = cli
    ft.trades = [dict(TRADE)]
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1, "trade_id": 7}))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert smoke.main(["restart-check"]) == 0
    assert prompts
    output = capsys.readouterr().out
    assert "docker compose -f freqtrade/docker-compose.yml restart freqtrade" in output
    assert "7" in output and "PASS" in output
    assert "start" in ft.calls
    assert t[0] == 30
    assert reader.calls == []


def test_cli_restart_wrong_trade_is_failure(cli, monkeypatch, tmp_path, capsys):
    _, _, ft, _, _ = cli
    ft.trades = [{**TRADE, "trade_id": 8}]
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1, "trade_id": 7}))
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert smoke.main(["restart-check"]) == 1
    assert EXIT_WARNING in capsys.readouterr().out


def test_cli_error_does_not_print_secret_or_traceback(cli, monkeypatch, capsys):
    _, reader, _, _, _ = cli

    def fail(_):
        raise RuntimeError("SECRET-SENTINEL")

    monkeypatch.setattr(reader, "fetch_contract", fail)
    assert smoke.main(["enter"]) == 1
    output = capsys.readouterr().out
    assert "SECRET-SENTINEL" not in output
    assert "Traceback" not in output


def test_cli_ledger_live_uses_saved_start_and_is_idempotent(cli, monkeypatch, tmp_path):
    fill = {"tradeId": "t1", "orderId": "o1", "symbol": "BTCUSDT", "side": "buy",
            "tradeSide": "open", "price": "111240.5", "baseVolume": "0.0001",
            "quoteVolume": "11.12405", "profit": "0", "cTime": "1792396800000",
            "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.00667443"}]}

    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False))
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1792396800000, "trade_id": 7}))

    def fills(symbol, start_ms):
        assert (symbol, start_ms) == ("BTCUSDT", 1792396800000)
        return [fill]

    monkeypatch.setattr(reader, "fetch_fills", fills)
    monkeypatch.setattr(smoke, "fetch_krw_per_usdt", lambda: (1390.1, "2026-10-19T10:00:00Z"))
    assert smoke.main(["ledger"]) == 0
    assert smoke.main(["ledger"]) == 0
    [path] = (tmp_path / "ledger").glob("smoke-*.csv")
    assert len(path.read_text().splitlines()) == 2


def test_cli_ledger_dry_run_never_fetches_fx(cli, monkeypatch):
    _, reader, _, _, _ = cli
    monkeypatch.setattr(smoke, "fetch_krw_per_usdt", lambda: pytest.fail("no FX in dry-run"))
    assert smoke.main(["ledger"]) == 0
    assert reader.calls == []


def test_cli_enter_recovers_transient_status_read_without_repeating_order(cli, monkeypatch):
    _, _, ft, _, t = cli
    status = ft.status
    reads_after_order = [0]

    def flaky_status():
        if ft.trades:
            reads_after_order[0] += 1
            if reads_after_order[0] == 1:
                raise FreqtradeUnreachable("SECRET-SENTINEL")
        return status()

    monkeypatch.setattr(ft, "status", flaky_status)
    assert smoke.main(["enter"]) == 0
    assert t[0] == 5
    assert len([call for call in ft.calls if isinstance(call, tuple) and call[0] == "enter"]) == 1


def test_cli_exit_live_waits_for_both_sources_then_exports(cli, monkeypatch, tmp_path):
    settings, reader, ft, _, t = cli
    ft.dry_run = False
    ft.trades = [dict(TRADE)]
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False))
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1792396800000, "trade_id": 7}))
    queries = [0]

    def position(symbol):
        queries[0] += 1
        return POS if queries[0] < 3 else None

    monkeypatch.setattr(reader, "fetch_position", position)
    assert smoke.main(["exit"]) == 0
    assert t[0] == 10
    assert ("exit", (7, "market")) in ft.calls
    assert ("fills", "BTCUSDT", 1792396800000) in reader.calls


def test_cli_exit_timeout_does_not_export_or_repeat_order(cli, monkeypatch, capsys):
    settings, reader, ft, _, t = cli
    ft.dry_run = False
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False))
    assert smoke.main(["exit"]) == 1
    assert t[0] == 60
    assert ("exit", ("all", "market")) in ft.calls
    assert "비트겟 웹(PC)" in capsys.readouterr().out
    assert not any(isinstance(call, tuple) for call in reader.calls)


def test_cli_restart_ping_timeout_does_not_order(cli, monkeypatch, tmp_path, capsys):
    _, _, ft, _, t = cli
    ft.reachable = False
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1, "trade_id": 7}))
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert smoke.main(["restart-check"]) == 1
    assert t[0] == 90
    assert "start" not in ft.calls
    assert EXIT_WARNING in capsys.readouterr().out


def test_cli_enter_order_error_is_never_retried_and_keeps_start(cli, monkeypatch, tmp_path, capsys):
    _, _, ft, _, _ = cli
    attempts = []

    def uncertain(*args):
        attempts.append(args)
        raise FreqtradeUnreachable("SECRET-SENTINEL")

    monkeypatch.setattr(ft, "force_enter", uncertain)
    assert smoke.main(["enter"]) == 1
    assert len(attempts) == 1
    assert json.loads((tmp_path / ".smoke_state.json").read_text())["started_ms"] > 0
    output = capsys.readouterr().out
    assert "SECRET-SENTINEL" not in output and EXIT_WARNING in output


@pytest.mark.parametrize("argv", [["--help"], ["check", "--help"]])
def test_cli_help_is_korean(argv, capsys):
    with pytest.raises(SystemExit) as result:
        smoke.main(argv)
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "사용법:" in output and "옵션:" in output and "도움말" in output
    assert "show this help message" not in output


def test_cli_bad_argument_is_korean_and_does_not_echo_input(capsys):
    with pytest.raises(SystemExit) as result:
        smoke.main(["SECRET-SENTINEL"])
    assert result.value.code == 2
    output = capsys.readouterr().err
    assert "오류:" in output
    assert "SECRET-SENTINEL" not in output


def test_cli_init_reports_existing_without_loading_settings(cli, monkeypatch, capsys):
    monkeypatch.setattr(smoke, "init_private", lambda: False)
    monkeypatch.setattr(smoke, "load_settings", lambda: pytest.fail("init loads settings"))
    assert smoke.main(["init"]) == 0
    output = capsys.readouterr().out
    assert "이미 있음" in output and "exchange.key/secret/password" in output
