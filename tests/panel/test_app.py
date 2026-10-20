import json
import os
import secrets
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from bitgetbot.ftclient import FreqtradeError, FreqtradeUnreachable
from bitgetbot.panel.auth import Lockout, Sessions, hash_pin, verify_pin
from bitgetbot.privcfg import Settings
from tests.panel.test_jobs import finished


TOKEN = "TOKEN-SENTINEL-NOT-FOR-OUTPUT"
PIN = "918273645"
HOST = "pc.tail1234.ts.net"
PANEL_HEADER = {"X-Panel-Request": "1"}
LIVE_PROMPT = "실전 주문을 냅니다. 계속하려면 LIVE 입력:"
SENTINELS = [TOKEN, PIN, "KEY-SENTINEL", "SECRET-SENTINEL", "PASS-SENTINEL", "API-PASS-SENTINEL", "API-USER-SENTINEL"]
MANUAL_LINK = "https://www.bitget.com/futures/usdt/BTCUSDT"


class FT:
    def __init__(self):
        self.calls = []
        self.error = None
        self.dry = True
        self.trades = []
        self.ping_error = None

    def ping(self):
        if self.ping_error:
            raise self.ping_error
        return True

    def show_config(self):
        return {"state": "running", "dry_run": self.dry, "api_server": {"password": SENTINELS[5]}}

    def status(self):
        return list(self.trades)

    def start(self):
        self.calls.append(("start",))
        if self.error:
            raise self.error
        return {"status": SENTINELS[3]}

    def pause(self):
        self.calls.append(("pause",))
        if self.error:
            raise self.error
        return {"status": SENTINELS[3]}

    def force_exit(self, trade_id, ordertype):
        self.calls.append((trade_id, ordertype))
        if self.error:
            raise self.error
        return {"secret": SENTINELS[3], "result": "ignored"}


class Reader:
    def account_mode_report(self):
        return {"v2_ok": True, "v3_ok": False, "v3_error": "40000"}

    def fetch_usdt_available(self):
        return 20

    def fetch_positions(self):
        return []

    def fetch_stop_orders(self, symbol):
        assert symbol == "BTCUSDT"
        return []


@pytest.fixture
def panel(tmp_path, monkeypatch):
    from bitgetbot.panel import app as module, ops
    from bitgetbot.panel.jobs import JobRunner

    def forbidden(*args, **kwargs):
        pytest.fail("Real processes and network are forbidden")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(ops, "TAILSCALE", "tailscale")
    monkeypatch.setattr(ops, "STATE_PATH", tmp_path / "smoke.json")
    now = [1000.0]
    settings = [Settings(True, "http://127.0.0.1:8080", SENTINELS[6], SENTINELS[5], *SENTINELS[2:5])]
    ft = FT()
    runner_calls, reader_calls, ft_calls, executions, modes, keys = [], [], [], [], [], []
    release, entered = Event(), Event()
    options = {"block": False, "restart_error": False, "container": True, "compose_code": 0, "execute_error": False}

    def runner(argv, timeout):
        assert timeout > 0
        runner_calls.append(list(argv))
        if argv == ["docker", "info"]:
            return 0, SENTINELS[2]
        if argv == ["docker", "compose", "-f", "freqtrade/docker-compose.yml", "ps", "--format", "json"]:
            return 0, json.dumps({"Service": "freqtrade", "State": "running" if options["container"] else "exited"})
        if argv[:4] == ["docker", "compose", "-f", "freqtrade/docker-compose.yml"]:
            if options["restart_error"]:
                raise RuntimeError("command text " + SENTINELS[3])
            return options["compose_code"], SENTINELS[3]
        if argv == ["tailscale", "status", "--json"]:
            return 0, json.dumps({"BackendState": "Running", "Self": {"DNSName": HOST + "."}})
        if argv in [["tailscale", "serve", "status", "--json"], ["tailscale", "funnel", "status", "--json"]]:
            return 0, json.dumps({"Web": {HOST + ":443": {}}})
        if argv == ["tailscale", "serve", "--bg", "8090"]:
            return 0, SENTINELS[3]
        pytest.fail("Unexpected command")

    def execute(name, io):
        executions.append((name, io.ask(LIVE_PROMPT), io.ask("unrecognized prompt")))
        entered.set()
        if options["block"]:
            assert release.wait(5)
        if name == "restart-check":
            io.restart()
        if options["execute_error"]:
            raise RuntimeError(" ".join(SENTINELS))
        io.out("완료")
        return 0

    def reader_factory(*credentials):
        reader_calls.append(credentials)
        return Reader()

    def ft_factory(*credentials):
        ft_calls.append(credentials)
        return ft

    def mode_setter(mode):
        modes.append(mode)
        settings[0] = replace(settings[0], dry_run=mode == "dry")

    deps = module.PanelDeps(
        token=TOKEN, settings_loader=lambda: settings[0], reader_factory=reader_factory,
        ft_factory=ft_factory, runner=runner, jobs=JobRunner(execute),
        sessions=Sessions(clock=lambda: now[0]), lockout=Lockout(clock=lambda: now[0]),
        pin_store_path=tmp_path / ".panel" / "panel.json", clock=lambda: now[0],
        mode_setter=mode_setter, keys_setter=lambda *values: keys.append(values),
        keys_configured=lambda: {"key": True, "secret": True, "password": True},
    )
    application = module.create_app(deps)
    with TestClient(application, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 12345)) as local:
        with TestClient(application, base_url="https://" + HOST, client=("127.0.0.1", 12346)) as tailnet:
            data = SimpleNamespace(
                module=module, app=application, deps=deps, local=local, tailnet=tailnet,
                settings=settings, ft=ft, runner_calls=runner_calls, reader_calls=reader_calls,
                ft_calls=ft_calls, executions=executions, modes=modes, keys=keys,
                options=options, now=now, release=release, entered=entered, state_path=ops.STATE_PATH,
            )
            yield data
            release.set()
            finished(deps.jobs)


