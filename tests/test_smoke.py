import json
from dataclasses import replace

import pytest

from bitgetbot import smoke
from bitgetbot.ftclient import FreqtradeError, FreqtradeUnreachable
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
        self.plan_orders = 0

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
        return {"v2_ok": True, "v3_ok": False, "v3_error": "Exception: 40084"}

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

    def fetch_plan_order_count(self, symbol=None):
        self.calls.append(("plan_orders", symbol))
        return self.plan_orders

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
        self.bot_startup_ts = 100

    def health(self):
        self.calls.append("health")
        return {"bot_startup_ts": self.bot_startup_ts}

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
    reader.plan_orders = 99
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("dry-run asked for LIVE"))
    assert smoke.main(["preflight"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "=== 모드: 모의(DRY-RUN) ==="
    assert smoke.main(["enter"]) == 0
    state = json.loads((tmp_path / ".smoke_state.json").read_text())
    assert state["trade_id"] == 7 and state["started_ms"] > 0
    assert state["mode"] == "dry"
    assert ("enter", ("BTC/USDT:USDT", "long", 7.29, 2, "market", "smoke")) in ft.calls
    assert ft.calls.index("start") < ft.calls.index(("enter", ("BTC/USDT:USDT", "long", 7.29, 2, "market", "smoke")))
    assert smoke.main(["check"]) == 0
    assert smoke.main(["exit"]) == 0
    assert ("exit", (7, "market")) in ft.calls
    assert ft.trades == []
    assert not (tmp_path / ".smoke_state.json").exists()
    assert "dry-run: 거래소 손절 확인은 실전에서만 합니다" in capsys.readouterr().out
    assert set(reader.calls) <= {"contract", "mark"}
    assert all(k == ("", "", "") for k in keys)


@pytest.mark.parametrize("count,expected", [(0, 0), (1, 1), (3, 1)])
def test_cli_live_preflight_rejects_leftover_plan_orders(cli, monkeypatch, capsys, count, expected):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.plan_orders = count
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    assert smoke.main(["preflight"]) == expected
    assert ("plan_orders", None) in reader.calls
    output = capsys.readouterr().out
    if count:
        assert f"미체결 예약(트리거·손절) 주문이 {count}개 있습니다" in output
    else:
        assert "문제 0개" in output


def test_cli_live_enter_rejects_plan_orders_before_confirmation(cli, monkeypatch, capsys):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.plan_orders = 1
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("leftover trigger must block entry"))
    assert smoke.main(["enter"]) == 1
    assert "미체결 예약(트리거·손절) 주문이 1개 있습니다" in capsys.readouterr().out
    assert "start" not in ft.calls
    assert not any(isinstance(call, tuple) and call[0] == "enter" for call in ft.calls)


@pytest.mark.parametrize("missing", [False, True])
def test_cli_dry_check_unknown_liquidation_warns_and_succeeds(cli, capsys, missing):
    _, reader, ft, _, _ = cli
    trade = {**TRADE, "liquidation_price": None}
    if missing:
        trade.pop("liquidation_price")
    ft.trades = [trade]
    assert smoke.main(["check"]) == 0
    output = capsys.readouterr().out
    assert "UNKNOWN" in output and "경고" in output and "청산가" in output
    assert EXIT_WARNING not in output
    assert reader.calls == []


def test_cli_dry_check_fail_still_fails_with_unknown_liquidation(cli, capsys):
    _, reader, ft, _, _ = cli
    ft.trades = [{**TRADE, "liquidation_price": None, "stop_loss_abs": 120000}]
    assert smoke.main(["check"]) == 1
    output = capsys.readouterr().out
    assert "FAIL" in output and EXIT_WARNING in output
    assert reader.calls == []


def test_cli_live_check_unknown_liquidation_still_fails(cli, monkeypatch, capsys):
    settings, reader, ft, _, t = cli
    ft.dry_run = False
    reader.position = replace(POS, liq_price=None)
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    assert smoke.main(["check"]) == 1
    output = capsys.readouterr().out
    assert "UNKNOWN" in output and EXIT_WARNING in output
    assert t[0] == 120


def test_cli_enter_never_orders_if_start_stays_stopped(cli, monkeypatch, tmp_path, capsys):
    _, _, ft, _, t = cli

    def start_without_ready():
        ft.calls.append("start")
        return {"status": "starting"}

    monkeypatch.setattr(ft, "start", start_without_ready)
    assert smoke.main(["enter"]) == 1
    assert t[0] == 10
    assert "start" in ft.calls
    assert not any(isinstance(call, tuple) and call[0] == "enter" for call in ft.calls)
    assert not (tmp_path / ".smoke_state.json").exists()
    assert "10초" in capsys.readouterr().out


def test_cli_enter_waits_for_running_at_one_second_intervals(cli, monkeypatch):
    _, _, ft, _, t = cli
    config = ft.show_config
    enter = ft.force_enter
    polls = []

    def start_without_ready():
        ft.calls.append("start")
        return {"status": "starting"}

    def delayed_config():
        if "start" in ft.calls:
            polls.append(t[0])
            if t[0] >= 3:
                ft.state = "running"
        return config()

    def enter_when_ready(*args):
        assert ft.state == "running" and t[0] == 3
        return enter(*args)

    monkeypatch.setattr(ft, "start", start_without_ready)
    monkeypatch.setattr(ft, "show_config", delayed_config)
    monkeypatch.setattr(ft, "force_enter", enter_when_ready)
    assert smoke.main(["enter"]) == 0
    assert polls == [0, 1, 2, 3, 3]


def test_cli_live_balance_missing_is_preflight_problem(cli, monkeypatch, capsys):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.balance = None
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    assert smoke.main(["preflight"]) == 1
    output = capsys.readouterr().out
    assert output.splitlines()[0] == "=== 모드: 실전(LIVE) ==="
    assert "USDT" in output
    assert "SECRET-SENTINEL" not in output


def test_cli_enter_rechecks_preflight_before_confirmation(cli, monkeypatch):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.positions = [POS]
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must reject exposure first"))
    assert smoke.main(["enter"]) == 1
    assert "start" not in ft.calls
    assert not any(isinstance(call, tuple) for call in ft.calls)


def test_cli_live_confirmation_refusal_never_orders(cli, monkeypatch):
    settings, _, ft, _, _ = cli
    ft.dry_run = False
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert smoke.main(["enter"]) == 1
    assert "start" not in ft.calls
    assert not any(isinstance(call, tuple) for call in ft.calls)


@pytest.mark.parametrize("cmd", ["preflight", "enter"])
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
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1, "trade_id": 7, "mode": "dry"}))
    prompts = []
    def restart(prompt):
        assert ft.calls == ["health"]
        prompts.append(prompt)
        ft.bot_startup_ts = 101
        return ""

    monkeypatch.setattr("builtins.input", restart)
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
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1, "trade_id": 7, "mode": "dry"}))
    monkeypatch.setattr("builtins.input", lambda _: setattr(ft, "bot_startup_ts", 101) or "")
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
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1792396800000, "trade_id": 7, "mode": "live"}))

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


