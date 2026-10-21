"""GET /api/widget/summary (contract v1) and the persistent widget token. Fakes only."""

import base64
import json
import logging
import os
import re
import socket
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bitgetbot.ftclient import FreqtradeUnreachable
from bitgetbot.models import PlanOrder, PositionSnapshot
from bitgetbot.panel import charts, widget, widget_token
from tests.panel.test_app import (  # noqa: F401 - `panel` is a fixture
    PANEL_HEADER, SENTINELS, TOKEN, Reader, local_login, panel, refuse_connection, tailnet_login,
)

WIDGET = "WIDGET-TOKEN-SENTINEL-NOT-FOR-OUTPUT-0123456789"
OTHER = "OTHER-WIDGET-TOKEN-SENTINEL-NOT-FOR-OUTPUT-012"
PATH = "/api/widget/summary"
FIXTURE = Path(__file__).with_name("fixtures") / "trading-summary.json"
NOW = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc).timestamp()
TODAY = "2026-10-21"
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
SAFE_TRADE = {
    "pair": "BTC/USDT:USDT", "is_short": False, "enter_tag": "safe_trend", "leverage": 2,
    "open_rate": 100000, "current_rate": 101000, "stop_loss_abs": 97000, "liquidation_price": 50000,
    "stake_amount": 50, "profit_abs": 0.95, "profit_ratio": 0.019, "note": SENTINELS[3],
}
FORBIDDEN = SENTINELS + [WIDGET, OTHER]


def strict_json(text: str):
    def refuse(name):
        raise AssertionError(f"non-finite JSON constant {name}")
    return json.loads(text, parse_constant=refuse)


@pytest.fixture
def w(panel):
    assert len(WIDGET) >= 43 and len(OTHER) >= 43
    panel.now[0] = NOW
    panel.token_path = panel.deps.pin_store_path.with_name("widget_token")
    panel.token_path.parent.mkdir(parents=True, exist_ok=True)
    panel.token_path.write_text(WIDGET + "\n", encoding="utf-8")
    panel.ft.equity = 102.0
    panel.ft.daily = lambda timescale=1: {"data": [
        {"date": TODAY, "abs_profit": 2.0, "starting_balance": 100.0},
        {"date": "2026-10-20", "abs_profit": -1.0, "starting_balance": 101.0},
    ]}
    panel.ft.profit = lambda: {"closed_trade_count": 3, "profit_closed_coin": 12.0, "note": SENTINELS[3]}
    return panel


def get(panel, token=WIDGET, client="local", **headers):
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return getattr(panel, client).get(PATH, headers=headers)


def go_live(panel):
    panel.settings[0] = replace(panel.settings[0], dry_run=False)
    panel.ft.dry = False


def assert_clean(text: str):
    assert all(value not in text for value in FORBIDDEN)


# --- authentication -------------------------------------------------------------------------

def test_valid_bearer_works_locally_and_over_tailnet_without_session(w):
    for client in ["local", "tailnet"]:
        response = get(w, client=client)
        assert response.status_code == 200
        assert response.json()["v"] == 1
        assert "set-cookie" not in response.headers
        assert_clean(response.text + str(response.headers))