def local_login(panel):
    response = panel.local.get("/", params={"t": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    return response


def tailnet_login(panel):
    panel.deps.pin_store_path.parent.mkdir(exist_ok=True)
    panel.deps.pin_store_path.write_text(json.dumps({"pin_hash": hash_pin(PIN)}), encoding="utf-8")
    response = panel.tailnet.post("/api/login", json={"pin": PIN}, headers=PANEL_HEADER)
    assert response.status_code == 200
    return response


def test_token_login_sets_local_session_and_redirects(panel):
    response = local_login(panel)
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "panel_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie
    status = panel.local.get("/api/status")
    assert status.status_code == 200
    assert status.json()["session_kind"] == "local"
    assert TOKEN not in str(response.headers) + response.text


@pytest.mark.parametrize("token", ["wrong", "", "한글", TOKEN + "x"])
def test_wrong_token_does_not_create_session(panel, token):
    response = panel.local.get("/", params={"t": token}, follow_redirects=False)
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert panel.local.get("/api/status").status_code == 401


def test_tailnet_token_and_proxy_headers_never_issue_local_session(panel):
    response = panel.tailnet.get("/", params={"t": TOKEN}, follow_redirects=False)
    assert response.status_code == 200 and "set-cookie" not in response.headers
    for name in ["Tailscale-User-Login", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded"]:
        response = panel.local.get("/", params={"t": TOKEN}, headers={name: "proxy"}, follow_redirects=False)
        assert "set-cookie" not in response.headers
    assert panel.local.post("/api/login", json={"pin": PIN}, headers=PANEL_HEADER).status_code == 403


def test_tailnet_requires_pin_and_status_401_without_session(panel):
    assert panel.tailnet.get("/api/ping").json() == {"ok": True}
    assert panel.tailnet.get("/api/status").status_code == 401
    assert panel.tailnet.get("/api/job").status_code == 401
    response = panel.tailnet.post("/api/login", json={"pin": PIN}, headers=PANEL_HEADER)
    assert response.status_code == 409
    assert "PC에서 PIN을 먼저 설정하세요" in response.text
    response = tailnet_login(panel)
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    status = panel.tailnet.get("/api/status")
    assert status.status_code == 200
    assert status.json()["session_kind"] == "tailnet"
    assert status.json().get("tailscale") is None
    assert not any(argv[0] == "tailscale" for argv in panel.runner_calls)


def test_login_lockout_returns_429_and_success_resets(panel):
    tailnet_login(panel)
    panel.tailnet.cookies.clear()
    for _ in range(5):
        assert panel.tailnet.post("/api/login", json={"pin": "000000"}, headers=PANEL_HEADER).status_code == 401
    response = panel.tailnet.post("/api/login", json={"pin": PIN}, headers=PANEL_HEADER)
    assert response.status_code == 429
    assert response.json()["retry_after"] == 900
    assert response.headers["retry-after"] == "900"
    panel.now[0] += 900
    assert panel.tailnet.post("/api/login", json={"pin": PIN}, headers=PANEL_HEADER).status_code == 200
    assert panel.deps.lockout.check("unknown") == 0
    assert panel.deps.lockout.check("*") == 0


def test_rotating_spoofed_identities_cannot_bypass_global_login_lockout(panel):
    tailnet_login(panel)
    for index in range(5):
        response = panel.local.post("/api/login", json={"pin": "000000"}, headers={
            **PANEL_HEADER, "Tailscale-User-Login": f"person-{index}",
        })
        assert response.status_code == 401
    response = panel.local.post("/api/login", json={"pin": PIN}, headers={
        **PANEL_HEADER, "Tailscale-User-Login": "person-5",
    })
    assert response.status_code == 429
    assert response.json()["retry_after"] == 900
    assert response.headers["retry-after"] == "900"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("valid", [False, True])
def test_login_reserves_both_attempts_once_before_verifying_and_never_fails_twice(panel, monkeypatch, valid):
    tailnet_login(panel)
    events = []

    class Reservation:
        def attempt(self, ident):
            events.append(("attempt", ident))
            return 0

        def success(self, ident):
            events.append(("success", ident))

        def fail(self, ident):
            pytest.fail("Reserved attempts must never call fail")

    def verify(pin, stored):
        events.append(("verify",))
        return valid

    panel.deps.lockout = Reservation()
    monkeypatch.setattr(panel.module, "verify_pin", verify)
    response = panel.tailnet.post("/api/login", json={"pin": PIN}, headers={**PANEL_HEADER, "Tailscale-User-Login": "person"})
    assert response.status_code == (200 if valid else 401)
    expected = [("attempt", "person"), ("attempt", "*"), ("verify",)]
    if valid:
        expected += [("success", "person"), ("success", "*")]
    assert events == expected


@pytest.mark.parametrize("identity_wait,global_wait,seconds", [
    (12.1, 1.2, 13), (1.2, 12.1, 13), (0, 1.2, 2), (1.2, 0, 2),
])
def test_login_reserves_both_buckets_and_returns_longest_lock_without_verifying(
    panel, monkeypatch, identity_wait, global_wait, seconds,
):
    tailnet_login(panel)
    events = []

    class Reservation:
        def attempt(self, ident):
            events.append(ident)
            return global_wait if ident == "*" else identity_wait

        def success(self, ident):
            events.append(("success", ident))

        def fail(self, ident):
            events.append(("fail", ident))

    def verify(pin, stored):
        events.append("verify")
        return True

    panel.deps.lockout = Reservation()
    monkeypatch.setattr(panel.module, "verify_pin", verify)
    response = panel.tailnet.post("/api/login", json={"pin": PIN}, headers={
        **PANEL_HEADER, "Tailscale-User-Login": "person",
    })
    assert response.status_code == 429
    assert response.json()["retry_after"] == seconds
    assert response.headers["retry-after"] == str(seconds)
    assert events == ["person", "*"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "UNKNOWN"])
def test_every_unsafe_api_method_requires_csrf_even_unknown_routes(panel, method):
    local_login(panel)
    response = panel.local.request(method, "/api/not-a-route")
    assert response.status_code == 403
    response = panel.local.request(method, "/api/ping", headers={**PANEL_HEADER, "Origin": "https://evil.invalid"})
    assert response.status_code == 403
    assert not any(name.lower().startswith("access-control-allow-") for name in response.headers)


def test_post_without_panel_header_is_403(panel):
    local_login(panel)
    assert panel.local.post("/api/steps/exit", json={}).status_code == 403
    assert not panel.executions


def test_post_with_foreign_origin_is_403(panel):
    local_login(panel)
    for origin in ["null", "https://evil.invalid", "https://127.0.0.1:8090"]:
        assert panel.local.post("/api/steps/exit", json={}, headers={**PANEL_HEADER, "Origin": origin}).status_code == 403
    assert not panel.executions
    assert panel.local.post("/api/docker/up", headers={**PANEL_HEADER, "Origin": "http://127.0.0.1:8090"}).status_code == 200


def test_tailnet_https_origin_works_behind_tls_termination(panel):
    tailnet_login(panel)
    response = panel.tailnet.post("/api/docker/up", headers={**PANEL_HEADER, "Origin": "https://" + HOST, "X-Forwarded-Proto": "https"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/status", "/api/job", "/api/not-a-route"])
def test_all_protected_api_paths_require_session(panel, path):
    assert panel.local.get(path).status_code == 401
    assert panel.local.post(path, headers=PANEL_HEADER).status_code == 401


def test_session_expires_and_local_cookie_cannot_elevate_proxy_requests(panel):
    local_login(panel)
    response = panel.local.post("/api/keys", json={"key": "k", "secret": "s", "password": "p"}, headers={**PANEL_HEADER, "Forwarded": "proxy"})
    assert response.status_code in {401, 403}
    assert not panel.keys
    panel.now[0] += 43200
    assert panel.local.get("/api/status").status_code == 401


@pytest.mark.parametrize("body", [{}, {"confirm": "live"}, {"confirm": " LIVE"}, {"confirm": True}])
def test_enter_live_requires_confirm(panel, body):
    local_login(panel)
    panel.settings[0] = replace(panel.settings[0], dry_run=False)
    response = panel.local.post("/api/steps/enter", json=body, headers=PANEL_HEADER)
    assert response.status_code == 400
    assert not panel.executions
    assert panel.deps.jobs.snapshot()["name"] is None


@pytest.mark.parametrize("dry,confirm,answer", [(True, {}, ""), (False, {"confirm": "LIVE"}, "LIVE")])
def test_enter_passes_only_explicit_live_answer(panel, dry, confirm, answer):
    local_login(panel)
    panel.settings[0] = replace(panel.settings[0], dry_run=dry)
    response = panel.local.post("/api/steps/enter", json=confirm, headers=PANEL_HEADER)
    assert response.status_code == 200
    finished(panel.deps.jobs)
    assert panel.executions == [("enter", answer, "")]
    assert panel.local.get("/api/job").json()["exit_code"] == 0


@pytest.mark.parametrize("name", ["init", "set-mode", "forceenter", "unknown"])
def test_only_smoke_steps_are_allowed(panel, name):
    local_login(panel)
    assert panel.local.post("/api/steps/" + name, json={}, headers=PANEL_HEADER).status_code == 400
    assert not panel.executions


def test_second_step_and_mode_while_running_are_409_emergency_still_allowed(panel):
    local_login(panel)
    panel.options["block"] = True
    assert panel.local.post("/api/steps/check", json={}, headers=PANEL_HEADER).status_code == 200
    assert panel.entered.wait(5)
    assert panel.local.post("/api/steps/exit", json={}, headers=PANEL_HEADER).status_code == 409
    assert panel.local.post("/api/mode", json={"mode": "dry"}, headers=PANEL_HEADER).status_code == 409
    assert panel.local.post("/api/emergency/close-all", headers=PANEL_HEADER).status_code == 200
    assert panel.ft.calls == [("all", "market")]
    assert len(panel.executions) == 1 and not panel.modes


def test_mode_live_requires_confirm_and_restarts_container(panel):
    local_login(panel)
    for body in [{"mode": "live"}, {"mode": "live", "confirm": "live"}, {"mode": "LIVE"}, {"mode": []}]:
        assert panel.local.post("/api/mode", json=body, headers=PANEL_HEADER).status_code == 400
    assert not panel.modes and not panel.runner_calls
    assert panel.local.post("/api/mode", json={"mode": "live", "confirm": "LIVE"}, headers=PANEL_HEADER).status_code == 200
    assert panel.modes == ["live"]
    assert panel.runner_calls[-1] == ["docker", "compose", "-f", "freqtrade/docker-compose.yml", "restart", "freqtrade"]


def test_mode_starts_container_if_not_running_and_reports_compose_failure(panel):
    local_login(panel)
    panel.options["container"] = False
    assert panel.local.post("/api/mode", json={"mode": "dry"}, headers=PANEL_HEADER).status_code == 200
    assert panel.runner_calls[-1] == ["docker", "compose", "-f", "freqtrade/docker-compose.yml", "up", "-d"]
    panel.options["compose_code"] = 1
    assert panel.local.post("/api/docker/up", headers=PANEL_HEADER).status_code == 503


def test_step_start_cannot_race_mode_update(panel):
    local_login(panel)
    setter_started, release_setter = Event(), Event()

    def setter(mode):
        setter_started.set()
        assert release_setter.wait(5)
        panel.settings[0] = replace(panel.settings[0], dry_run=False)

    panel.deps.mode_setter = setter
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            mode = pool.submit(panel.local.post, "/api/mode", json={"mode": "live", "confirm": "LIVE"}, headers=PANEL_HEADER)
            assert setter_started.wait(5)
            step = pool.submit(panel.local.post, "/api/steps/enter", json={}, headers=PANEL_HEADER)
            release_setter.set()
            assert mode.result(timeout=5).status_code == 200
            assert step.result(timeout=5).status_code in {400, 409}
    finally:
        release_setter.set()
    assert not panel.executions


def test_keys_and_pin_and_phone_are_local_only(panel):
    tailnet_login(panel)
    for path, body in [("/api/keys", {"key": "k", "secret": "s", "password": "p"}), ("/api/pin", {"pin": PIN}), ("/api/phone/enable", {})]:
        assert panel.tailnet.post(path, json=body, headers=PANEL_HEADER).status_code == 403
    assert not panel.keys and not panel.runner_calls
    local_login(panel)
    response = panel.local.post("/api/keys", json={"key": "k", "secret": "s", "password": "p"}, headers=PANEL_HEADER)
    assert response.json() == {"saved": True, "message": "거래 봇을 재시작해야 새 키가 적용됩니다."}
    assert panel.keys == [("k", "s", "p")]
    assert panel.local.post("/api/phone/enable", headers=PANEL_HEADER).status_code == 200
    assert ["tailscale", "serve", "--bg", "8090"] in panel.runner_calls


def test_pin_saved_hashed_and_replaced_atomically(panel, monkeypatch):
    local_login(panel)
    for bad in ["12345", "1234567890123", "12a456", "123456\n", 123456, []]:
        assert panel.local.post("/api/pin", json={"pin": bad}, headers=PANEL_HEADER).status_code == 400
    assert not panel.deps.pin_store_path.exists()
    assert panel.local.post("/api/pin", json={"pin": PIN}, headers=PANEL_HEADER).json() == {"saved": True}
    stored = json.loads(panel.deps.pin_store_path.read_text(encoding="utf-8"))
    assert PIN not in repr(stored)
    assert verify_pin(PIN, stored["pin_hash"])
    old = panel.deps.pin_store_path.read_bytes()

    def fail_replace(*args):
        raise OSError(SENTINELS[3])

    monkeypatch.setattr(panel.module.os, "replace", fail_replace)
    response = panel.local.post("/api/pin", json={"pin": "654321"}, headers=PANEL_HEADER)
    assert response.status_code == 503
    assert panel.deps.pin_store_path.read_bytes() == old
    audit = panel.deps.pin_store_path.with_name("audit.log")
    assert sorted(panel.deps.pin_store_path.parent.iterdir()) == sorted([panel.deps.pin_store_path, audit])


def test_emergency_close_all_calls_force_exit_once(panel):
    local_login(panel)
    response = panel.local.post("/api/emergency/close-all", headers=PANEL_HEADER)
    assert response.status_code == 200
    assert panel.ft.calls == [("all", "market")]
    assert SENTINELS[3] not in response.text
    assert panel.executions == []


@pytest.mark.parametrize("error", [FreqtradeUnreachable("SECRET-SENTINEL"), FreqtradeError(500, "SECRET-SENTINEL"), RuntimeError("SECRET-SENTINEL")])
def test_emergency_close_all_ft_down_is_503_with_manual_link(panel, error):
    local_login(panel)
    panel.ft.error = error
    response = panel.local.post("/api/emergency/close-all", headers=PANEL_HEADER)
    assert response.status_code == 503
    assert "비트겟 웹(PC)에서 직접 시장가 청산하세요" in response.text
    assert response.json()["link"] == MANUAL_LINK
    assert panel.ft.calls == [("all", "market")]
    assert "SECRET-SENTINEL" not in response.text


def test_settings_failure_is_closed_and_sanitized_including_emergency(panel, caplog, capsys):
    local_login(panel)

    def fail():
        raise RuntimeError(" ".join(SENTINELS))

    panel.deps.settings_loader = fail
    for method, path in [("GET", "/api/status"), ("POST", "/api/steps/enter"), ("POST", "/api/emergency/close-all")]:
        response = panel.local.request(method, path, json={}, headers=PANEL_HEADER)
        assert response.status_code == 503
        assert all(secret not in response.text + str(response.headers) for secret in SENTINELS)
        if "emergency" in path:
            assert response.json()["link"] == MANUAL_LINK
    assert not panel.executions and not panel.ft.calls
    assert all(secret not in caplog.text for secret in SENTINELS)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_upstream_http_exception_never_echoes_its_detail(panel):
    from fastapi import HTTPException

    local_login(panel)

    def fail():
        raise HTTPException(500, " ".join(SENTINELS))

    panel.deps.settings_loader = fail
    response = panel.local.get("/api/status")
    assert response.status_code == 503
    assert all(secret not in response.text for secret in SENTINELS)


def test_no_secret_in_any_response_or_error_or_logs(panel, caplog, capsys):
    local_login(panel)
    requests = [
        ("GET", "/api/status", None), ("GET", "/api/job", None),
        ("POST", "/api/keys", {"key": SENTINELS[2], "secret": SENTINELS[3], "password": SENTINELS[4]}),
        ("POST", "/api/pin", {"pin": PIN}),
        ("POST", "/api/keys", {"key": {"nested": SENTINELS[3]}, "secret": SENTINELS[3]}),
        ("POST", "/api/mode", {"mode": SENTINELS[3]}),
        ("POST", "/api/pin", {"pin": SENTINELS[3]}),
        ("GET", "/api/steps/" + SENTINELS[3], None),
    ]
    for method, path, body in requests:
        response = panel.local.request(method, path, json=body, headers=PANEL_HEADER)
        assert all(secret not in response.text + str(response.headers) for secret in SENTINELS)
    response = panel.local.post("/api/keys", content='{bad "' + SENTINELS[3], headers={**PANEL_HEADER, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert all(secret not in response.text for secret in SENTINELS)
    panel.options["execute_error"] = True
    assert panel.local.post("/api/steps/check", json={}, headers=PANEL_HEADER).status_code == 200
    finished(panel.deps.jobs)
    assert all(secret not in panel.local.get("/api/job").text for secret in SENTINELS)
    assert all(secret not in caplog.text for secret in SENTINELS)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_automatic_slash_redirect_never_reflects_secret_urls(panel):
    local_login(panel)
    for path in ["/api/status/", "/api/steps/" + SENTINELS[3] + "/", "/static"]:
        response = panel.local.get(path, params={"secret": SENTINELS[3]}, follow_redirects=False)
        assert response.status_code == 404
        assert "location" not in response.headers
        assert SENTINELS[3] not in response.text + str(response.headers)


def test_default_jobs_redact_known_credentials_and_token_before_recording(panel):
    def command(name, io):
        assert name == "check"
        io.out("완료 " + " ".join([TOKEN, *SENTINELS[2:]]))
        return 0

    deps = replace(panel.deps, jobs=None, smoke_command=command)
    app = panel.module.create_app(deps)
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 12349)) as client:
        client.get("/", params={"t": TOKEN}, follow_redirects=False)
        assert client.post("/api/steps/check", json={}, headers=PANEL_HEADER).status_code == 200
        snapshot = finished(deps.jobs)
        assert snapshot["exit_code"] == 0
        assert "완료" in snapshot["lines"][0]
        assert all(secret not in repr(snapshot) for secret in [TOKEN, *SENTINELS[2:]])
        assert all(secret not in client.get("/api/job").text for secret in [TOKEN, *SENTINELS[2:]])


def test_status_uses_empty_dry_credentials_and_never_dumps_settings(panel):
    local_login(panel)
    status = panel.local.get("/api/status").json()
    assert panel.reader_calls == [("", "", "")]
    assert panel.ft_calls == [("http://127.0.0.1:8080", SENTINELS[6], SENTINELS[5])]
    assert status["mode"] == "dry" and status["keys_configured"] == {"key": True, "secret": True, "password": True}
    assert status["pin_set"] is False and status["docker_running"] is True
    assert status["tailscale"]["funnel"] is False
    assert status["job"] == panel.deps.jobs.snapshot()
    panel.settings[0] = replace(panel.settings[0], dry_run=False)
    assert panel.local.get("/api/status").json()["mode"] == "live"
    assert panel.reader_calls[-1] == tuple(SENTINELS[2:5])


def test_status_reports_smoke_state_after_restart(panel):
    panel.state_path.write_text(json.dumps({"trade_id": 17, "mode": "live", "close_confirmed": True}), encoding="utf-8")
    app = panel.module.create_app(replace(panel.deps, sessions=Sessions(clock=lambda: panel.now[0])))
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 12347)) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/", params={"t": TOKEN}, follow_redirects=False).status_code == 303
        state = client.get("/api/status").json()["smoke_state"]
    assert state == {"exists": True, "trade_id": 17, "mode": "live", "close_confirmed": True}


@pytest.mark.parametrize("fail", [False, True])
def test_restart_check_job_uses_compose_restart_and_hides_failure(panel, fail):
    local_login(panel)
    panel.options["restart_error"] = fail
    assert panel.local.post("/api/steps/restart-check", json={}, headers=PANEL_HEADER).status_code == 200
    job = finished(panel.deps.jobs)
    assert panel.runner_calls == [["docker", "compose", "-f", "freqtrade/docker-compose.yml", "restart", "freqtrade"]]
    assert job["exit_code"] == (1 if fail else 0)
    assert "command text" not in repr(job) and SENTINELS[3] not in repr(job)


def test_security_headers_and_public_static_assets(panel, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<title>제어판</title>", encoding="utf-8")
    (static / "manifest.webmanifest").write_text('{"name":"제어판"}', encoding="utf-8")
    (static / "sw.js").write_text("// test service worker", encoding="utf-8")
    app = panel.module.create_app(replace(panel.deps, static_dir=static))
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 12348)) as client:
        for path in ["/", "/static/index.html", "/manifest.webmanifest", "/sw.js", "/api/ping", "/api/status"]:
            response = client.get(path)
            assert response.status_code == (401 if path == "/api/status" else 200)
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'"
            assert not any(name.startswith("access-control-allow-") for name in response.headers)
            if path.startswith("/api/"):
                assert response.headers["cache-control"] == "no-store"
        (static / "manifest.webmanifest").unlink()
        (static / "sw.js").unlink()
        assert client.get("/manifest.webmanifest").status_code == 404
        assert client.get("/sw.js").status_code == 404
        assert client.get("/static/../app.py").status_code == 404


def test_entrypoint_refuses_wrong_cwd_and_host_without_side_effects(tmp_path, monkeypatch, capsys):
    from bitgetbot.panel import __main__ as entry

    calls = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.chdir(tmp_path)
    assert entry.main([]) == 1
    output = capsys.readouterr()
    assert "저장소 루트" in output.out + output.err
    assert not calls and not (tmp_path / ".panel").exists()
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)
    assert entry.main(["--host", "0.0.0.0"]) != 0
    assert not calls and not (tmp_path / ".panel").exists()


