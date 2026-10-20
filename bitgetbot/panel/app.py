"""Loopback control-panel API. Orders stay inside smoke or emergency close-all."""

import json
import math
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from bitgetbot.bitget_read import BitgetReader, make_exchange
from bitgetbot.ftclient import FreqtradeClient
from bitgetbot.panel import ops
from bitgetbot.panel.auth import (
    Lockout, Sessions, classify, csrf_ok, effective_scheme, hash_pin, identity, valid_pin, verify_pin,
)
from bitgetbot.panel.jobs import JobRunner
from bitgetbot.privcfg import Settings, keys_configured, load_settings, set_exchange_keys, set_mode
from bitgetbot.smoke import SmokeIO, run_command


STATIC_DIR = Path(__file__).with_name("static")
STEPS = {"preflight", "enter", "check", "restart-check", "exit", "ledger"}
LIVE_PROMPT = "실전 주문을 냅니다. 계속하려면 LIVE 입력:"
MANUAL_LINK = "https://www.bitget.com/futures/usdt/BTCUSDT"
FAILURE = "요청 실패: 설정·연결 상태를 확인하세요. 오류 원문은 표시하지 않습니다"


class _PanelError(HTTPException):
    """Only panel-authored messages may cross the exception boundary."""


def _reader_factory(key: str, secret: str, password: str):
    return BitgetReader(make_exchange(key, secret, password))


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

    def __post_init__(self):
        if self.jobs is None:
            self.jobs = JobRunner(self._execute, clock=self.clock)
        if self.sessions is None:
            self.sessions = Sessions(clock=self.clock)
        if self.lockout is None:
            self.lockout = Lockout(clock=self.clock)

    def _execute(self, name: str, io: SmokeIO) -> int:
        settings = _settings(self)
        hidden = [self.token, settings.key, settings.secret, settings.password,
                  settings.api_user, settings.api_pass]

        def out(line: str):
            # Defense in depth for upstream identifiers: redact before retaining any job line.
            for value in sorted((value for value in hidden if value), key=len, reverse=True):
                line = line.replace(value, "[REDACTED]")
            io.out(line)

        return self.smoke_command(name, SmokeIO(out, io.ask, io.restart))


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


def create_app(deps: PanelDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    # Serialize mode writes with job admission, not with long-running jobs or emergency exits.
    operation_lock = threading.Lock()

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
            if is_api:
                # csrf_ok's legacy method allowlist does not cover PATCH/OPTIONS or unknown verbs.
                # Every unsafe verb must pass the same check as POST before routing/authentication.
                method = "GET" if request.method in {"GET", "HEAD"} else "POST"
                scheme = effective_scheme(request.url.scheme, host, request.headers)
                if not csrf_ok(method, request.headers, scheme, host):
                    response = JSONResponse({"detail": "요청 출처를 확인할 수 없습니다"}, status_code=403)
                elif request.url.path not in {"/api/ping", "/api/login"}:
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

    @app.post("/api/pin")
    def save_pin(request: Request, body: dict = Depends(_body)):
        require_local(request)
        pin = body.get("pin")
        if not isinstance(pin, str) or not valid_pin(pin):
            raise _PanelError(400, "PIN은 숫자 6~12자리로 입력하세요")
        _save_pin(deps.pin_store_path, pin)
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
        snapshot = ops.collect_status(settings, reader, ft)
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

    @app.post("/api/mode")
    def mode(body: dict = Depends(_body)):
        with operation_lock:
            if deps.jobs.snapshot()["running"]:
                raise _PanelError(409, "다른 작업이 실행 중입니다")
            value = body.get("mode")
            if not isinstance(value, str) or value not in {"dry", "live"}:
                raise _PanelError(400, "모드는 dry 또는 live로 입력하세요")
            if value == "live" and body.get("confirm") != "LIVE":
                raise _PanelError(400, "실전 전환에는 LIVE 확인이 필요합니다")
            deps.mode_setter(value)
            ok = (ops.compose_restart(deps.runner) if ops.freqtrade_container_running(deps.runner)
                  else ops.compose_up(deps.runner))
            if not ok:
                raise _PanelError(503, "모드는 저장했지만 컨테이너 적용에 실패했습니다. 실행 상태를 확인하세요")
        return {"ok": True}

    @app.post("/api/docker/up")
    def docker_up():
        with operation_lock:
            if not ops.compose_up(deps.runner):
                raise _PanelError(503, "컨테이너 시작에 실패했습니다. Docker Desktop을 확인하세요")
        return {"ok": True}

    @app.post("/api/emergency/close-all")
    def emergency():
        # No job/mode lock and no POST retries, even on an ambiguous timeout.
        try:
            _ft(deps).force_exit("all", "market")
        except Exception:
            return JSONResponse({
                "detail": "청산 요청을 확인할 수 없습니다. 비트겟 웹(PC)에서 직접 시장가 청산하세요",
                "link": MANUAL_LINK,
            }, status_code=503)
        return {"ok": True, "message": "시장가 전부 청산을 요청했습니다. 포지션을 확인하세요"}

    @app.post("/api/keys")
    def save_keys(request: Request, body: dict = Depends(_body)):
        require_local(request)
        values = [body.get(name) for name in ("key", "secret", "password")]
        if not all(isinstance(value, str) and value and not any(char.isspace() for char in value) for value in values):
            raise _PanelError(400, "거래소 키는 비어 있거나 공백을 포함할 수 없습니다")
        deps.keys_setter(*values)
        return {"saved": True}

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
