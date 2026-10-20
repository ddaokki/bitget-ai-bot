# 계획 1b: 원클릭 컨트롤 패널 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스모크 경로(점검·진입·확인·재시작 확인·청산·장부)와 모드 전환, API 키 입력, 긴급 전부 청산을 버튼으로 하는 로컬 웹 패널을 만들고, 바탕화면 원클릭 실행과 폰 접속(Tailscale + PIN, PWA)을 붙인다.

**Architecture:** `bitgetbot.smoke`의 명령 본체를 입출력 주입형 `run_command(command, io)`로 꺼내고, FastAPI 패널(`bitgetbot.panel`)이 단일 작업 실행기로 그것을 백그라운드 스레드에서 돌린다. 서버는 `127.0.0.1:8090`에만 바인딩하고, 데스크톱은 실행마다 새 토큰으로, 폰은 `tailscale serve`를 거쳐 PIN으로 세션을 받는다. 주문은 여전히 Freqtrade REST로만 나간다.

**Tech Stack:** Python 3.13, FastAPI 0.150.0, uvicorn 0.54.0, 기존 httpx/ccxt 4.5.84, 바닐라 HTML/CSS/JS(빌드 도구 없음), PowerShell 런처, Edge `--app` 창, Tailscale(사용자 설치).

**Spec:** `docs/superpowers/specs/2026-10-19-bitget-ai-bot-design.md` §10.6 (이 계획의 범위), §11. 로드맵: `docs/superpowers/plans/2026-10-19-roadmap.md`.

## Global Constraints

- 패널 서버 주소는 `127.0.0.1:8090` 고정. 다른 호스트로 바인딩하려 하면 시작을 거부한다.
- 패널이 낼 수 있는 주문은 (1) `bitgetbot.smoke.run_command`의 기존 단계와 (2) 긴급 전부 청산 `FreqtradeClient.force_exit("all", "market")` 뿐이다. 거래소 직접 주문 코드 금지.
- 비밀값(`exchange.key/secret/password`, `api_server.*`, PIN, 세션 ID, 실행 토큰)은 어떤 HTTP 응답·작업 기록·서버 로그에도 나가면 안 된다. 예외 원문도 출력하지 않는다(기존 smoke 규칙 유지).
- 실전 진입(`enter` in live)과 실전 전환(`mode=live`)은 요청 본문 `{"confirm": "LIVE"}`가 정확히 있어야 한다.
- 작업은 한 번에 하나. 실행 중 다른 단계·모드 전환 요청은 HTTP 409.
- 로컬 판정: 클라이언트가 `127.0.0.1`/`::1`이고 Host 헤더가 `127.0.0.1:8090` 또는 `localhost:8090`이며 `Tailscale-User-Login` 헤더가 없을 때만 "local". 그 외는 전부 "tailnet".
- local 세션은 실행 토큰(`GET /?t=<token>`)으로만, tailnet 세션은 PIN 로그인으로만 발급. API 키 저장·PIN 설정·Tailscale 공개 켜기는 local 세션만.
- 모든 POST는 헤더 `X-Panel-Request: 1` 필수, `Origin`이 있으면 `{scheme}://{Host}`와 같아야 함. scheme은 `X-Forwarded-Proto` 헤더가 있으면 그 값, 없고 Host가 `.ts.net`으로 끝나면 `https`(tailscale serve가 TLS를 끝내고 http로 넘기므로), 그 외는 요청 scheme. 세션 쿠키 `panel_session`: HttpOnly, SameSite=Strict, Path=/, tailnet 세션은 Secure.
- PIN: 숫자 6~12자리, `hashlib.scrypt`(n=2**14, r=8, p=1, 16바이트 salt) 해시. 같은 Tailscale 신원(`Tailscale-User-Login`, 없으면 `"unknown"`) 기준 5회 실패 → 15분 잠금, 잠금이 반복되면 2배씩(최대 24시간).
- 세션 TTL 12시간, 서버 메모리 저장(재시작하면 다시 로그인).
- 런타임 파일(모두 gitignore): `.panel/token`, `.panel/panel.json`(PIN 해시). 패널 프로세스의 작업 폴더는 저장소 루트(`.smoke_state.json`, `ledger/` 위치 때문).
- Docker 명령은 항상 `docker compose -f freqtrade/docker-compose.yml ...`, `cwd`=저장소 루트, 타임아웃 지정.
- 사용자 화면 문구는 한국어. 기존 테스트 309개는 계속 통과해야 한다.