def test_entrypoint_busy_port_preserves_running_token_and_does_not_start_server(tmp_path, monkeypatch, capsys):
    from bitgetbot.panel import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)
    path = tmp_path / ".panel" / "token"
    entry._write_token(path, TOKEN)
    original = path.read_bytes()
    probes, closed, calls, received, generated = [], [], [], [], []

    @contextmanager
    def connect(address, timeout):
        probes.append((address, timeout))
        try:
            yield
        finally:
            closed.append(True)

    def token(size):
        generated.append(size)
        return "NEW-LAUNCH-TEST-TOKEN"

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(entry.secrets, "token_urlsafe", token)
    monkeypatch.setattr(entry, "create_app", lambda deps: received.append(deps) or "fake-app")
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert entry.main([]) == 0
    assert path.read_bytes() == original
    assert probes == [(("127.0.0.1", 8090), 0.2)]
    assert closed == [True]
    assert not calls and not received and not generated
    captured = capsys.readouterr()
    assert "패널이 이미 실행 중입니다." in captured.out + captured.err
    assert TOKEN not in captured.out + captured.err
    assert list(path.parent.iterdir()) == [path]


def test_entrypoint_rotates_private_token_and_binds_only_loopback(tmp_path, monkeypatch, capsys):
    from bitgetbot.panel import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)
    calls, received = [], []
    monkeypatch.setattr(entry, "create_app", lambda deps: received.append(deps) or "fake-app")
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    probes = []

    def refuse(address, timeout):
        probes.append((address, timeout))
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", refuse)
    tokens = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    seen_sizes = []

    def token(size):
        seen_sizes.append(size)
        return tokens[len(seen_sizes) - 1]

    monkeypatch.setattr(entry.secrets, "token_urlsafe", token)
    for index in range(2):
        assert entry.main([]) == 0
        assert (tmp_path / ".panel" / "token").read_text(encoding="utf-8").strip() == tokens[index]
        assert received[index].token == tokens[index]
    assert probes == [(("127.0.0.1", 8090), 0.2)] * 2
    assert seen_sizes == [32, 32]
    assert calls == [("fake-app", {
        "host": "127.0.0.1", "port": 8090, "log_level": "warning", "access_log": False,
        "proxy_headers": False,
    })] * 2
    captured = capsys.readouterr()
    assert all(token not in captured.out + captured.err for token in tokens)
    assert list((tmp_path / ".panel").iterdir()) == [tmp_path / ".panel" / "token"]


