"""Loopback control-panel API. Orders stay inside smoke, emergency close-all, the per-trade close
and take-profit controls, or the strategy the user switches on with /api/autotrade or a confirmed
mode switch (Freqtrade /start, /pause). Every order goes through Freqtrade, never straight to Bitget."""

import json
import math
import re
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from bitgetbot.bitget_read import BitgetReader, make_exchange
from bitgetbot.ftclient import FreqtradeClient
from bitgetbot.ledger import fetch_krw_per_usdt
from bitgetbot.panel import charts, ops, targets, widget, widget_token
from bitgetbot.panel.auth import (
    Lockout, Sessions, allowed_host, classify, csrf_ok, effective_scheme, hash_pin, identity, valid_pin,
    verify_pin,
)
from bitgetbot.panel.jobs import JobRunner
from bitgetbot.privcfg import Settings, keys_configured, load_settings, set_exchange_keys, set_mode
from bitgetbot.smoke import SmokeIO, run_command


STATIC_DIR = Path(__file__).with_name("static")
STEPS = {"preflight", "enter", "check", "restart-check", "exit", "ledger"}
LIVE_PROMPT = "실전 주문을 냅니다. 계속하려면 LIVE 입력:"
MANUAL_LINK = "https://www.bitget.com/futures/usdt/BTCUSDT"
FAILURE = "요청 실패: 설정·연결 상태를 확인하세요. 오류 원문은 표시하지 않습니다"
BAD_HOST = "허용하지 않는 접속 주소입니다. 바로가기나 테일스케일 주소로 여세요."
OPEN_TRADE = "진행 중인 거래를 먼저 청산하세요"
KEYS_SAVED = "거래 봇을 재시작해야 새 키가 적용됩니다."
EMERGENCY_SCOPE = "거래 봇(Freqtrade)이 연 거래를 시장가로 청산 요청했습니다 (현재 모드: {mode})."
EMERGENCY_DRY = "모의 거래만 청산했습니다. 실거래 포지션은 비트겟 웹에서 확인하세요."
EMERGENCY_LIVE = "봇이 모르는 포지션은 비트겟 웹에서 확인하세요."
AUTOTRADE_STARTED = "자동매매를 켰습니다 (현재 모드: {mode}). 전략이 스스로 진입·청산합니다."
AUTOTRADE_PAUSED = "새 진입을 멈췄습니다. 열린 포지션과 손절은 계속 관리합니다."
AUTOTRADE_MISMATCH = "거래 봇 모드가 저장된 모드와 다릅니다. 모드를 다시 적용한 뒤 시도하세요"
AUTOTRADE_FAILED = "거래 봇에 요청하지 못했습니다. 실행 상태를 확인하세요"
TRADE_GONE = "열린 거래가 아닙니다. 화면을 새로 고쳐 확인하세요"
TRADE_SMOKE = "점검(스모크) 거래는 단계 버튼의 청산으로 닫으세요"
TRADE_CLOSE_FAILED = "종료 요청을 확인할 수 없습니다. 다시 누르지 말고 비트겟 앱에서 포지션과 주문을 직접 확인하세요"
TRADE_PRICE = "가격은 0보다 큰 숫자로 입력하세요"
TP_WRONG_SIDE = "익절가는 현재가보다 유리한 쪽(롱은 위, 숏은 아래)이어야 합니다. 지금 닫으려면 시장가 종료를 쓰세요"
TP_FAR = "익절가는 현재가에서 50% 안쪽으로 입력하세요"
TP_UNSUPPORTED = "거래 봇을 다시 시작해야 익절가를 쓸 수 있습니다"
TP_SAVE_FAILED = "익절가를 저장하지 못했습니다"
# A resting limit exit can fill partly; at this account's size Freqtrade then cannot cancel the rest,
# which blocks the bot's own exits and close-all. So "exit at my price" is a take-profit the strategy
# executes at market on touch, and the panel never rests limit exits.
TP_BAND = 0.5
MODE_SWITCHED = "{mode} 모드로 전환했습니다. 거래 봇이 준비되면(약 1~2분) 자동매매가 저절로 켜집니다."
AUTO_START_TIMEOUT_S = 180
AUTO_START_INTERVAL_S = 3
WIDGET_PATH = "/api/widget/summary"
WIDGET_DENIED = "위젯 토큰이 올바르지 않습니다"
WIDGET_LOCKED = "위젯 토큰 확인이 잠겼습니다. 잠시 후 다시 시도하세요"
# Audit actions by exact POST path. Request bodies, keys, PINs and tokens are never recorded.
AUDIT_ACTIONS = {
    "/api/pin": "pin_set", "/api/keys": "keys_saved", "/api/mode": "mode",
    "/api/docker/up": "docker_up", "/api/emergency/close-all": "emergency",
    "/api/phone/enable": "phone_enable", "/api/autotrade": "autotrade",
}
TRADE_ACTIONS = {"close": "trade_close", "take-profit": "take_profit"}
TRADE_ID = re.compile(r"[0-9]{1,12}")