## Review Focus

1. **진입 버튼 두 번 클릭 → 주문 두 번** — 두 번째 요청은 409여야 한다. (Task 4: `test_second_step_while_running_is_409`)
2. **폰 요청이 local로 분류돼 PIN을 건너뜀** (tailscale serve는 127.0.0.1에서 프록시) — ts.net Host나 Tailscale 헤더가 있으면 tailnet. (Task 2: `test_classify_tailnet_via_serve_proxy`)
3. **다른 웹사이트가 127.0.0.1:8090으로 몰래 POST (CSRF)** — 헤더 없거나 Origin 불일치면 403. (Task 4: `test_post_without_panel_header_is_403`, `test_post_with_foreign_origin_is_403`)
4. **비밀값이 상태 응답이나 작업 기록에 섞임** — 센티널 비밀값이 어떤 응답에도 없어야 한다. (Task 4: `test_no_secret_in_any_response`)
5. **패널 재시작으로 작업 상태를 잃음** — `.smoke_state.json`이 있으면 상태 응답이 거래 진행 중을 보여 주고 check/exit 버튼을 켜야 한다. (Task 4: `test_status_reports_smoke_state_after_restart`)

---

## File Structure

```
bitgetbot/
  smoke.py                 # (수정) SmokeIO, run_command 추출
  privcfg.py               # (수정) set_exchange_keys, keys_configured
  panel/
    __init__.py
    __main__.py            # python -m bitgetbot.panel: 토큰 파일, uvicorn 127.0.0.1:8090
    auth.py                # 분류, 세션, PIN 해시, 잠금, CSRF 검사 (순수)
    ops.py                 # docker/tailscale 명령, 상태 수집
    jobs.py                # 단일 작업 실행기
    app.py                 # FastAPI 앱 팩토리, 라우트
    static/                # index.html, app.js, style.css, manifest.webmanifest, sw.js, icon-192.png, icon-512.png
launcher/
  start-panel.ps1
  install-shortcut.ps1
docs/runbooks/panel.md     # 한국어: 데스크톱 사용, 폰 연결
tests/
  test_smoke.py            # (수정)
  test_privcfg.py          # (수정)
  panel/test_auth.py, test_ops.py, test_jobs.py, test_app.py, test_static.py
```

---

### Task 1: smoke 명령을 입출력 주입형으로 꺼내기