def test_entrypoint_private_token_has_protected_owner_only_permissions(tmp_path):
    from bitgetbot.panel import __main__ as entry

    path = tmp_path / ".panel" / "token"
    entry._write_token(path, TOKEN)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        return
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi.GetFileSecurityW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get_security.restype = wintypes.BOOL
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    for target in [path.parent, path]:
        size = wintypes.DWORD()
        get_security(str(target), 4, None, 0, ctypes.byref(size))
        descriptor = ctypes.create_string_buffer(size.value)
        assert get_security(str(target), 4, descriptor, size, ctypes.byref(size))
        output = ctypes.c_void_p()
        assert convert(descriptor, 1, 4, ctypes.byref(output), None)
        try:
            sddl = ctypes.wstring_at(output)
            assert sddl.startswith("D:P")
            assert sddl.count("(") == 1
            assert ";FA;;;OW)" in sddl
        finally:
            kernel.LocalFree(output)


def test_entrypoint_permission_failure_stops_without_writing_token(tmp_path, monkeypatch, capsys):
    from bitgetbot.panel import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)

    def refuse(path):
        raise OSError(TOKEN)

    def forbidden(*args, **kwargs):
        pytest.fail("Permission failure must not start server")

    def port_free(address, timeout):
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", port_free)
    monkeypatch.setattr(entry, "_restrict_to_owner", refuse)
    monkeypatch.setattr(entry.uvicorn, "run", forbidden)
    assert entry.main([]) == 1
    assert not (tmp_path / ".panel" / "token").exists()
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