class _PanelError(HTTPException):
    """Only panel-authored messages may cross the exception boundary."""


def _reader_factory(key: str, secret: str, password: str):
    return BitgetReader(make_exchange(key, secret, password))


def _daemon(task: Callable[[], None]) -> None:
    threading.Thread(target=task, name="panel-background", daemon=True).start()


def _close(client) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


@dataclass(repr=False)
class PanelDeps:
    token: str
    settings_loader: Callable[[], Settings] = load_settings
    reader_factory: Callable[..., Any] = _reader_factory
    ft_factory: Callable[..., Any] = FreqtradeClient
    runner: ops.Runner = ops.default_runner
    jobs: JobRunner | None = None
    sessions: Sessions | None = None
    lockout: Lockout | None = None
    pin_store_path: Path = Path(".panel/panel.json")
    clock: Callable[[], float] = time.time
    mode_setter: Callable[[str], None] = set_mode
    keys_setter: Callable[[str, str, str], None] = set_exchange_keys
    keys_configured: Callable[[], dict[str, bool]] = keys_configured
    static_dir: Path = STATIC_DIR
    smoke_command: Callable[[str, SmokeIO], int] = run_command
    audit_path: Path | None = None
    targets_path: Path = targets.PATH
    # Runs the post-switch "start autotrade when ready" task and the startup equity snapshot;
    # tests replace it to run them inline.
    background: Callable[[Callable[[], None]], None] = _daemon
    record_on_startup: bool = True
    sleep: Callable[[float], None] = time.sleep
    krw_rate: Callable[[], tuple[float, str]] = fetch_krw_per_usdt
    equity_log: charts.EquityLog | None = None
    fx: charts.FxCache | None = None
    # Persistent read-only widget token (.panel/widget_token), re-read when the file changes.
    widget_token: Callable[[], str | None] | None = None
    # Separate from the PIN lockout: widget failures never lock the panel or the phone login.
    widget_lockout: Lockout | None = None

    def __post_init__(self):
        if self.audit_path is None:
            self.audit_path = self.pin_store_path.with_name("audit.log")
        if self.equity_log is None:
            self.equity_log = charts.EquityLog(self.pin_store_path.with_name("equity.jsonl"), self.clock)
        if self.fx is None:
            self.fx = charts.FxCache(self.krw_rate, self.clock)
        if self.jobs is None:
            self.jobs = JobRunner(self._execute, clock=self.clock)
        if self.sessions is None:
            self.sessions = Sessions(clock=self.clock)
        if self.lockout is None:
            self.lockout = Lockout(clock=self.clock)
        if self.widget_token is None:
            self.widget_token = widget_token.FileToken(self.pin_store_path.with_name("widget_token"))
        if self.widget_lockout is None:
            self.widget_lockout = Lockout(clock=self.clock)

    def _execute(self, name: str, io: SmokeIO) -> int:
        settings = _settings(self)
        hidden = [self.token, settings.key, settings.secret, settings.password,
                  settings.api_user, settings.api_pass]

        def out(line: str):
            # Defense in depth for upstream identifiers: redact before retaining any job line.
            for value in sorted((value for value in hidden if value), key=len, reverse=True):
                line = line.replace(value, "[REDACTED]")
            io.out(line)

        return self.smoke_command(name, SmokeIO(out, io.ask, io.restart, io.hint_style))


def _pin_hash(path: Path) -> str | None:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        value = stored.get("pin_hash") if isinstance(stored, dict) else None
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, UnicodeError):
        return None