@pytest.fixture
def live_fills(cli, monkeypatch):
    _, reader, _, _, _ = cli
    fill = {"tradeId": "t1", "orderId": "o1", "symbol": "BTCUSDT", "side": "sell",
            "tradeSide": "close", "price": "111240.5", "baseVolume": "0.0001",
            "quoteVolume": "11.12405", "profit": "0", "cTime": "1792396800000",
            "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.00667443"}]}

    def fills(symbol, start_ms):
        reader.calls.append(("fills", symbol, start_ms))
        return [fill]

    monkeypatch.setattr(reader, "fetch_fills", fills)
    monkeypatch.setattr(smoke, "fetch_krw_per_usdt", lambda: (1390.1, "2026-10-19T10:00:00Z"))


def test_cli_exit_live_waits_for_both_sources_then_exports(cli, live_fills, monkeypatch, tmp_path):
    settings, reader, ft, _, t = cli
    ft.dry_run = False
    ft.trades = [dict(TRADE)]
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1792396800000, "trade_id": 7, "mode": "live"}))
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
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite"))
    assert smoke.main(["exit"]) == 1
    assert t[0] == 60
    assert ("exit", ("all", "market")) in ft.calls
    assert "비트겟 웹(PC)" in capsys.readouterr().out
    assert not any(isinstance(call, tuple) for call in reader.calls)


def test_cli_restart_ping_timeout_does_not_order(cli, monkeypatch, tmp_path, capsys):
    _, _, ft, _, t = cli
    ft.reachable = False
    (tmp_path / ".smoke_state.json").write_text(json.dumps({"started_ms": 1, "trade_id": 7, "mode": "dry"}))
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


@pytest.mark.parametrize("argv", [["--help"], ["check", "--help"], ["set-mode", "--help"]])
def test_cli_help_is_korean(argv, capsys):
    with pytest.raises(SystemExit) as result:
        smoke.main(argv)
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "사용법:" in output and "옵션:" in output and "도움말" in output
    assert "show this help message" not in output
    assert "positional arguments" not in output