EVIL = "http://evil.example:8090"


def refuse_connection(*args, **kwargs):
    raise ConnectionRefusedError


@pytest.mark.parametrize("path,method", [
    ("/", "GET"), ("/api/ping", "GET"), ("/api/login", "POST"), ("/api/status", "GET"),
    ("/static/app.js", "GET"), ("/sw.js", "GET"), ("/api/emergency/close-all", "POST"),
])
def test_foreign_host_is_rejected_before_csrf_and_auth(panel, path, method):
    tailnet_login(panel)
    with TestClient(panel.app, base_url=EVIL, client=("127.0.0.1", 12350)) as evil:
        headers = {**PANEL_HEADER, "Origin": EVIL} if method == "POST" else {}
        response = evil.request(method, path, json={"pin": PIN} if method == "POST" else None, headers=headers)
    assert response.status_code == 421
    assert response.json() == {"detail": "허용하지 않는 접속 주소입니다. 바로가기나 테일스케일 주소로 여세요."}
    assert "set-cookie" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert not panel.ft.calls


def test_foreign_host_cannot_lock_out_phone_login(panel):
    tailnet_login(panel)
    panel.tailnet.cookies.clear()
    with TestClient(panel.app, base_url=EVIL, client=("127.0.0.1", 12351)) as evil:
        for _ in range(10):
            response = evil.post("/api/login", json={"pin": "000000"}, headers={**PANEL_HEADER, "Origin": EVIL})
            assert response.status_code == 421
    assert panel.deps.lockout.check("*") == 0
    response = panel.tailnet.post("/api/login", json={"pin": PIN}, headers={**PANEL_HEADER, "Tailscale-User-Login": "phone"})
    assert response.status_code == 200