**Files:**
- Modify: `bitgetbot/smoke.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces:
  - `@dataclass class SmokeIO: out: Callable[[str], None]; ask: Callable[[str], str]; restart: Callable[[], None] | None = None`
  - `SmokeIO.console() -> SmokeIO` — `SmokeIO(print, input, None)`, 호출 시점의 `print`/`input`을 쓴다(테스트의 monkeypatch 호환).
  - `run_command(command: str, io: SmokeIO) -> int` — `main()`의 `try` 블록 본체(설정 로드, 모드 표시, reader/ft 생성, 디스패치, 예외 처리 문구)를 그대로 옮긴 것. `command` ∈ `preflight`, `enter`, `check`, `restart-check`, `exit`, `ledger`. `main()`은 인수를 파싱한 뒤 `init`/`set-mode`는 기존대로, 나머지는 `run_command(command, SmokeIO.console())`.
  - 모든 내부 함수(`_preflight`, `_enter`, `_check`, `_restart_check`, `_ledger`, `_exit`, `_mode_matches`, `_state_mode_matches`, `run_check` 등)는 `print(`/`input(` 대신 `io.out(`/`io.ask(`를 쓴다. `io`는 키워드 인수 `io: SmokeIO | None = None`(None이면 `SmokeIO.console()`).
  - `_restart_check`: `io.restart`가 있으면 안내·Enter 대기 대신 `io.out("컨테이너를 재시작합니다…")` 후 `io.restart()`를 부른다. 없으면 기존 문구와 `io.ask`.

- [ ] **Step 1: 실패하는 테스트**
```python
def test_run_command_uses_injected_io_for_enter_live(monkeypatch, tmp_path):
    # 기존 enter 실전 테스트의 가짜 FT/reader 준비를 재사용
    lines, asked = [], []
    io = SmokeIO(out=lines.append, ask=lambda p: asked.append(p) or "LIVE")
    assert run_command("enter", io) == 0
    assert asked == ["실전 주문을 냅니다. 계속하려면 LIVE 입력:"]
    assert any("진입 확인" in l for l in lines)

def test_restart_check_calls_injected_restart_instead_of_prompt(...):
    # io.restart가 있으면 io.ask를 부르지 않고 restart를 정확히 한 번 호출, 이후 PASS 흐름은 기존과 같음
    assert restarts == 1 and asked == []

def test_console_io_resolves_builtins_at_call_time(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda p: "X")
    assert SmokeIO.console().ask("q") == "X"
```
- [ ] **Step 2: 실패 확인** — `.venv/Scripts/python -m pytest tests/test_smoke.py -k "injected or console_io" -q` → FAIL (`SmokeIO`/`run_command` 없음)
- [ ] **Step 3: 구현** (위 Interfaces 그대로. 기존 문구·종료 코드·순서는 바꾸지 않는다)
- [ ] **Step 4: 통과 확인** — `.venv/Scripts/python -m pytest -q` → 기존 309 + 신규 전부 통과
- [ ] **Step 5: Commit** — `refactor: extract injectable SmokeIO and run_command from smoke CLI`

---

### Task 2: 패널 인증 모듈 (순수 함수)

**Files:**
- Create: `bitgetbot/panel/__init__.py`, `bitgetbot/panel/auth.py`
- Modify: `.gitignore` (`.panel/` 추가)
- Test: `tests/panel/__init__.py`, `tests/panel/test_auth.py`

**Interfaces:**
- Produces (`bitgetbot/panel/auth.py`):
  - `LOCAL_HOSTS = {"127.0.0.1:8090", "localhost:8090"}`
  - `classify(client_host: str, host_header: str, headers: Mapping[str, str]) -> Literal["local","tailnet"]` (Global Constraints의 규칙)
  - `identity(headers) -> str` — `Tailscale-User-Login` 값, 없으면 `"unknown"`
  - `hash_pin(pin: str) -> str` / `verify_pin(pin: str, stored: str) -> bool` — `scrypt$<salt hex>$<hash hex>` 형식, `hmac.compare_digest`
  - `valid_pin(pin: str) -> bool` — `^\d{6,12}$`
  - `class Lockout(clock=time.monotonic)`: `check(ident) -> float` (남은 잠금 초, 0이면 허용), `fail(ident) -> None`, `success(ident) -> None`. 5회 실패 → 900초, 다음 잠금마다 2배, 최대 86400초.
  - `class Sessions(clock=time.time, ttl=43200)`: `create(kind: str) -> str`(`secrets.token_urlsafe(32)`), `get(sid) -> str | None`(kind 또는 만료·없음 None), `revoke(sid)`.
  - `effective_scheme(request_scheme: str, host_header: str, headers) -> str` — Global Constraints의 scheme 규칙.
  - `csrf_ok(method: str, headers, scheme: str, host_header: str) -> bool` — POST/PUT/DELETE는 `X-Panel-Request == "1"` 그리고 Origin이 없거나 `f"{scheme}://{host_header}"`와 같아야 True. GET은 True. 호출부는 `effective_scheme(...)` 결과를 scheme으로 넘긴다.

- [ ] **Step 1: 실패하는 테스트** (`tests/panel/test_auth.py`)
```python
def test_classify_local_direct():
    assert classify("127.0.0.1", "127.0.0.1:8090", {}) == "local"
def test_classify_tailnet_via_serve_proxy():
    assert classify("127.0.0.1", "pc.tail1234.ts.net", {}) == "tailnet"
    assert classify("127.0.0.1", "127.0.0.1:8090", {"Tailscale-User-Login": "u@x"}) == "tailnet"
    assert classify("100.101.1.2", "127.0.0.1:8090", {}) == "tailnet"
def test_pin_rules_and_hash_roundtrip():
    assert valid_pin("123456") and not valid_pin("12345") and not valid_pin("12a456")
    h = hash_pin("123456"); assert h.startswith("scrypt$") and verify_pin("123456", h) and not verify_pin("654321", h)
def test_lockout_escalates_and_caps():
    t=[0.0]; lo=Lockout(clock=lambda: t[0])
    for _ in range(5): lo.fail("a")
    assert lo.check("a") == 900
    t[0]+=900; assert lo.check("a") == 0
    for _ in range(5): lo.fail("a")
    assert lo.check("a") == 1800
def test_sessions_expire():
    t=[0.0]; s=Sessions(clock=lambda: t[0]); sid=s.create("local")
    assert s.get(sid) == "local"; t[0]+=43201; assert s.get(sid) is None
def test_csrf_rules():
    assert not csrf_ok("POST", {}, "http", "127.0.0.1:8090")
    assert csrf_ok("POST", {"X-Panel-Request": "1"}, "http", "127.0.0.1:8090")
    assert not csrf_ok("POST", {"X-Panel-Request": "1", "Origin": "https://evil.example"}, "http", "127.0.0.1:8090")
    assert csrf_ok("GET", {}, "http", "127.0.0.1:8090")
    host = "pc.tail1234.ts.net"
    assert csrf_ok("POST", {"X-Panel-Request": "1", "Origin": "https://" + host}, effective_scheme("http", host, {}), host)
```
- [ ] **Step 2: 실패 확인** — `pytest tests/panel/test_auth.py -q` → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, 전체 스위트 PASS
- [ ] **Step 5: Commit** — `feat: add panel auth primitives`

---

### Task 3: 운영 명령과 상태 수집

**Files:**
- Create: `bitgetbot/panel/ops.py`
- Modify: `bitgetbot/privcfg.py`
- Test: `tests/panel/test_ops.py`, `tests/test_privcfg.py`

**Interfaces:**
- Produces (`bitgetbot/privcfg.py`):
  - `set_exchange_keys(key: str, secret: str, password: str, path: Path = PRIVATE_PATH) -> None` — 세 값만 원자적으로 교체(set_mode와 같은 임시 파일 + `os.replace`), 빈 값·공백 포함 값은 `ValueError`, 출력 없음.
  - `keys_configured(path: Path = PRIVATE_PATH) -> dict[str, bool]` — `{"key","secret","password"}` 설정 여부만.
- Produces (`bitgetbot/panel/ops.py`):
  - `Runner = Callable[[list[str], int], tuple[int, str]]` — (argv, timeout_s) → (exit code, 출력). 기본 구현은 `subprocess.run(..., cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=...)`, 타임아웃이면 `(124, "")`.
  - `REPO_ROOT: Path` — 이 파일 기준 저장소 루트.
  - `COMPOSE = ["docker", "compose", "-f", "freqtrade/docker-compose.yml"]`
  - `docker_running(run=default_runner) -> bool` (`docker info` 종료 코드 0)
  - `compose_up(run=...) -> bool`, `compose_restart(run=...) -> bool`, `freqtrade_container_running(run=...) -> bool` (`compose ps --format json`에서 `freqtrade` 서비스 State=="running")
  - `tailscale_status(run=...) -> dict` — `{"installed": bool, "logged_in": bool, "dns_name": str|None, "serving": bool, "funnel": bool}`. `tailscale status --json`의 `Self.DNSName`·`BackendState`, `tailscale serve status --json`, `tailscale funnel status --json`을 해석. tailscale이 없으면 installed False.
  - `enable_phone_access(run=...) -> tuple[bool, str]` — funnel이 켜져 있으면 거부. `tailscale serve --bg 8090` 실행 후 `tailscale_status()` 재확인. 반환 메시지는 한국어, URL은 `https://{dns_name}`(끝 점 제거).
  - `collect_status(settings, reader, ft) -> dict` — 키: `mode`("dry"/"live"), `freqtrade`(`{"reachable","state","dry_run"}`), `account`(실전만: `{"kind","usdt_available"}`, 실패 시 None), `positions`(실전은 reader, 모의는 ft `/status`에서: `symbol, side, size, entry, mark, liq, leverage`), `stops`(실전만: `plan_type, trigger_price, trigger_type`), `smoke_state`(`{"exists","trade_id","mode","close_confirmed"}`). 어떤 오류도 문자열로 내보내지 않고 해당 칸을 None으로.

- [ ] **Step 1: 실패하는 테스트** — 가짜 runner로 각 docker/tailscale 함수의 argv와 해석 결과, `enable_phone_access`가 funnel 켜짐이면 serve를 부르지 않음, `collect_status`가 센티널 비밀값을 담은 설정·예외에서도 비밀값과 예외 원문을 내보내지 않음, 모의 모드에서 reader private 메서드를 부르지 않음, `set_exchange_keys`가 다른 키를 보존하고 실패 시 원본 보존, `keys_configured`가 값이 아닌 불리언만 반환.
- [ ] **Step 2: 실패 확인** — `pytest tests/panel/test_ops.py tests/test_privcfg.py -q` → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, 전체 PASS
- [ ] **Step 5: Commit** — `feat: add panel ops, status collection and key storage`

---

### Task 4: 작업 실행기 + FastAPI 앱 + 서버 진입점

**Files:**
- Create: `bitgetbot/panel/jobs.py`, `bitgetbot/panel/app.py`, `bitgetbot/panel/__main__.py`
- Modify: `requirements.txt` (`fastapi==0.150.0`, `uvicorn==0.54.0`)
- Test: `tests/panel/test_jobs.py`, `tests/panel/test_app.py`

**Interfaces:**
- Consumes: Task 1 `run_command`, `SmokeIO`; Task 2 전부; Task 3 전부; 기존 `FreqtradeClient`, `BitgetReader`, `make_exchange`, `load_settings`, `set_mode`.
- Produces (`jobs.py`): `class JobRunner(execute: Callable[[str, SmokeIO], int])` — `start(name: str, io_answers: dict[str, str], restart: Callable[[], None] | None) -> bool`(실행 중이면 False), `snapshot() -> dict`(`name, running, exit_code, lines, started_at, finished_at`), 기록은 최근 500줄. 실행 함수의 예외는 잡아서 `"작업 실패: 오류 원문은 표시하지 않습니다"`와 exit_code 1. `ask`는 `io_answers`에 없는 질문이면 빈 문자열을 반환(= 취소).
- Produces (`app.py`): `create_app(deps: PanelDeps) -> FastAPI`. `PanelDeps`(테스트 주입용): `token: str`, `settings_loader`, `reader_factory`, `ft_factory`, `runner`(ops Runner), `jobs: JobRunner`, `sessions`, `lockout`, `pin_store_path: Path`, `clock`.
  라우트(모두 JSON, 한국어 메시지):
  - `GET /api/ping` → `{"ok": true}` (인증 없음)
  - `GET /?t=<token>` → 토큰 일치 + local이면 local 세션 쿠키 발급 후 `/`로 303. 그 외 `GET /`는 정적 `index.html`.
  - `POST /api/login {"pin"}` → tailnet 전용. PIN 미설정이면 409 "PC에서 PIN을 먼저 설정하세요". 잠금이면 429와 남은 초. 성공 시 tailnet 세션(Secure 쿠키).
  - `POST /api/pin {"pin"}` → local 세션만, `valid_pin` 아니면 400. `.panel/panel.json`에 해시 저장(원자적).
  - `GET /api/status` → 세션 필수. `collect_status` + `keys_configured` + `pin_set` + `docker_running` + `tailscale_status`(local만) + `jobs.snapshot()` + `session_kind`.
  - `POST /api/steps/{name}` → 세션 필수, name 검증, 실전 `enter`는 `{"confirm":"LIVE"}` 필수(아니면 400, 작업 시작 안 함). 실행 중이면 409. `restart-check`는 `restart=lambda: compose_restart(runner)`. `enter`의 답은 `{"실전 주문을 냅니다. 계속하려면 LIVE 입력:": "LIVE"}`(확인했을 때만).
  - `POST /api/mode {"mode", "confirm"?}` → 세션 필수, 작업 중 409, live는 `confirm=="LIVE"` 필수. `set_mode` 후 컨테이너가 돌고 있으면 `compose_restart`, 아니면 `compose_up`.
  - `POST /api/docker/up` → 세션 필수, `compose_up`.
  - `POST /api/emergency/close-all` → 세션 필수, 작업 실행 중이어도 허용(작업과 별개), `ft.force_exit("all","market")` 한 번(재시도 없음). Freqtrade 불통이면 503과 "비트겟 웹(PC)에서 직접 시장가 청산하세요" + 링크 `https://www.bitget.com/futures/usdt/BTCUSDT`.
  - `POST /api/keys {"key","secret","password"}` → local만, `set_exchange_keys`, 응답 `{"saved": true}`.
  - `POST /api/phone/enable` → local만, `enable_phone_access`.
  - 미들웨어: 모든 POST에 `csrf_ok`(실패 403), 세션 없는 보호 라우트 401. 응답 헤더 `Cache-Control: no-store`(API), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`.
- Produces (`__main__.py`): `.panel/token`에 새 토큰 기록(사용자 전용, 매 실행 교체), `uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning", access_log=False)`. 작업 폴더가 저장소 루트가 아니면 시작 거부.

- [ ] **Step 1: 실패하는 테스트** (`TestClient`, 가짜 의존성)
```python
def test_token_login_sets_local_session_and_redirects(): ...
def test_wrong_token_does_not_create_session(): ...
def test_tailnet_requires_pin_and_status_401_without_session(): ...
def test_login_lockout_returns_429(): ...
def test_post_without_panel_header_is_403(): ...
def test_post_with_foreign_origin_is_403(): ...
def test_enter_live_requires_confirm(): ...      # 확인 없으면 400, 작업 미시작
def test_second_step_while_running_is_409(): ...
def test_mode_live_requires_confirm_and_restarts_container(): ...
def test_keys_and_pin_are_local_only(): ...      # tailnet 세션은 403
def test_emergency_close_all_calls_force_exit_once(): ...
def test_emergency_close_all_ft_down_is_503_with_manual_link(): ...
def test_no_secret_in_any_response(): ...         # 센티널 key/secret/password/api_pass/PIN/토큰이 모든 응답 본문·헤더에 없음
def test_status_reports_smoke_state_after_restart(): ...
def test_restart_check_job_uses_compose_restart(): ...
```
`tests/panel/test_jobs.py`: 동시 시작 거부, 예외 시 원문 비노출, 500줄 상한, 미지 질문은 빈 답.
- [ ] **Step 2: 실패 확인** — `pytest tests/panel -q` → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `pytest -q` 전체 PASS
- [ ] **Step 5: Commit** — `feat: add panel server with jobs, auth and routes`

---

### Task 5: 화면(정적 파일) + PWA

**Files:**
- Create: `bitgetbot/panel/static/index.html`, `app.js`, `style.css`, `manifest.webmanifest`, `sw.js`, `icon-192.png`, `icon-512.png`
- Test: `tests/panel/test_static.py`

**요구 화면** (스펙 §10.2 시안 A 톤, 다크, 한국어, 폭 600px 미만은 폰 1단, 이상은 2단):
- 상단: 모드 배지(모의 노랑/실전 빨강, 크게), 상태 칩(Docker, Freqtrade 상태, 계정 모드, 사용 가능 USDT).
- 포지션 카드: 코인, 방향, 수량, 진입가, 현재가, 청산가, 레버리지, 손절가(전부 달러 표기). 손절 후보가 여러 개면 모두 표시.
- 단계 버튼(순서): 사전 점검 → 진입 → 손절 확인 → 재시작 확인 → 청산 → 장부 저장. 활성 규칙: 작업 중이면 전부 비활성. 상태 파일 없으면 점검·진입만, 진입은 이번 화면에서 점검이 성공(exit_code 0)한 뒤에만. 상태 파일 있으면 손절 확인·재시작 확인·청산. 장부 저장은 실전 + 상태 파일의 `close_confirmed`.
- 실전 진입·실전 전환: 모달에 `LIVE` 입력해야 확인 버튼 활성.
- 긴급 "전부 청산"(빨강, 항상 보임, 2단 확인: 누르고 3초 안에 한 번 더) + "비트겟 웹 열기" 링크.
- 진행 기록 창: 작업 중 1초마다 `/api/job` 폴링, 아니면 상태 5초마다.
- 설정(로컬 세션만 보임): API 키 입력(비밀번호형 입력칸, 저장 후 비우고 "설정됨"만 표시), PIN 설정, 폰 연결(Tailscale 설치·로그인·공개 상태, "폰 접속 켜기" 버튼, 주소 표시).
- tailnet에서 세션 없으면 PIN 로그인 화면.
- 모든 POST에 `X-Panel-Request: 1`. 인라인 스크립트·외부 CDN 금지(CSP `default-src 'self'`).
- PWA: `manifest.webmanifest`(name "비트겟 봇", `display: standalone`, `start_url: "/"`, 아이콘 192/512), `sw.js`는 정적 셸만 캐시하고 `/api/*`는 절대 캐시하지 않음. iOS용 `apple-touch-icon`.

- [ ] **Step 1: 실패하는 테스트** — `/`가 index.html을 주고 `<script src="/static/app.js">`가 있으며 인라인 `<script>`가 없음, manifest가 JSON이고 필수 키를 가짐, sw.js에 `/api/` 캐시 제외 분기가 있음, 아이콘 PNG 시그니처.
- [ ] **Step 2: 실패 확인** — `pytest tests/panel/test_static.py -q` → FAIL
- [ ] **Step 3: 구현** (PNG 아이콘은 표준 라이브러리 `zlib`/`struct`로 쓴 임시 스크립트로 만들고, 결과 PNG만 커밋한다)
- [ ] **Step 4: 통과 확인** — 전체 PASS
- [ ] **Step 5: Commit** — `feat: add panel UI and PWA shell`

---

### Task 6: 원클릭 런처 + 바로가기 + 안내서

**Files:**
- Create: `launcher/start-panel.ps1`, `launcher/install-shortcut.ps1`, `docs/runbooks/panel.md`

**`start-panel.ps1` 순서** (저장소 루트로 이동 후):
1. `docker info`가 실패하면: Docker Desktop 프로세스가 없을 때만 `$env:LOCALAPPDATA\Docker\run\dockerInference`가 있으면 그 파일 하나만 지움 → `C:\Program Files\Docker\Docker\Docker Desktop.exe` 실행 → 최대 120초 `docker info` 대기. 실패하면 한국어 메시지 상자로 안내하고 종료.
2. `docker compose -f freqtrade/docker-compose.yml up -d`.
3. `http://127.0.0.1:8090/api/ping`이 응답 없으면 `.venv\Scripts\pythonw.exe -m bitgetbot.panel`을 숨김 창, 작업 폴더=저장소 루트로 시작 → 최대 20초 대기.
4. `.panel\token`을 읽어 `msedge.exe --app=http://127.0.0.1:8090/?t=<token>`(Edge 경로: `${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe`, 없으면 기본 브라우저)로 연다. 토큰을 화면이나 로그에 출력하지 않는다.
**`install-shortcut.ps1`**: 바탕화면에 "비트겟 봇.lnk" 생성 → 대상 `powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "<repo>\launcher\start-panel.ps1"`, 작업 폴더 저장소 루트, 아이콘은 `bitgetbot\panel\static\icon-192.png`에서 만든 `.ico`가 없으면 기본.
**`panel.md`**: 바로가기 설치 1회, 데스크톱 사용(버튼 순서, LIVE 확인, 긴급 청산), PIN 설정, 폰 연결(PC·폰에 Tailscale 설치·같은 계정 로그인, Tailscale 관리 콘솔에서 HTTPS 인증서 사용 켜기, 패널의 "폰 접속 켜기", 폰 브라우저로 주소 열기 → PIN → 홈 화면에 추가), Funnel 절대 켜지 않기, PC가 켜져 있어야 함, 문제 해결(Docker 오류, 패널 안 열림).

- [ ] **Step 1: 스크립트 작성**
- [ ] **Step 2: 검증** — `powershell -NoProfile -ExecutionPolicy Bypass -File launcher\install-shortcut.ps1` → 바탕화면에 바로가기 생성 확인. `start-panel.ps1` 실행 → 패널이 Edge 앱 창으로 열리고 `GET /api/ping` 200. 패널 프로세스가 `127.0.0.1:8090`에서만 듣는지 `Get-NetTCPConnection -LocalPort 8090` 확인.
- [ ] **Step 3: Commit** — `feat: add one-click launcher, desktop shortcut and panel runbook`

---

## 실행 후 검증 (컨트롤러)

- 모의 모드에서 패널로 점검 → 진입 → 손절 확인 → 재시작 확인 → 청산 전 과정을 내장 브라우저로 클릭해 확인(실제 Docker).
- 실전 단계와 Tailscale 설치·로그인은 사용자가 한다.