@pytest.mark.parametrize("argv", [["SECRET-SENTINEL"], ["set-mode", "SECRET-SENTINEL"]])
def test_cli_bad_argument_is_korean_and_does_not_echo_input(capsys, argv):
    with pytest.raises(SystemExit) as result:
        smoke.main(argv)
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


@pytest.mark.parametrize("mode,dry_run,db_url", [
    ("dry", True, "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite"),
    ("live", False, "sqlite:////freqtrade/user_data/tradesv3.sqlite"),
])
def test_cli_set_mode_changes_private_file_without_clients(cli, tmp_path, monkeypatch, capsys,
                                                         mode, dry_run, db_url):
    path = tmp_path / "freqtrade/user_data/config.private.json"
    path.parent.mkdir(parents=True)
    original = {"exchange": {"key": "SECRET-SENTINEL"}, "custom": {"keep": True}}
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(smoke, "load_settings", lambda: pytest.fail("no settings or clients"))
    assert smoke.main(["set-mode", mode]) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        **original, "dry_run": dry_run, "db_url": db_url,
    }
    output = capsys.readouterr().out
    assert "설정" in output and ("모의" if dry_run else "실전") in output
    assert "docker compose -f freqtrade/docker-compose.yml restart freqtrade" in output
    assert "SECRET-SENTINEL" not in output


@pytest.mark.parametrize("dry_run,db_url", [
    (False, "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite"),
    (True, "sqlite:////freqtrade/user_data/tradesv3.sqlite"),
])
def test_cli_preflight_rejects_mode_database_mismatch(cli, monkeypatch, capsys, dry_run, db_url):
    settings, _, ft, _, _ = cli
    ft.dry_run = dry_run
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=dry_run, db_url=db_url))
    assert smoke.main(["preflight"]) == 1
    output = capsys.readouterr().out
    assert "모드" in output and "db_url" in output and "문제" in output
    assert "SECRET-SENTINEL" not in output


@pytest.mark.parametrize("contents", ['{"started_ms":1,"trade_id":7,"mode":"dry"}', "invalid"])
def test_cli_enter_refuses_any_existing_state(cli, tmp_path, capsys, contents):
    _, _, ft, _, _ = cli
    path = tmp_path / ".smoke_state.json"
    path.write_text(contents, encoding="utf-8")
    assert smoke.main(["enter"]) == 1
    output = capsys.readouterr().out
    assert "이전 스모크 상태 파일이 남아 있습니다. 먼저 exit으로 정리하세요" in output
    assert path.read_text(encoding="utf-8") == contents
    assert ft.calls == []
    assert EXIT_WARNING not in output


@pytest.mark.parametrize("cmd", ["check", "restart-check"])
@pytest.mark.parametrize("saved_mode", ["live", None])
def test_cli_check_commands_refuse_wrong_or_legacy_state_mode(cli, tmp_path, monkeypatch, capsys,
                                                            cmd, saved_mode):
    _, reader, ft, _, _ = cli
    state = {"started_ms": 1, "trade_id": 7}
    if saved_mode is not None:
        state["mode"] = saved_mode
    (tmp_path / ".smoke_state.json").write_text(json.dumps(state), encoding="utf-8")
    ft.trades = [dict(TRADE)]
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("no restart prompt on stale state"))
    assert smoke.main([cmd]) == 1
    assert "모드" in capsys.readouterr().out
    assert ft.calls == [] and reader.calls == []


@pytest.mark.parametrize("before,after", [(100, 100), (100, 99), (100, None), (None, 101)])
def test_cli_restart_rejects_unproven_restart(cli, tmp_path, monkeypatch, capsys, before, after):
    _, _, ft, _, t = cli
    ft.bot_startup_ts = before
    ft.trades = [dict(TRADE)]
    (tmp_path / ".smoke_state.json").write_text(json.dumps({
        "started_ms": 1, "trade_id": 7, "mode": "dry",
    }), encoding="utf-8")

    def no_restart(_):
        assert "health" in ft.calls
        ft.bot_startup_ts = after
        return ""

    monkeypatch.setattr("builtins.input", no_restart)
    assert smoke.main(["restart-check"]) == 1
    assert "재시작이 확인되지 않았습니다. 컨테이너를 재시작한 뒤 다시 실행하세요." in capsys.readouterr().out
    assert "start" not in ft.calls and t[0] == 0


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("mismatch", ["freqtrade", "state"])
def test_cli_exit_mode_mismatch_still_closes_without_exchange_claim(cli, tmp_path, monkeypatch,
                                                                  capsys, mismatch, dry_run):
    settings, reader, ft, _, _ = cli
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(settings, dry_run=dry_run))
    ft.dry_run = dry_run
    ft.trades = [dict(TRADE)]
    state = {"started_ms": 1, "trade_id": 7, "mode": "dry" if dry_run else "live"}
    if mismatch == "freqtrade":
        ft.dry_run = not dry_run
    else:
        state["mode"] = "live" if dry_run else "dry"
    (tmp_path / ".smoke_state.json").write_text(json.dumps(state), encoding="utf-8")
    assert smoke.main(["exit"]) == 1
    assert [call for call in ft.calls if isinstance(call, tuple)] == [("exit", ("all", "market"))]
    output = capsys.readouterr().out
    assert "경고" in output and "모드" in output and "비트겟 웹(PC)" in output
    assert "거래소 포지션 없음" not in output
    assert reader.calls == []
    assert (tmp_path / ".smoke_state.json").exists()