@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:8090", "http://localhost:8090", "https://pc.tail1234.ts.net", "https://PC.TAIL1234.TS.NET",
])
def test_allowed_hosts_still_reach_public_routes(panel, base_url):
    with TestClient(panel.app, base_url=base_url, client=("127.0.0.1", 12352)) as client:
        assert client.get("/api/ping").json() == {"ok": True}
        assert client.get("/").status_code == 200


def test_mode_switch_refused_while_smoke_state_exists(panel):
    local_login(panel)
    panel.state_path.write_text(json.dumps({"started_ms": 1, "trade_id": 7, "mode": "dry"}), encoding="utf-8")
    response = panel.local.post("/api/mode", json={"mode": "live", "confirm": "LIVE"}, headers=PANEL_HEADER)
    assert response.status_code == 409
    assert response.json()["detail"] == "진행 중인 거래를 먼저 청산하세요"
    assert not panel.modes and not panel.runner_calls
    panel.state_path.write_text("not json", encoding="utf-8")
    assert panel.local.post("/api/mode", json={"mode": "dry"}, headers=PANEL_HEADER).status_code == 409
    assert not panel.modes


def test_mode_switch_refused_while_freqtrade_reports_open_trades(panel):
    local_login(panel)
    panel.ft.trades = [{"trade_id": 3, "pair": "BTC/USDT:USDT"}]
    response = panel.local.post("/api/mode", json={"mode": "dry"}, headers=PANEL_HEADER)
    assert response.status_code == 409
    assert response.json()["detail"] == "진행 중인 거래를 먼저 청산하세요"
    assert not panel.modes and not panel.runner_calls