@pytest.mark.parametrize("authorization", [
    None, "", "Bearer", "Bearer ", f"Basic {WIDGET}", f"Bearer {WIDGET}x", f"Bearer {WIDGET[:-1]}",
    f"Bearer {OTHER}", f"Bearer {TOKEN}", "Bearer 한글".encode("utf-8"), f"{WIDGET}",
])
def test_wrong_or_missing_token_is_401_with_generic_message(w, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = w.local.get(PATH, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "위젯 토큰이 올바르지 않습니다"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert_clean(response.text + str(response.headers))


def test_bearer_scheme_is_case_insensitive(w):
    assert w.local.get(PATH, headers={"Authorization": f"bearer {WIDGET}"}).status_code == 200


@pytest.mark.parametrize("content", [None, "", "short", "has space " + WIDGET, WIDGET + "!"])
def test_missing_or_malformed_token_file_rejects_everything(w, content):
    if content is None:
        w.token_path.unlink()
    else:
        w.token_path.write_text(content, encoding="utf-8")
    assert get(w).status_code == 401
    assert get(w, token=content or "x").status_code == 401


def test_panel_session_and_launch_token_do_not_open_the_widget_route(w):
    local_login(w)
    assert w.local.get("/api/status").status_code == 200
    assert get(w, token=None).status_code == 401
    assert get(w, token=TOKEN).status_code == 401


def test_widget_token_is_accepted_by_no_other_route(w):
    bearer = {"Authorization": f"Bearer {WIDGET}"}
    for client in [w.local, w.tailnet]:
        for path in ["/api/status", "/api/job", "/api/charts"]:
            assert client.get(path, headers=bearer).status_code == 401
        for path in ["/api/emergency/close-all", "/api/autotrade", "/api/mode", "/api/steps/enter", "/api/keys"]:
            assert client.post(path, json={"action": "start"}, headers={**bearer, **PANEL_HEADER}).status_code == 401
    response = w.local.get("/", params={"t": WIDGET}, follow_redirects=False)
    assert response.status_code == 200 and "set-cookie" not in response.headers
    assert w.local.get("/api/status").status_code == 401
    assert w.ft.calls == []
    assert w.local.post(PATH, headers={**bearer, **PANEL_HEADER}).status_code == 405


def test_foreign_host_is_rejected_even_with_the_widget_token(w):
    with TestClient(w.app, base_url="http://evil.example:8090", client=("127.0.0.1", 12360)) as evil:
        response = evil.get(PATH, headers={"Authorization": f"Bearer {WIDGET}"})
    assert response.status_code == 421


def test_failures_lock_only_the_widget_route_and_never_the_panel(w):
    for _ in range(5):
        assert get(w, token=OTHER).status_code == 401
    locked = get(w)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) == 900
    assert_clean(locked.text)
    # The panel itself is never locked by widget failures.
    local_login(w)
    assert w.local.get("/api/status").status_code == 200
    tailnet_login(w)
    assert w.tailnet.get("/api/status").status_code == 200
    w.now[0] += 900
    assert get(w).status_code == 200


def test_lock_escalates_and_a_valid_request_resets_the_count(w):
    for _ in range(4):
        assert get(w, token=OTHER).status_code == 401
    assert get(w).status_code == 200
    for _ in range(4):
        assert get(w, token=OTHER).status_code == 401
    assert get(w).status_code == 200
    for _ in range(5):
        get(w, token=OTHER)
    w.now[0] += 900
    for _ in range(5):
        get(w, token=OTHER)
    assert int(get(w).headers["retry-after"]) == 1800


def test_pin_lockout_does_not_lock_the_widget(w):
    w.deps.pin_store_path.write_text(json.dumps({"pin_hash": "scrypt$00$00"}), encoding="utf-8")
    for _ in range(6):
        w.tailnet.post("/api/login", json={"pin": "000000"}, headers=PANEL_HEADER)
    assert get(w).status_code == 200


def test_rotated_token_applies_without_restart(w):
    from bitgetbot.panel import __main__ as entry

    assert get(w).status_code == 200
    entry._write_token(w.token_path, OTHER)
    assert get(w).status_code == 401
    assert get(w, token=OTHER).status_code == 200


# --- ETag / seq / cache ---------------------------------------------------------------------

def test_etag_is_quoted_seq_and_if_none_match_gives_empty_304(w):
    first = get(w)
    seq = first.json()["seq"]
    assert type(seq) is int
    assert first.headers["etag"] == f'"{seq}"'
    for value in [f'"{seq}"', f'W/"{seq}"', f'"1", "{seq}"', "*"]:
        response = get(w, **{"If-None-Match": value})
        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["etag"] == f'"{seq}"'
    for value in ['"1"', str(seq), f'"{seq + 1}"']:
        assert get(w, **{"If-None-Match": value}).status_code == 200


def test_304_still_requires_the_token(w):
    seq = get(w).json()["seq"]
    response = get(w, token=OTHER, **{"If-None-Match": f'"{seq}"'})
    assert response.status_code == 401


def test_seq_changes_only_when_content_changes(w):
    first = get(w).json()
    w.now[0] += 3
    same = get(w)
    assert same.json()["seq"] == first["seq"]
    assert same.json()["updatedAt"] != first["updatedAt"]
    assert get(w, **{"If-None-Match": f'"{first["seq"]}"'}).status_code == 304
    w.ft.equity = 110.0
    w.now[0] += 3
    changed = get(w, **{"If-None-Match": f'"{first["seq"]}"'})
    assert changed.status_code == 200
    assert changed.json()["seq"] == first["seq"] + 1
    assert changed.json()["equity"]["usdt"] == 110.0


def test_seq_restarts_above_any_earlier_value(w):
    from bitgetbot.panel.app import create_app

    before = get(w).json()["seq"]
    w.now[0] += 60
    with TestClient(create_app(w.deps), base_url="http://127.0.0.1:8090", client=("127.0.0.1", 12361)) as again:
        after = again.get(PATH, headers={"Authorization": f"Bearer {WIDGET}"}).json()["seq"]
    assert after > before


def test_summary_is_built_at_most_once_per_3_seconds(w, monkeypatch):
    go_live(w)
    reads = {"equity": 0, "positions": 0}

    def equity(self):
        reads["equity"] += 1
        return 120.0

    def positions(self):
        reads["positions"] += 1
        return []

    monkeypatch.setattr(Reader, "fetch_usdt_equity", equity)
    monkeypatch.setattr(Reader, "fetch_positions", positions)
    for _ in range(5):
        assert get(w).status_code == 200
    assert reads == {"equity": 1, "positions": 1} and len(w.reader_calls) == 1
    w.now[0] += 2.9
    get(w, client="tailnet")
    assert reads == {"equity": 1, "positions": 1}
    w.now[0] += 0.1
    get(w)
    assert reads == {"equity": 2, "positions": 2}


def test_build_failure_is_generic_503_and_also_rate_limited(w):
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError(SENTINELS[3])

    w.deps.settings_loader = broken
    for _ in range(3):
        response = get(w)
        assert response.status_code == 503
        assert_clean(response.text)
    assert len(calls) == 1
    w.now[0] += 3
    get(w)
    assert len(calls) == 2


# --- contract shape and mapping -------------------------------------------------------------

def test_shape_matches_the_robo_briefing_fixture(w):
    w.ft.open_trades = [SAFE_TRADE]
    body = strict_json(get(w).text)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert list(body) == list(fixture)
    for key in ["bot", "equity", "pnl"]:
        assert set(body[key]) == set(fixture[key])
    assert set(body["bot"]["aggressive"]) == set(fixture["bot"]["aggressive"])
    assert set(body["positions"][0]) == set(fixture["positions"][0]) | {"takeProfit"}
    assert all(set(point) == {"t", "krw"} for point in body["spark"])
    assert all(set(item) == {"level", "text"} for item in body["alerts"])
    assert body["v"] == 1 and ISO.fullmatch(body["updatedAt"])
    assert body["mode"] in {"dry_run", "live"}
    assert body["bot"]["state"] in {"running", "paused", "close_only", "stopped"}
    assert body["bot"]["aggressive"] == {"armed": False, "budgetKrw": 0, "lossThisWeekKrw": 0}
    assert body["pending"] == [] and body["ai"] is None
    numbers = [*body["equity"].values(), *body["pnl"].values(), *body["positions"][0].values()]
    assert all(value is None or isinstance(value, (int, float, str)) for value in numbers)


def test_dry_summary_values(w):
    w.ft.open_trades = [SAFE_TRADE, {**SAFE_TRADE, "is_short": True, "enter_tag": "smoke",
                                     "stop_loss_abs": None, "profit_ratio": None}]
    body = get(w).json()
    assert body["mode"] == "dry_run"
    assert body["bot"] == {"state": "running", "reason": None,
                           "aggressive": {"armed": False, "budgetKrw": 0, "lossThisWeekKrw": 0}}
    assert body["equity"] == {"krw": 102.0 * 1400, "usdt": 102.0, "krwPerUsdt": 1400.0,
                              "fxAt": "2026-10-21T09:30:00Z"}
    assert body["pnl"] == {
        "todayKrw": 2800.0, "todayUsdt": 2.0, "todayPct": pytest.approx(2.0),
        "totalKrw": 16800.0, "totalUsdt": 12.0, "totalPct": pytest.approx(12 / 90 * 100),
    }
    assert body["positions"][0] == {
        "symbol": "BTCUSDT", "side": "long", "mode": "safe", "leverage": 2.0, "entry": 100000.0,
        "mark": 101000.0, "stopLoss": 97000.0, "takeProfit": None, "liqPrice": 50000.0,
        "marginUsdt": 50.0, "uPnlUsdt": 0.95, "uPnlPct": pytest.approx(1.9),
    }
    second = body["positions"][1]
    assert (second["side"], second["mode"], second["stopLoss"], second["uPnlPct"]) == ("short", "manual", None, None)
    # A dry position without a stop is not an exchange-stop alert (no exchange orders in dry mode).
    assert body["alerts"] == []


@pytest.mark.parametrize("ft_state,state,reason", [
    ("running", "running", None),
    ("paused", "paused", "새 진입 멈춤 · 열린 포지션은 계속 관리"),
    ("stopped", "stopped", "거래 봇 중지됨"),
    ("reload_config", "paused", "거래 봇 설정 다시 읽는 중"),
])
def test_bot_state_mapping(w, ft_state, state, reason):
    w.ft.state = ft_state
    body = get(w).json()
    assert (body["bot"]["state"], body["bot"]["reason"]) == (state, reason)
    assert not [item for item in body["alerts"] if item["level"] == "critical"]


def test_unreachable_bot_is_stopped_with_critical_alert(w):
    w.ft.ping_error = FreqtradeUnreachable(SENTINELS[3])
    body = get(w).json()
    assert body["bot"]["state"] == "stopped"
    assert body["bot"]["reason"] == "거래 봇에 연결할 수 없습니다"
    assert {"level": "critical", "text": "거래 봇에 연결할 수 없습니다"} in body["alerts"]
    assert not any("Docker" in item["text"] for item in body["alerts"])
    assert body["pnl"]["todayUsdt"] is None and body["positions"] == [] and body["equity"]["usdt"] is None
    assert {"level": "warn", "text": "자산을 읽지 못했습니다"} in body["alerts"]
    assert body["alerts"][0]["level"] == "critical"


def test_docker_down_is_a_critical_alert(w):
    w.ft.ping_error = FreqtradeUnreachable(SENTINELS[3])
    w.deps.runner = lambda argv, timeout: (1, SENTINELS[3])
    body = get(w).json()
    assert body["bot"]["reason"] == "Docker가 꺼져 있습니다"
    assert {"level": "critical", "text": "Docker가 꺼져 있습니다"} in body["alerts"]
    assert {"level": "critical", "text": "거래 봇에 연결할 수 없습니다"} in body["alerts"]


def test_docker_is_probed_only_when_the_bot_is_unreachable(w):
    probes = []
    w.deps.runner = lambda argv, timeout: probes.append(argv) or (0, "")
    get(w)
    assert probes == []


def test_unreadable_config_and_mode_mismatch_are_critical(w):
    w.ft.config_error = RuntimeError(SENTINELS[3])
    body = get(w).json()
    assert body["bot"]["state"] == "stopped"
    assert {"level": "critical", "text": "거래 봇 상태를 확인할 수 없습니다"} in body["alerts"]
    w.ft.config_error = None
    w.ft.dry = False
    w.ft.open_trades = [SAFE_TRADE]
    w.now[0] += 3
    body = get(w).json()
    assert {"level": "critical", "text": "거래 봇 모드가 저장된 모드와 다릅니다"} in body["alerts"]
    # The other mode's database must not show up as this mode's P&L or positions.
    assert body["pnl"]["totalUsdt"] is None and body["positions"] == []


def test_equity_and_fx_unavailable_are_warnings_with_null_krw(w):
    def fail():
        raise RuntimeError(SENTINELS[3])

    w.deps.fx = charts.FxCache(fail, w.deps.clock)
    body = get(w).json()
    assert body["equity"] == {"krw": None, "usdt": 102.0, "krwPerUsdt": None, "fxAt": None}
    assert body["pnl"]["todayKrw"] is None and body["pnl"]["todayUsdt"] == 2.0
    assert body["spark"] == []
    assert body["alerts"] == [{"level": "warn", "text": "환율을 받지 못해 원화를 표시하지 못합니다"}]


@pytest.mark.parametrize("daily,profit", [
    ({"data": [{"date": TODAY, "abs_profit": float("nan")}]}, {"profit_closed_coin": float("inf")}),
    ({"data": [{"date": "2026-10-20", "abs_profit": 5.0}]}, {"profit_closed_coin": "bad"}),
    ({"data": "bad"}, ["bad"]),
])
def test_non_finite_or_missing_pnl_is_null(w, daily, profit):
    w.ft.daily = lambda timescale=1: daily
    w.ft.profit = lambda: profit
    response = get(w)
    body = strict_json(response.text)
    assert {key: body["pnl"][key] for key in ["todayUsdt", "todayPct", "totalUsdt", "totalPct"]} == {
        "todayUsdt": None, "todayPct": None, "totalUsdt": None, "totalPct": None}


def test_pct_is_null_when_period_start_equity_is_not_positive(w):
    w.ft.equity = 2.0
    body = get(w).json()
    assert body["pnl"]["todayPct"] is None and body["pnl"]["totalPct"] is None


def test_clean_replaces_every_non_finite_float():
    value = {"a": [float("nan"), 1.5, {"b": float("-inf")}], "c": float("inf"), "d": "nan", "e": 0}
    assert widget.clean(value) == {"a": [None, 1.5, {"b": None}], "c": None, "d": "nan", "e": 0}


def test_spark_is_last_7_days_at_most_48_points_in_krw(w):
    log = w.deps.pin_store_path.with_name("equity.jsonl")
    lines = [{"ts": int(NOW - 10 * 86400), "mode": "dry", "equity_usdt": 1.0},
             {"ts": int(NOW - 5 * 86400), "mode": "live", "equity_usdt": 999.0}]
    lines += [{"ts": int(NOW - 7 * 86400 + 600 + i * 3000), "mode": "dry", "equity_usdt": 100.0 + i}
              for i in range(200)]
    log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    spark = get(w).json()["spark"]
    assert 2 <= len(spark) <= 48
    stamps = [datetime.strptime(point["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
              for point in spark]
    assert stamps == sorted(stamps) and stamps[0] >= NOW - 7 * 86400
    assert spark[0]["krw"] == 100.0 * 1400
    # The newest point is kept: the widget just recorded the current equity (102 USDT).
    assert spark[-1] == {"t": "2026-10-21T09:30:00Z", "krw": 102.0 * 1400}
    assert all(point["krw"] != 999.0 * 1400 for point in spark)


def test_live_positions_use_exchange_stops_and_freqtrade_tags(w, monkeypatch):
    go_live(w)
    w.ft.open_trades = [SAFE_TRADE]
    stops = {"BTCUSDT": [PlanOrder("BTCUSDT", "pos_loss", 96000.0, "mark_price", None, "1"),
                         PlanOrder("BTCUSDT", "loss_plan", 95000.0, "mark_price", 0.001, "2")],
             "ETHUSDT": []}
    monkeypatch.setattr(Reader, "fetch_positions", lambda self: [
        PositionSnapshot("BTCUSDT", "long", 0.001, 100000.0, 101000.0, 50000.0, 2.0, "isolated",
                         margin=50.5, unrealized_pnl=1.01),
        PositionSnapshot("ETHUSDT", "short", 0.1, 3000.0, 2990.0, 4500.0, 5.0, "isolated"),
    ])
    monkeypatch.setattr(Reader, "fetch_stop_orders", lambda self, symbol: stops[symbol])
    body = get(w).json()
    assert body["mode"] == "live"
    btc, eth = body["positions"]
    assert btc == {"symbol": "BTCUSDT", "side": "long", "mode": "safe", "leverage": 2.0, "entry": 100000.0,
                   "mark": 101000.0, "stopLoss": 95000.0, "takeProfit": None, "liqPrice": 50000.0,
                   "marginUsdt": 50.5, "uPnlUsdt": 1.01, "uPnlPct": pytest.approx(2.0)}
    # Unknown to Freqtrade: manual. Margin and PnL fall back to entry * size / leverage and the mark.
    assert eth["mode"] == "manual" and eth["stopLoss"] is None
    assert eth["marginUsdt"] == pytest.approx(60.0) and eth["uPnlUsdt"] == pytest.approx(1.0)
    assert eth["uPnlPct"] == pytest.approx(1.0 / 60 * 100)
    assert body["alerts"] == [{"level": "critical", "text": "ETHUSDT 거래소 손절 주문이 없습니다"}]
    assert body["equity"]["usdt"] == 114.25
    assert w.reader_calls == [tuple(SENTINELS[2:5])]
    assert_clean(json.dumps(body, ensure_ascii=False))


def test_live_unreadable_stops_or_positions_are_critical(w, monkeypatch):
    go_live(w)
    monkeypatch.setattr(Reader, "fetch_positions", lambda self: [
        PositionSnapshot("BTCUSDT", "long", 0.001, 100000.0, 101000.0, None, 2.0, "isolated")])

    def broken(self, symbol):
        raise RuntimeError(SENTINELS[3])

    monkeypatch.setattr(Reader, "fetch_stop_orders", broken)
    body = get(w).json()
    assert body["positions"][0]["stopLoss"] is None
    assert body["alerts"] == [{"level": "critical", "text": "BTCUSDT 거래소 손절을 확인할 수 없습니다"}]

    def no_positions(self):
        raise RuntimeError(SENTINELS[3])

    monkeypatch.setattr(Reader, "fetch_positions", no_positions)
    w.now[0] += 3
    response = get(w)
    assert response.json()["positions"] == []
    assert {"level": "critical", "text": "실전 포지션을 확인할 수 없습니다"} in response.json()["alerts"]
    assert_clean(response.text)


def test_live_equity_failure_is_a_warning(w, monkeypatch):
    go_live(w)
    monkeypatch.setattr(Reader, "equity", None)
    body = get(w).json()
    assert body["equity"]["usdt"] is None and body["pnl"]["todayPct"] is None
    assert {"level": "warn", "text": "자산을 읽지 못했습니다"} in body["alerts"]


def test_dry_mode_never_reads_private_exchange_data(w):
    get(w)
    assert w.reader_calls == [("", "", "")]


# --- secrets ----------------------------------------------------------------------------------

def test_no_secret_in_responses_audit_or_logs(w, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    w.ft.open_trades = [SAFE_TRADE]
    texts = []
    for token in [WIDGET, OTHER, None, TOKEN]:
        response = get(w, token=token)
        texts.append(response.text + str(response.headers))
    captured = capsys.readouterr()
    for text in [*texts, caplog.text, captured.out, captured.err]:
        assert_clean(text)
    # Widget reads leave no audit line; a later panel action's line carries no token either.
    assert not w.deps.audit_path.exists()
    local_login(w)
    w.local.post("/api/autotrade", json={"action": "pause"}, headers=PANEL_HEADER)
    audit = w.deps.audit_path.read_text(encoding="utf-8")
    assert "widget" not in audit
    assert_clean(audit)


def test_widget_requests_never_change_bot_state(w):
    w.ft.open_trades = [SAFE_TRADE]
    for _ in range(3):
        get(w)
        w.now[0] += 3
    assert w.ft.calls == []
    assert w.modes == [] and w.runner_calls == []


# --- token file and CLI -----------------------------------------------------------------------

def test_generated_token_is_32_random_bytes_url_safe():
    tokens = {widget_token.generate() for _ in range(20)}
    assert len(tokens) == 20
    for token in tokens:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        assert len(base64.urlsafe_b64decode(token + "=")) == 32


def test_ensure_creates_once_and_replaces_only_malformed(tmp_path):
    from bitgetbot.panel import __main__ as entry

    path = tmp_path / ".panel" / "widget_token"
    assert widget_token.ensure(path, entry._write_token) is True
    first = widget_token.read(path)
    assert first is not None
    assert widget_token.ensure(path, entry._write_token) is False
    assert widget_token.read(path) == first
    path.write_text("bad token", encoding="utf-8")
    assert widget_token.ensure(path, entry._write_token) is True
    assert widget_token.read(path) not in {None, first}


def test_file_token_follows_the_file(tmp_path):
    from bitgetbot.panel import __main__ as entry

    path = tmp_path / "widget_token"
    reader = widget_token.FileToken(path)
    assert reader() is None
    entry._write_token(path, WIDGET)
    assert reader() == WIDGET
    entry._write_token(path, OTHER)
    assert reader() == OTHER
    path.unlink()
    assert reader() is None


def test_widget_token_file_has_owner_only_permissions(tmp_path, monkeypatch):
    from bitgetbot.panel import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(socket, "create_connection", refuse_connection)
    monkeypatch.setattr(entry, "create_app", lambda deps: "fake-app")
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: None)
    assert entry.main([]) == 0
    assert_owner_only(tmp_path / ".panel" / "widget_token")


def test_launch_permission_failure_writes_no_widget_token(tmp_path, monkeypatch):
    from bitgetbot.panel import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(socket, "create_connection", refuse_connection)

    def refuse(path):
        raise OSError(WIDGET)

    monkeypatch.setattr(entry, "_restrict_to_owner", refuse)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: pytest.fail("must not start"))
    assert entry.main([]) == 1
    assert not (tmp_path / ".panel" / "widget_token").exists()


def test_rotate_cli_prints_nothing_secret(tmp_path, monkeypatch, capsys):
    from bitgetbot.panel import __main__ as entry, ops

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    path = tmp_path / ".panel" / "widget_token"
    entry._write_token(path, WIDGET)
    assert widget_token.main(["rotate"]) == 0
    new = widget_token.read(path)
    assert new not in {None, WIDGET}
    captured = capsys.readouterr()
    assert captured.out == "새 위젯 토큰을 만들었습니다\n" and captured.err == ""
    assert_owner_only(path)
    assert widget_token.main(["rotate"]) == 0
    newer = widget_token.read(path)
    assert newer != new
    output = capsys.readouterr()
    assert new not in output.out + output.err and newer not in output.out + output.err


@pytest.mark.parametrize("argv", [[], ["rotate", "now"], ["show"], ["--print"]])
def test_rotate_cli_rejects_other_arguments(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    assert widget_token.main(argv) == 2
    assert not (tmp_path / ".panel").exists()
    assert "rotate" in capsys.readouterr().err


def test_rotate_cli_refuses_wrong_folder_and_permission_failure(tmp_path, monkeypatch, capsys):
    from bitgetbot.panel import __main__ as entry, ops

    monkeypatch.chdir(tmp_path)
    assert widget_token.main(["rotate"]) == 1
    assert not (tmp_path / ".panel").exists()
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)

    def refuse(path):
        raise OSError(WIDGET)

    monkeypatch.setattr(entry, "_restrict_to_owner", refuse)
    assert widget_token.main(["rotate"]) == 1
    assert not (tmp_path / ".panel" / "widget_token").exists()
    captured = capsys.readouterr()
    assert WIDGET not in captured.out + captured.err


def test_fx_cache_reports_fetch_time(tmp_path):
    now = [100.0]
    rates = [1400.0]

    def fetch():
        if rates[0] is None:
            raise RuntimeError("down")
        return rates[0], "ignored"

    cache = charts.FxCache(fetch, lambda: now[0])
    assert cache.rate_at() == (1400.0, 100.0)
    now[0] = 350.0
    assert cache.rate_at() == (1400.0, 100.0)
    now[0] = 401.0
    rates[0] = 1390.0
    assert cache.rate_at() == (1390.0, 401.0) and cache.rate() == 1390.0
    rates[0] = None
    now[0] = 2000.0
    assert cache.rate_at() == (None, None) and cache.rate() is None


def assert_owner_only(path: Path) -> None:
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        return
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi.GetFileSecurityW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD)]
    get_security.restype = wintypes.BOOL
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    size = wintypes.DWORD()
    get_security(str(path), 4, None, 0, ctypes.byref(size))
    descriptor = ctypes.create_string_buffer(size.value)
    assert get_security(str(path), 4, descriptor, size, ctypes.byref(size))
    output = ctypes.c_void_p()
    assert convert(descriptor, 1, 4, ctypes.byref(output), None)
    try:
        sddl = ctypes.wstring_at(output)
        assert sddl.startswith("D:P") and sddl.count("(") == 1 and ";FA;;;OW)" in sddl
    finally:
        kernel.LocalFree(output)