@pytest.mark.parametrize("raises", [True, False])
def test_cli_exit_ledger_failure_reports_closed_without_manual_close(cli, tmp_path, monkeypatch,
                                                                   capsys, raises):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.position = None
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(
        settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite",
    ))
    state = {"started_ms": 1, "trade_id": 7, "mode": "live"}
    path = tmp_path / ".smoke_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    def ledger_failure(*_):
        if raises:
            raise OSError("SECRET-SENTINEL")
        return 1

    monkeypatch.setattr(smoke, "_ledger", ledger_failure)
    assert smoke.main(["exit"]) == 1
    output = capsys.readouterr().out
    assert "청산은 완료됐습니다. 장부 저장에 실패했으니 'python -m bitgetbot.smoke ledger'를 다시 실행하세요." in output
    assert "시장가 청산하세요" not in output and "SECRET-SENTINEL" not in output
    assert json.loads(path.read_text(encoding="utf-8")) == {**state, "close_confirmed": True}


@pytest.mark.parametrize("failure", ["interrupt", "eof", "preflight", "stake", "save"])
def test_cli_enter_preorder_errors_do_not_print_exit_warning(cli, tmp_path, monkeypatch, capsys, failure):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(
        settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite",
    ))

    def prompt(_):
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "eof":
            raise EOFError("SECRET-SENTINEL")
        return "LIVE"

    def fail(*_):
        raise RuntimeError("SECRET-SENTINEL")

    mark = reader.fetch_mark_price
    queries = [0]

    def mark_failure(symbol):
        queries[0] += 1
        if queries[0] == (1 if failure == "preflight" else 2):
            fail()
        return mark(symbol)

    monkeypatch.setattr("builtins.input", prompt)
    if failure in {"preflight", "stake"}:
        monkeypatch.setattr(reader, "fetch_mark_price", mark_failure)
    if failure == "save":
        monkeypatch.setattr(smoke, "_save_state", fail)
    assert smoke.main(["enter"]) == 1
    output = capsys.readouterr().out
    assert "명령 실패" in output
    assert EXIT_WARNING not in output and "SECRET-SENTINEL" not in output
    assert not any(isinstance(call, tuple) for call in ft.calls)
    assert not (tmp_path / ".smoke_state.json").exists()


@pytest.mark.parametrize("last_config", [
    {"state": "running", "dry_run": False},
    {"state": "stopped", "dry_run": True},
    {"state": "running"},
])
def test_cli_enter_rechecks_running_and_mode_together_before_order(cli, monkeypatch, tmp_path,
                                                                 capsys, last_config):
    _, _, ft, _, _ = cli
    ft.state = "running"
    config = ft.show_config
    queries = [0]

    def changed_config():
        queries[0] += 1
        return config() if queries[0] == 1 else last_config

    monkeypatch.setattr(ft, "show_config", changed_config)
    assert smoke.main(["enter"]) == 1
    assert queries[0] == 2
    assert not any(isinstance(call, tuple) for call in ft.calls)
    assert not (tmp_path / ".smoke_state.json").exists()
    assert "주문하지 않습니다" in capsys.readouterr().out