def test_mode_switch_allowed_when_freqtrade_unreachable_and_no_smoke_state(panel):
    local_login(panel)
    panel.ft.ping_error = FreqtradeUnreachable(SENTINELS[3])
    response = panel.local.post("/api/mode", json={"mode": "dry"}, headers=PANEL_HEADER)
    assert response.status_code == 200
    assert panel.modes == ["dry"]
    assert SENTINELS[3] not in response.text


@pytest.mark.parametrize("dry,expected", [
    (True, "모의 거래만 청산했습니다. 실거래 포지션은 비트겟 웹에서 확인하세요."),
    (False, "봇이 모르는 포지션은 비트겟 웹에서 확인하세요."),
])
def test_emergency_message_names_freqtrade_trades_and_current_mode(panel, dry, expected):
    local_login(panel)
    panel.settings[0] = replace(panel.settings[0], dry_run=dry)
    response = panel.local.post("/api/emergency/close-all", headers=PANEL_HEADER)
    assert response.status_code == 200
    message = response.json()["message"]
    assert "거래 봇(Freqtrade)이 연 거래를 시장가로 청산 요청" in message
    assert expected in message
    assert "모든 포지션" not in message and "전부" not in message
    assert ("모의 거래만" in message) is dry
    assert panel.ft.calls == [("all", "market")]


def test_keys_and_docker_up_refused_while_job_runs(panel):
    local_login(panel)
    panel.options["block"] = True
    assert panel.local.post("/api/steps/check", json={}, headers=PANEL_HEADER).status_code == 200
    assert panel.entered.wait(5)
    response = panel.local.post("/api/keys", json={"key": "k", "secret": "s", "password": "p"}, headers=PANEL_HEADER)
    assert response.status_code == 409
    assert not panel.keys
    assert panel.local.post("/api/docker/up", headers=PANEL_HEADER).status_code == 409
    assert not any(argv[-2:] == ["up", "-d"] for argv in panel.runner_calls)


def test_keys_wait_for_operation_lock(panel):
    local_login(panel)
    setter_started, release_setter = Event(), Event()

    def setter(mode):
        setter_started.set()
        assert release_setter.wait(5)

    panel.deps.mode_setter = setter
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            mode = pool.submit(panel.local.post, "/api/mode", json={"mode": "dry"}, headers=PANEL_HEADER)
            assert setter_started.wait(5)
            keys = pool.submit(
                panel.local.post, "/api/keys", json={"key": "k", "secret": "s", "password": "p"}, headers=PANEL_HEADER,
            )
            Event().wait(0.2)
            assert not keys.done() and not panel.keys
            release_setter.set()
            assert mode.result(timeout=5).status_code == 200
            assert keys.result(timeout=5).status_code == 200
    finally:
        release_setter.set()
    assert panel.keys == [("k", "s", "p")]


def test_pin_save_revokes_phone_sessions_but_keeps_desktop(panel):
    tailnet_login(panel)
    assert panel.tailnet.get("/api/status").status_code == 200
    local_login(panel)
    assert panel.local.post("/api/pin", json={"pin": "123456"}, headers=PANEL_HEADER).status_code == 200
    assert panel.tailnet.get("/api/status").status_code == 401
    assert panel.local.get("/api/status").status_code == 200


def audit_lines(panel):
    path = panel.deps.pin_store_path.with_name("audit.log")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_audit_log_records_actions_with_source_and_no_values(panel):
    import re as regex

    local_login(panel)
    panel.local.post("/api/pin", json={"pin": PIN}, headers=PANEL_HEADER)
    panel.local.post("/api/keys", json={"key": SENTINELS[2], "secret": SENTINELS[3], "password": SENTINELS[4]}, headers=PANEL_HEADER)
    panel.local.post("/api/mode", json={"mode": "live", "confirm": "LIVE"}, headers=PANEL_HEADER)
    panel.local.post("/api/mode", json={"mode": "dry"}, headers=PANEL_HEADER)
    panel.local.post("/api/steps/preflight", json={}, headers=PANEL_HEADER)
    finished(panel.deps.jobs)
    panel.local.post("/api/steps/" + SENTINELS[3], json={}, headers=PANEL_HEADER)
    panel.local.post("/api/docker/up", headers=PANEL_HEADER)
    panel.local.post("/api/phone/enable", headers=PANEL_HEADER)
    panel.local.post("/api/emergency/close-all", headers=PANEL_HEADER)
    phone = {**PANEL_HEADER, "Tailscale-User-Login": "me@example.com"}
    panel.tailnet.post("/api/login", json={"pin": "000000"}, headers=phone)
    panel.tailnet.post("/api/login", json={"pin": PIN}, headers=phone)
    panel.tailnet.post("/api/emergency/close-all", headers=phone)
    panel.local.get("/api/status")
    lines = audit_lines(panel)
    assert [(line["source"], line["action"], line["result"]) for line in lines] == [
        ("local", "pin_set", 200), ("local", "keys_saved", 200), ("local", "mode", 200),
        ("local", "mode", 200), ("local", "step", 200), ("local", "step", 400),
        ("local", "docker_up", 200), ("local", "phone_enable", 200), ("local", "emergency", 200),
        ("tailnet", "login_fail", 401), ("tailnet", "login_ok", 200), ("tailnet", "emergency", 200),
    ]
    assert lines[4]["step"] == "preflight" and "step" not in lines[5]
    for line in lines:
        assert regex.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", line["ts"])
        assert ("identity" in line) is (line["source"] == "tailnet")
        assert set(line) <= {"ts", "source", "identity", "action", "step", "result"}
    assert lines[-1]["identity"] == "me@example.com"
    text = panel.deps.pin_store_path.with_name("audit.log").read_text(encoding="utf-8")
    assert all(secret not in text for secret in SENTINELS)
    assert "LIVE" not in text and '"live"' not in text