def _save_pin(path: Path, pin: str) -> None:
    value = hash_pin(pin)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump({"pin_hash": value}, temporary)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def _body(request: Request) -> dict:
    # Do not use default validation errors: they echo input, including credentials.
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 16384:
            raise _PanelError(400, "입력 크기를 확인하세요")
    if not data:
        return {}
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise _PanelError(400, "JSON 입력을 확인하세요") from None
    if not isinstance(value, dict):
        raise _PanelError(400, "JSON 객체를 입력하세요")
    return value


def _settings(deps: PanelDeps) -> Settings:
    settings = deps.settings_loader()
    if type(settings.dry_run) is not bool:
        raise _PanelError(503, "모드 설정을 확인할 수 없습니다")
    return settings


def _ft(deps: PanelDeps):
    settings = _settings(deps)
    return deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass)


_audit_lock = threading.Lock()


def _audit_action(method: str, path: str, status: int) -> tuple[str, str | None] | None:
    if method != "POST":
        return None
    if path == "/api/login":
        return ("login_ok" if status == 200 else "login_fail"), None
    if path.startswith("/api/steps/"):
        name = path.removeprefix("/api/steps/")
        return "step", name if name in STEPS else None
    if path.startswith("/api/trades/"):
        parts = path.removeprefix("/api/trades/").split("/")
        if len(parts) == 2 and TRADE_ID.fullmatch(parts[0]) and parts[1] in TRADE_ACTIONS:
            return TRADE_ACTIONS[parts[1]], str(int(parts[0]))
        return None
    action = AUDIT_ACTIONS.get(path)
    return None if action is None else (action, None)