@pytest.mark.parametrize("source,error", [
    ("freqtrade", FreqtradeError(503, "SECRET-SENTINEL")),
    ("freqtrade", FreqtradeUnreachable("SECRET-SENTINEL")),
    ("bitget", RuntimeError("SECRET-SENTINEL")),
])
@pytest.mark.parametrize("recovers", [True, False])
def test_cli_exit_polls_through_transient_read_errors(cli, live_fills, monkeypatch, tmp_path, capsys,
                                                    source, error, recovers):
    settings, reader, ft, _, t = cli
    ft.dry_run = False
    reader.position = None
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(
        settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite",
    ))
    (tmp_path / ".smoke_state.json").write_text(json.dumps({
        "started_ms": 1, "trade_id": 7, "mode": "live",
    }), encoding="utf-8")
    reads = [0]
    original = ft.status if source == "freqtrade" else reader.fetch_position

    def flaky(*args):
        reads[0] += 1
        if not recovers or reads[0] == 1:
            raise error
        return original(*args)

    monkeypatch.setattr(ft if source == "freqtrade" else reader,
                        "status" if source == "freqtrade" else "fetch_position", flaky)
    assert smoke.main(["exit"]) == (0 if recovers else 1)
    assert t[0] == (5 if recovers else 60)
    assert [call for call in ft.calls if isinstance(call, tuple)] == [("exit", (7, "market"))]
    assert "SECRET-SENTINEL" not in capsys.readouterr().out
    assert (tmp_path / ".smoke_state.json").exists() is (not recovers)


@pytest.mark.parametrize("command", ["ledger", "exit"])
def test_cli_empty_live_fills_fail_without_losing_ledger_start(cli, monkeypatch, tmp_path,
                                                            capsys, command):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.position = None
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(
        settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite",
    ))
    path = tmp_path / ".smoke_state.json"
    state = {"started_ms": 1, "trade_id": 7, "mode": "live"}
    path.write_text(json.dumps(state), encoding="utf-8")
    assert smoke.main([command]) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {
        **state, **({"close_confirmed": True} if command == "exit" else {}),
    }
    output = capsys.readouterr().out
    assert "체결이 없습니다" in output
    assert "시장가 청산하세요" not in output


def test_cli_live_enter_saves_live_mode_before_single_order(cli, monkeypatch, tmp_path, capsys):
    settings, _, ft, _, _ = cli
    ft.dry_run = False
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(
        settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite",
    ))
    monkeypatch.setattr("builtins.input", lambda _: "LIVE")
    original = ft.force_enter

    def order(*args):
        state = json.loads((tmp_path / ".smoke_state.json").read_text(encoding="utf-8"))
        assert state["mode"] == "live" and state["trade_id"] is None
        return original(*args)

    monkeypatch.setattr(ft, "force_enter", order)
    assert smoke.main(["enter"]) == 0
    state = json.loads((tmp_path / ".smoke_state.json").read_text(encoding="utf-8"))
    assert state["mode"] == "live" and state["trade_id"] == 7
    assert len([call for call in ft.calls if isinstance(call, tuple) and call[0] == "enter"]) == 1
    assert "SECRET-SENTINEL" not in capsys.readouterr().out


def test_cli_ledger_retry_cleans_closed_state_without_another_exit(cli, live_fills, monkeypatch,
                                                                tmp_path, capsys):
    settings, reader, ft, _, _ = cli
    ft.dry_run = False
    reader.position = None
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(
        settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite",
    ))
    path = tmp_path / ".smoke_state.json"
    path.write_text(json.dumps({"started_ms": 1, "trade_id": 7, "mode": "live"}), encoding="utf-8")

    def unavailable_fx():
        raise RuntimeError("SECRET-SENTINEL")

    monkeypatch.setattr(smoke, "fetch_krw_per_usdt", unavailable_fx)
    assert smoke.main(["exit"]) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["close_confirmed"] is True
    monkeypatch.setattr(smoke, "fetch_krw_per_usdt", lambda: (1390.1, "2026-10-19T10:00:00Z"))
    assert smoke.main(["ledger"]) == 0
    assert not path.exists()
    assert [call for call in ft.calls if isinstance(call, tuple)] == [("exit", (7, "market"))]
    assert "SECRET-SENTINEL" not in capsys.readouterr().out


@pytest.mark.parametrize("saved_mode", ["dry", None])
def test_cli_live_ledger_refuses_stale_state_mode(cli, monkeypatch, tmp_path, capsys, saved_mode):
    settings, reader, _, _, _ = cli
    monkeypatch.setattr(smoke, "load_settings", lambda: replace(
        settings, dry_run=False, db_url="sqlite:////freqtrade/user_data/tradesv3.sqlite",
    ))
    state = {"started_ms": 1, "trade_id": 7, "close_confirmed": True}
    if saved_mode is not None:
        state["mode"] = saved_mode
    path = tmp_path / ".smoke_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert smoke.main(["ledger"]) == 1
    assert reader.calls == []
    assert "모드" in capsys.readouterr().out
    assert path.exists()