def test_audit_write_failure_never_breaks_the_action(panel):
    local_login(panel)
    panel.deps.audit_path = panel.deps.pin_store_path.parent / "missing" / "\0bad" / "audit.log"
    response = panel.local.post("/api/emergency/close-all", headers=PANEL_HEADER)
    assert response.status_code == 200
    assert panel.ft.calls == [("all", "market")]


def test_default_jobs_run_smoke_with_panel_hints(panel):
    seen = []

    def command(name, io):
        seen.append(io.hint_style)
        return 0

    deps = replace(panel.deps, jobs=None, smoke_command=command)
    app = panel.module.create_app(deps)
    with TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 12353)) as client:
        client.get("/", params={"t": TOKEN}, follow_redirects=False)
        assert client.post("/api/steps/check", json={}, headers=PANEL_HEADER).status_code == 200
        finished(deps.jobs)
    assert seen == ["panel"]


def test_entrypoint_reopens_missing_pythonw_streams_to_private_logs(tmp_path, monkeypatch):
    import sys
    from bitgetbot.panel import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(socket, "create_connection", refuse_connection)
    monkeypatch.setattr(entry, "create_app", lambda deps: "fake-app")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    seen = []

    def run(app, **kwargs):
        seen.append((sys.stdout, sys.stderr))
        print("stdout probe")
        print("stderr probe", file=sys.stderr)

    monkeypatch.setattr(entry.uvicorn, "run", run)
    try:
        assert entry.main([]) == 0
        out, err = seen[0]
        assert out is not None and err is not None
        assert Path(out.name).resolve() == (tmp_path / ".panel" / "panel.out.log").resolve()
        assert Path(err.name).resolve() == (tmp_path / ".panel" / "panel.err.log").resolve()
        out.flush()
        err.flush()
        assert "stdout probe" in (tmp_path / ".panel" / "panel.out.log").read_text(encoding="utf-8")
        assert "stderr probe" in (tmp_path / ".panel" / "panel.err.log").read_text(encoding="utf-8")
        token = (tmp_path / ".panel" / "token").read_text(encoding="utf-8").strip()
        assert token not in (tmp_path / ".panel" / "panel.out.log").read_text(encoding="utf-8")
    finally:
        for stream in seen[0] if seen else ():
            if stream is not None:
                stream.close()


def test_entrypoint_keeps_existing_streams(tmp_path, monkeypatch):
    import sys
    from bitgetbot.panel import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry.ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(socket, "create_connection", refuse_connection)
    monkeypatch.setattr(entry, "create_app", lambda deps: "fake-app")
    before = (sys.stdout, sys.stderr)
    seen = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: seen.append((sys.stdout, sys.stderr)))
    assert entry.main([]) == 0
    assert seen == [before]
    assert not (tmp_path / ".panel" / "panel.out.log").exists()


def test_autotrade_start_in_dry_mode_starts_freqtrade(panel):
    local_login(panel)
    response = panel.local.post("/api/autotrade", json={"action": "start"}, headers=PANEL_HEADER)
    assert response.status_code == 200
    assert panel.ft.calls == [("start",)]
    assert "모의" in response.json()["message"]
    assert SENTINELS[3] not in response.text


def test_autotrade_live_start_requires_exact_live_confirmation(panel):
    local_login(panel)
    panel.settings[0] = replace(panel.settings[0], dry_run=False)
    panel.ft.dry = False
    for body in [{"action": "start"}, {"action": "start", "confirm": "live"}]:
        assert panel.local.post("/api/autotrade", json=body, headers=PANEL_HEADER).status_code == 400
    assert panel.ft.calls == []
    response = panel.local.post("/api/autotrade", json={"action": "start", "confirm": "LIVE"}, headers=PANEL_HEADER)
    assert response.status_code == 200
    assert panel.ft.calls == [("start",)]
    assert "실전" in response.json()["message"]


def test_autotrade_pause_needs_no_confirmation_and_works_from_phone(panel):
    tailnet_login(panel)
    response = panel.tailnet.post("/api/autotrade", json={"action": "pause"}, headers=PANEL_HEADER)
    assert response.status_code == 200
    assert panel.ft.calls == [("pause",)]


@pytest.mark.parametrize("body", [{}, {"action": "stop"}, {"action": ["start"]}, {"action": "START"}])
def test_autotrade_rejects_unknown_actions(panel, body):
    local_login(panel)
    assert panel.local.post("/api/autotrade", json=body, headers=PANEL_HEADER).status_code == 400
    assert panel.ft.calls == []


def test_autotrade_refuses_when_running_bot_mode_differs_from_settings(panel):
    local_login(panel)
    panel.ft.dry = False          # running bot is live while the saved setting is dry
    response = panel.local.post("/api/autotrade", json={"action": "start"}, headers=PANEL_HEADER)
    assert response.status_code == 409
    assert panel.ft.calls == []


@pytest.mark.parametrize("error", [FreqtradeUnreachable("SECRET-SENTINEL"), FreqtradeError(500, "SECRET-SENTINEL")])
def test_autotrade_freqtrade_failure_is_sanitized(panel, error):
    local_login(panel)
    panel.ft.error = error
    response = panel.local.post("/api/autotrade", json={"action": "start"}, headers=PANEL_HEADER)
    assert response.status_code == 503
    assert "SECRET-SENTINEL" not in response.text


def test_autotrade_blocked_while_a_job_runs(panel):
    local_login(panel)
    panel.options["block"] = True
    assert panel.local.post("/api/steps/preflight", json={}, headers=PANEL_HEADER).status_code == 200
    assert panel.entered.wait(5)
    assert panel.local.post("/api/autotrade", json={"action": "start"}, headers=PANEL_HEADER).status_code == 409
    panel.release.set()