def _write_audit(deps: PanelDeps, kind: str, ident: str | None, action: str, step: str | None,
                 status: int) -> None:
    """Append one JSON line; a failed write never changes the response."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "source": kind,
        }
        if kind == "tailnet":
            entry["identity"] = (ident or "")[:256]
        entry["action"] = action
        if step is not None:
            entry["step"] = step
        entry["result"] = status
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _audit_lock:
            deps.audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(deps.audit_path, "a", encoding="utf-8") as log:
                log.write(line)
    except Exception:
        pass


def _audit(deps: PanelDeps, request: Request, kind: str, status: int) -> None:
    """One line per panel action: no request bodies, keys, PINs or tokens."""
    try:
        found = _audit_action(request.method, request.url.path, status)
        if found is None:
            return
        ident = identity(request.headers) if kind == "tailnet" else None
        _write_audit(deps, kind, ident, found[0], found[1], status)
    except Exception:
        pass


def _open_trade_blocks_mode(deps: PanelDeps) -> bool:
    """A mode switch must never hide an open smoke trade or a Freqtrade trade."""
    if ops.smoke_state_exists():
        return True
    ft = _ft(deps)
    try:
        if ft.ping() is not True:
            return False
    except Exception:
        # Unreachable Freqtrade cannot report trades; the smoke state file is the record then.
        return False
    try:
        return bool(ft.status())
    except Exception:
        # Reachable but unreadable is ambiguous: refuse rather than hide a position.
        return True


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, credentials = value.partition(" ")
    credentials = credentials.strip()
    return credentials if scheme.lower() == "bearer" and credentials else None


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


def _equity_reader(settings: Settings, reader, ft) -> Callable[[], float | None]:
    return lambda: charts.current_equity(settings.dry_run, reader, ft)


def create_app(deps: PanelDeps) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        # One equity snapshot on startup, in the background so startup never blocks or fails.
        if deps.record_on_startup:
            try:
                deps.background(record_equity_on_startup)
            except Exception:
                pass
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False, lifespan=lifespan)
    # Serialize config writes with job admission, not with long-running jobs or emergency exits.
    operation_lock = threading.Lock()
    # A later mode switch, manual autotrade action or emergency close cancels a pending auto start.
    auto_generation = [0]

    def cancel_auto_start() -> int:
        auto_generation[0] += 1
        return auto_generation[0]

    def auto_start(mode: str, generation: int, kind: str, ident: str | None) -> None:
        """After a switch, start automatic trading once the restarted bot runs in the saved mode."""
        dry = mode == "dry"
        result = 503
        # Wall-clock deadline: a slow or hanging API (10 s client timeout) must not stretch the wait.
        deadline = deps.clock() + AUTO_START_TIMEOUT_S
        ft = None
        try:
            first = True
            while True:
                if not first:
                    if deps.clock() + AUTO_START_INTERVAL_S > deadline:
                        return
                    deps.sleep(AUTO_START_INTERVAL_S)
                first = False
                if generation != auto_generation[0]:
                    result = 409
                    return
                try:
                    settings = _settings(deps)
                    if settings.dry_run is not dry:
                        return  # the saved mode changed again; never start the other mode
                    if ft is None:
                        ft = deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass)
                    config = ft.show_config()
                    if not isinstance(config, dict) or config.get("dry_run") is not dry:
                        continue
                    state = config.get("state")
                    if state == "running":
                        result = 200
                        return
                    if state not in {"paused", "stopped"}:
                        continue
                    with operation_lock:
                        if generation != auto_generation[0]:
                            result = 409
                            return
                        if deps.jobs.snapshot()["running"]:
                            continue
                        if deps.clock() > deadline:
                            return  # too late: never start long after the promised wait
                        ft.start()
                        if generation != auto_generation[0]:
                            # An emergency close (no lock) landed during start(): undo it.
                            try:
                                ft.pause()
                            except Exception:
                                pass
                            result = 409
                            return
                    result = 200
                    return
                except Exception:
                    continue
        finally:
            _close(ft)
            _write_audit(deps, kind, ident, "autotrade_auto", None, result)

    def record_equity(settings: Settings, reader, ft) -> None:
        deps.equity_log.maybe_record("dry" if settings.dry_run else "live", _equity_reader(settings, reader, ft))

    def record_equity_on_startup() -> None:
        ft = None
        try:
            settings = _settings(deps)
            credentials = ("", "", "") if settings.dry_run else (settings.key, settings.secret, settings.password)
            ft = deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass)
            record_equity(settings, deps.reader_factory(*credentials), ft)
        except Exception:
            pass  # startup never fails or blocks on an unreadable account
        finally:
            _close(ft)

    def build_widget_summary() -> dict:
        ft = None
        try:
            settings = _settings(deps)
            credentials = ("", "", "") if settings.dry_run else (settings.key, settings.secret, settings.password)
            ft = deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass,
                                 timeout=widget.FT_TIMEOUT_S)
            return widget.build(widget.Sources(
                settings=settings, reader=deps.reader_factory(*credentials), ft=ft, fx=deps.fx,
                equity_log=deps.equity_log, docker_running=lambda: ops.docker_running(deps.runner, widget.DOCKER_TIMEOUT_S),
                clock=deps.clock,
            ))
        finally:
            _close(ft)

    widget_cache = widget.SummaryCache(build_widget_summary, deps.clock)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"detail": "입력을 확인하세요"}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        if isinstance(error, _PanelError):
            return JSONResponse({"detail": error.detail}, status_code=error.status_code)
        messages = {404: "요청한 항목이 없습니다", 405: "허용하지 않는 요청 방식입니다"}
        return JSONResponse(
            {"detail": messages.get(error.status_code, FAILURE)},
            status_code=error.status_code if error.status_code in messages else 503,
        )

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = request.headers.get("host", "")
        client = request.client.host if request.client is not None else ""
        request_kind = classify(client, host, request.headers)
        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        response = None
        try:
            if not allowed_host(host):
                # DNS rebinding: a foreign name on loopback must not reach CSRF, login or static files.
                response = JSONResponse({"detail": BAD_HOST}, status_code=421)
            elif is_api:
                # csrf_ok's legacy method allowlist does not cover PATCH/OPTIONS or unknown verbs.
                # Every unsafe verb must pass the same check as POST before routing/authentication.
                method = "GET" if request.method in {"GET", "HEAD"} else "POST"
                scheme = effective_scheme(request.url.scheme, host, request.headers)
                if not csrf_ok(method, request.headers, scheme, host):
                    response = JSONResponse({"detail": "요청 출처를 확인할 수 없습니다"}, status_code=403)
                elif request.url.path not in {"/api/ping", "/api/login", WIDGET_PATH}:
                    # The widget route authenticates only with its own Bearer token (in the route).
                    sid = request.cookies.get("panel_session", "")
                    session_kind = deps.sessions.get(sid)
                    # A stolen local cookie is never a local capability over a proxy/tailnet request.
                    if session_kind not in {"local", "tailnet"} or session_kind != request_kind:
                        response = JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
                    else:
                        request.state.session_kind = session_kind
            request.state.request_kind = request_kind
            if response is None:
                response = await call_next(request)
        except Exception:
            # Catch before ServerErrorMiddleware logs an exception with upstream secrets.
            response = JSONResponse({"detail": FAILURE}, status_code=503)
        _audit(deps, request, request_kind, response.status_code)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"] = "no-referrer"
        # The token redirect and the service worker must not be stored either.
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_local(request: Request):
        if request.state.session_kind != "local":
            raise _PanelError(403, "PC에서만 사용할 수 있습니다")

    def session_response(kind: str):
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "panel_session", deps.sessions.create(kind), secure=kind == "tailnet",
            httponly=True, samesite="strict", path="/",
        )
        return response

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.get("/")
    def index(request: Request):
        token = request.query_params.get("t")
        if (token is not None and request.state.request_kind == "local"
                and secrets.compare_digest(token.encode("utf-8"), deps.token.encode("utf-8"))):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                "panel_session", deps.sessions.create("local"), secure=False,
                httponly=True, samesite="strict", path="/",
            )
            return response
        return public_file("index.html")

    @app.post("/api/login")
    def login(request: Request, body: dict = Depends(_body)):
        if request.state.request_kind != "tailnet":
            raise _PanelError(403, "휴대폰에서 PIN으로 로그인하세요")
        stored = _pin_hash(deps.pin_store_path)
        if stored is None:
            raise _PanelError(409, "PC에서 PIN을 먼저 설정하세요")
        ident = identity(request.headers)
        # Headers can be spoofed on loopback; also reserve a non-evictable global budget.
        remaining = max(deps.lockout.attempt(ident), deps.lockout.attempt("*"))
        if remaining > 0:
            seconds = math.ceil(remaining)
            return JSONResponse(
                {"detail": "PIN 입력이 잠겼습니다. 잠시 후 다시 시도하세요", "retry_after": seconds},
                status_code=429, headers={"Retry-After": str(seconds)},
            )
        pin = body.get("pin")
        if not isinstance(pin, str) or not valid_pin(pin) or not verify_pin(pin, stored):
            raise _PanelError(401, "PIN이 올바르지 않습니다")
        deps.lockout.success(ident)
        deps.lockout.success("*")
        return session_response("tailnet")

    @app.get(WIDGET_PATH)
    def widget_summary(request: Request):
        # Read-only and Bearer-only: the panel cookie and launch token are ignored here, and the
        # widget token is accepted nowhere else.
        expected = deps.widget_token()
        supplied = _bearer(request.headers.get("authorization"))
        if (expected is None or supplied is None
                or not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))):
            # The lock throttles wrong tokens only. A valid 256-bit token always passes, so a
            # stale board or another local process cannot lock the real widget out, and valid
            # polls never reset the failure count (no guess budget refilled between polls).
            remaining = deps.widget_lockout.attempt("*")
            if remaining > 0:
                seconds = math.ceil(remaining)
                return JSONResponse({"detail": WIDGET_LOCKED, "retry_after": seconds}, status_code=429,
                                    headers={"Retry-After": str(seconds)})
            return JSONResponse({"detail": WIDGET_DENIED}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            summary = widget_cache.get()
        except Exception:
            raise _PanelError(503, FAILURE) from None
        seq = summary["seq"]
        etag = f'"{seq}"'
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(summary, headers={"ETag": etag})

    @app.post("/api/pin")
    def save_pin(request: Request, body: dict = Depends(_body)):
        require_local(request)
        pin = body.get("pin")
        if not isinstance(pin, str) or not valid_pin(pin):
            raise _PanelError(400, "PIN은 숫자 6~12자리로 입력하세요")
        _save_pin(deps.pin_store_path, pin)
        # A lost or shared phone must not keep a session after the PIN changes.
        deps.sessions.revoke_kind("tailnet")
        return {"saved": True}

    @app.get("/api/job")
    def job():
        return deps.jobs.snapshot()

    @app.get("/api/status")
    def status(request: Request):
        settings = _settings(deps)
        credentials = ("", "", "") if settings.dry_run else (settings.key, settings.secret, settings.password)
        reader = deps.reader_factory(*credentials)
        ft = deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass)
        snapshot = ops.collect_status(settings, reader, ft, deps.targets_path)
        # Asset history: at most one snapshot per mode every 10 minutes while the panel is polled.
        record_equity(settings, reader, ft)
        snapshot.update(
            keys_configured=deps.keys_configured(), pin_set=_pin_hash(deps.pin_store_path) is not None,
            docker_running=ops.docker_running(deps.runner), job=deps.jobs.snapshot(),
            session_kind=request.state.session_kind,
        )
        if request.state.session_kind == "local":
            snapshot["tailscale"] = ops.tailscale_status(deps.runner)
        return snapshot

    @app.post("/api/steps/{name}")
    def step(name: str, body: dict = Depends(_body)):
        if name not in STEPS:
            raise _PanelError(400, "허용하지 않는 단계입니다")
        with operation_lock:
            if deps.jobs.snapshot()["running"]:
                raise _PanelError(409, "다른 작업이 실행 중입니다")
            answers = {}
            if name == "enter":
                settings = _settings(deps)
                confirmed = body.get("confirm") == "LIVE"
                if not settings.dry_run and not confirmed:
                    raise _PanelError(400, "실전 진입에는 LIVE 확인이 필요합니다")
                if confirmed:
                    answers[LIVE_PROMPT] = "LIVE"

            def restart():
                try:
                    ok = ops.compose_restart(deps.runner)
                except Exception:
                    ok = False
                if not ok:
                    raise RuntimeError("restart failed") from None

            if not deps.jobs.start(name, answers, restart if name == "restart-check" else None):
                raise _PanelError(409, "다른 작업이 실행 중입니다")
        return {"ok": True}

    @app.get("/api/charts")
    def chart_data():
        settings = _settings(deps)
        mode_name = "dry" if settings.dry_run else "live"
        credentials = ("", "", "") if settings.dry_run else (settings.key, settings.secret, settings.password)
        reader = deps.reader_factory(*credentials)
        ft = deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass)
        equity = ops.observe(_equity_reader(settings, reader, ft))
        history = ops.observe(lambda: deps.equity_log.history(mode_name)) or []
        krw = ops.observe(deps.fx.rate) if equity is not None else None
        reachable = ops.observe(ft.ping) is True
        return {
            "mode": mode_name,
            "asset": charts.asset_payload(mode_name, equity, history, krw),
            "btc": ops.observe(lambda: charts.btc_payload(ft)) if reachable else None,
        }

    @app.post("/api/mode")
    def mode(request: Request, body: dict = Depends(_body)):
        with operation_lock:
            if deps.jobs.snapshot()["running"]:
                raise _PanelError(409, "다른 작업이 실행 중입니다")
            value = body.get("mode")
            if not isinstance(value, str) or value not in {"dry", "live"}:
                raise _PanelError(400, "모드는 dry 또는 live로 입력하세요")
            if value == "live" and body.get("confirm") != "LIVE":
                raise _PanelError(400, "실전 전환에는 LIVE 확인이 필요합니다")
            # Stop automatic entries first, so no trade can open between this check and the switch.
            try:
                settings = _settings(deps)
                deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass).pause()
            except Exception:
                pass  # unreachable bot: the open-trade check below still decides
            if _open_trade_blocks_mode(deps):
                raise _PanelError(409, OPEN_TRADE)
            generation = cancel_auto_start()
            deps.mode_setter(value)
            ok = (ops.compose_restart(deps.runner) if ops.freqtrade_container_running(deps.runner)
                  else ops.compose_up(deps.runner))
            if not ok:
                raise _PanelError(503, "모드는 저장했지만 컨테이너 적용에 실패했습니다. 실행 상태를 확인하세요")
            kind = request.state.session_kind
            ident = identity(request.headers) if kind == "tailnet" else None
            # The LIVE confirmation above covers starting the strategy: one step, no second prompt.
            deps.background(lambda: auto_start(value, generation, kind, ident))
        return {"ok": True, "message": MODE_SWITCHED.format(mode="실전" if value == "live" else "모의")}

    @app.post("/api/autotrade")
    def autotrade(body: dict = Depends(_body)):
        with operation_lock:
            if deps.jobs.snapshot()["running"]:
                raise _PanelError(409, "다른 작업이 실행 중입니다")
            action = body.get("action")
            if not isinstance(action, str) or action not in {"start", "pause"}:
                raise _PanelError(400, "자동매매 동작은 start 또는 pause로 입력하세요")
            settings = _settings(deps)
            if action == "start" and not settings.dry_run and body.get("confirm") != "LIVE":
                raise _PanelError(400, "실전 자동매매에는 LIVE 확인이 필요합니다")
            cancel_auto_start()  # a manual choice wins over a pending automatic start
            ft = deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass)
            try:
                # Never start a bot whose running mode differs from the saved one (e.g. a live
                # bot still running after the setting was switched back to dry).
                if ft.show_config().get("dry_run") is not settings.dry_run:
                    raise _PanelError(409, AUTOTRADE_MISMATCH)
                (ft.start if action == "start" else ft.pause)()
            except _PanelError:
                raise
            except Exception:
                raise _PanelError(503, AUTOTRADE_FAILED) from None
        if action == "pause":
            return {"ok": True, "message": AUTOTRADE_PAUSED}
        return {"ok": True, "message": AUTOTRADE_STARTED.format(mode="모의" if settings.dry_run else "실전")}

    @app.post("/api/docker/up")
    def docker_up():
        with operation_lock:
            if deps.jobs.snapshot()["running"]:
                raise _PanelError(409, "다른 작업이 실행 중입니다")
            if not ops.compose_up(deps.runner):
                raise _PanelError(503, "컨테이너 시작에 실패했습니다. Docker Desktop을 확인하세요")
        return {"ok": True}

    @app.post("/api/emergency/close-all")
    def emergency():
        # No job/mode lock and no POST retries, even on an ambiguous timeout.
        # force_exit("all") reaches only the trades of the Freqtrade instance (mode/DB) now running.
        cancel_auto_start()  # never switch automatic trading on after an emergency close
        try:
            settings = _settings(deps)
            deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass).force_exit("all", "market")
        except Exception:
            return JSONResponse({
                "detail": "청산 요청을 확인할 수 없습니다. 비트겟 웹(PC)에서 직접 시장가 청산하세요",
                "link": MANUAL_LINK,
            }, status_code=503)
        scope = EMERGENCY_SCOPE.format(mode="모의" if settings.dry_run else "실전")
        return {"ok": True, "message": f"{scope} {EMERGENCY_DRY if settings.dry_run else EMERGENCY_LIVE}"}

    def _trade_id(raw: str) -> int:
        # ASCII digits only: str.isdigit() also accepts other scripts' digits.
        if not TRADE_ID.fullmatch(raw):
            raise _PanelError(404, TRADE_GONE)
        return int(raw)

    def _price(body: dict) -> float:
        price = body.get("price")
        try:
            valid = type(price) in (int, float) and math.isfinite(float(price)) and float(price) > 0
        except OverflowError:
            valid = False
        if not valid:
            raise _PanelError(400, TRADE_PRICE)
        return float(price)

    def _trade_rows(ft) -> list[dict]:
        try:
            rows = ft.status()
        except Exception:
            raise _PanelError(503, AUTOTRADE_FAILED) from None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _trade_row(rows: list[dict], trade_id: int) -> dict:
        for row in rows:
            if row.get("trade_id") == trade_id:
                if row.get("enter_tag") == ops.SMOKE_TAG:
                    raise _PanelError(409, TRADE_SMOKE)
                return row
        raise _PanelError(404, TRADE_GONE)

    def _trade_client():
        """(settings, client, bot config) once the running bot's mode matches the saved mode."""
        if deps.jobs.snapshot()["running"]:
            raise _PanelError(409, "다른 작업이 실행 중입니다")
        settings = _settings(deps)
        ft = deps.ft_factory(settings.api_url, settings.api_user, settings.api_pass)
        try:
            config = ft.show_config()
        except Exception:
            raise _PanelError(503, AUTOTRADE_FAILED) from None
        # Same rule as /api/autotrade: never act on a bot whose mode differs from the saved one.
        if not isinstance(config, dict) or config.get("dry_run") is not settings.dry_run:
            raise _PanelError(409, AUTOTRADE_MISMATCH)
        return settings, ft, config

    @app.post("/api/trades/{raw_id}/close")
    def close_trade(raw_id: str, body: dict = Depends(_body)):
        trade_id = _trade_id(raw_id)
        if body.get("type") != "market":
            raise _PanelError(400, "종료는 시장가만 됩니다. 원하는 가격에 나가려면 익절가를 설정하세요")
        with operation_lock:
            settings, ft, _ = _trade_client()
            _trade_row(_trade_rows(ft), trade_id)
            cancel_auto_start()  # a manual exit never turns automatic trading on afterwards
            try:
                # One request, never retried: an ambiguous timeout must not double an exit.
                ft.force_exit(trade_id, "market")
            except Exception:
                return JSONResponse({"detail": TRADE_CLOSE_FAILED, "link": MANUAL_LINK}, status_code=503)
        where = "모의 거래입니다." if settings.dry_run else "비트겟 앱에서 체결을 확인하세요."
        return {"ok": True, "message": f"거래 #{trade_id} 시장가 종료를 요청했습니다. {where}"}

    @app.post("/api/trades/{raw_id}/take-profit")
    def take_profit(raw_id: str, body: dict = Depends(_body)):
        trade_id = _trade_id(raw_id)
        price = None if body.get("price") is None else _price(body)
        with operation_lock:
            settings, ft, config = _trade_client()
            if price is not None and not ops.take_profit_supported(config):
                raise _PanelError(409, TP_UNSUPPORTED)
            rows = _trade_rows(ft)
            row = _trade_row(rows, trade_id)
            open_ts = row.get("open_timestamp")
            if type(open_ts) is not int or open_ts <= 0:
                raise _PanelError(503, AUTOTRADE_FAILED)
            if price is not None:
                rate = row.get("current_rate")
                if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
                    raise _PanelError(503, AUTOTRADE_FAILED)
                short = row.get("is_short") is True
                if (price >= rate) if short else (price <= rate):
                    raise _PanelError(400, TP_WRONG_SIDE)
                if not rate * (1 - TP_BAND) <= price <= rate * (1 + TP_BAND):
                    raise _PanelError(400, TP_FAR)
            open_trades = {r["trade_id"]: r.get("open_timestamp") for r in rows if type(r.get("trade_id")) is int}
            try:
                targets.set_target(trade_id, price, open_ts, settings.dry_run, open_trades, deps.targets_path)
            except Exception:
                raise _PanelError(500, TP_SAVE_FAILED) from None
        if price is None:
            return {"ok": True, "message": f"거래 #{trade_id} 익절가를 해제했습니다."}
        return {"ok": True, "message": (
            f"거래 #{trade_id} 익절가 {price:,.1f} 설정. 가격이 닿으면 거래 봇이 시장가로 전량 청산합니다. "
            "그때까지 거래소 손절은 그대로입니다.")}

    @app.post("/api/keys")
    def save_keys(request: Request, body: dict = Depends(_body)):
        require_local(request)
        values = [body.get(name) for name in ("key", "secret", "password")]
        if not all(isinstance(value, str) and value and not any(char.isspace() for char in value) for value in values):
            raise _PanelError(400, "거래소 키는 비어 있거나 공백을 포함할 수 없습니다")
        # The private config is read-modify-written; serialize with mode writes and job admission.
        with operation_lock:
            if deps.jobs.snapshot()["running"]:
                raise _PanelError(409, "다른 작업이 실행 중입니다")
            deps.keys_setter(*values)
        return {"saved": True, "message": KEYS_SAVED}

    @app.post("/api/phone/enable")
    def phone_enable(request: Request):
        require_local(request)
        ok, message = ops.enable_phone_access(deps.runner)
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 503)

    def public_file(name: str):
        path = deps.static_dir / name
        if not path.is_file():
            raise _PanelError(404, "요청한 파일이 없습니다")
        media_type = {"manifest.webmanifest": "application/manifest+json", "sw.js": "application/javascript"}.get(name)
        return FileResponse(path, media_type=media_type)

    @app.get("/manifest.webmanifest")
    def manifest():
        return public_file("manifest.webmanifest")

    @app.get("/sw.js")
    def service_worker():
        return public_file("sw.js")

    app.mount("/static", StaticFiles(directory=deps.static_dir, check_dir=False), name="static")
    return app
