# 계획 1: 스모크 테스트 최소 경로 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 사용자 PC에서 Freqtrade로 비트겟 USDT-M 선물 최소 주문을 실전으로 내고, 거래소 손절(mark 기준 `pos_loss`)이 걸리는지, 포지션이 열린 채 재시작해도 유지되는지 확인하고, 청산한 뒤 장부 CSV를 남긴다.

**Architecture:** Freqtrade 2026.9 Docker 컨테이너가 주문·손절을 전부 맡는다. 호스트의 Python 패키지 `bitgetbot`은 Freqtrade REST API로 진입·청산을 요청하고, 비트겟은 **조회만** 한다(이 계획에는 거래소에 직접 주문하는 코드가 없다). `python -m bitgetbot.smoke <명령>` CLI가 단계를 안내한다.

**Tech Stack:** Python 3.13(호스트, `py -3.13`), ccxt 4.5.84, httpx, PyYAML, pytest. Freqtrade `freqtradeorg/freqtrade:2026.9`(Docker Desktop, Windows).

**Spec:** `docs/superpowers/specs/2026-10-19-bitget-ai-bot-design.md` (§6.1, §6.2, §11, §13-1·2가 이 계획의 범위). 로드맵: `docs/superpowers/plans/2026-10-19-roadmap.md`.

## Global Constraints

- Freqtrade 이미지: `freqtradeorg/freqtrade:2026.9@sha256:989aff5636d8f0213ed44fe334c3314a194a51c0c755d16866960247aafd2eae` (amd64·arm64 포함 확인됨).
- 호스트 ccxt는 Freqtrade 2026.9와 같은 `ccxt==4.5.84`. 비트겟 거래소 객체 옵션: `{"defaultType": "swap", "uta": False}`.
- 가짜 PyPI 패키지(`python-bitget-api`, `python-bitget-connect`, `python-bitget-request`, `python-bitget-wrapper`) 금지. 의존성은 `requirements.txt`에 `==`로 고정.
- 페어 표기: Freqtrade `BTC/USDT:USDT`, 비트겟 v2 `BTCUSDT`, productType `USDT-FUTURES`, marginCoin `USDT`.
- 격리 마진, 원웨이, `stoploss_on_exchange=true`, `stoploss_price_type="mark"`, `force_exit="market"`, 텔레그램 끔, `initial_state="stopped"`.
- 손절 규칙: |진입가 − 손절가| ≤ 0.7 × |진입가 − 청산가|.
- 스모크 주문: `BTC/USDT:USDT` 롱, 레버리지 2, 시장가, 증거금 10 USDT 이하, entry_tag `smoke`.
- 비밀값(`exchange.key/secret/password`, `api_server.username/password/jwt_secret_key`)은 `freqtrade/user_data/config.private.json`에만. 깃·로그·화면 출력 금지. 커밋되는 `config.json`의 해당 값은 빈 문자열.
- Freqtrade API는 호스트 `127.0.0.1:8080`에만 공개.
- 사용자에게 보이는 CLI 문구는 한국어.

## Review Focus

1. **진입했는데 거래소 손절이 안 걸림** — `check`가 120초 안에 PASS를 못 받으면 FAIL과 함께 바로 `exit` 실행을 안내해야 한다. (Task 5: `test_check_fails_when_no_stop_order`)
2. **이미 포지션·미체결 주문·Freqtrade 열린 거래가 있는데 진입** — 원웨이에서 합쳐지고 `forceenter`가 물타기로 바뀐다. `preflight`·`enter`가 거부해야 한다. (Task 5: `test_preflight_rejects_existing_exposure`)
3. **dry-run인 줄 알고 실전 주문 / 실전인 줄 알고 dry-run** — 모드를 크게 보여 주고, 실전이면 `LIVE` 입력을 받아야 진입. (Task 5: `test_live_requires_typed_confirmation`)
4. **비트겟 응답의 숫자가 문자열이거나 비어 있음**(`liquidationPrice: "0"`, `""`) — 크래시 대신 해당 검사를 "알 수 없음"으로. (Task 2: `test_parse_position_blank_liq_is_none`)
5. **Freqtrade가 안 떠 있거나 인증이 틀림** — 진입 전에 명확한 한국어 오류로 멈춰야 한다. (Task 3: `test_unreachable_raises_ft_unreachable`, Task 5: `test_preflight_reports_ft_unreachable`)

---

## File Structure

```
bitget-ai-bot/
  pyproject.toml                       # 패키지 메타 + pytest 설정
  requirements.txt                     # 고정 의존성
  .gitignore                           # (수정) 비밀값·런타임 파일
  bitgetbot/
    __init__.py
    models.py                          # PositionSnapshot, PlanOrder, Check, ContractSpec
    stopcheck.py                       # 손절 보호 판정(순수 함수)
    bitget_read.py                     # 비트겟 조회 전용 어댑터
    ftclient.py                        # Freqtrade REST 클라이언트
    ledger.py                          # 체결 → 장부 행, CSV, 업비트 환율
    privcfg.py                         # config.private.json 생성·읽기
    smoke.py                           # 스모크 테스트 CLI
  freqtrade/
    docker-compose.yml
    user_data/
      config.json                      # 비밀값 없는 기본 설정(커밋)
      config.private.example.json      # 비밀값 자리 예시(커밋)
      strategies/SmokeTest.py
  tests/
    test_stopcheck.py
    test_bitget_read.py
    test_ftclient.py
    test_ledger.py
    test_freqtrade_config.py
    test_privcfg.py
    test_smoke.py
  docs/runbooks/smoke-test.md          # 사용자용 단계 안내(한국어)
```

---

### Task 1: 프로젝트 골격 + 손절 보호 판정

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `bitgetbot/__init__.py`, `bitgetbot/models.py`, `bitgetbot/stopcheck.py`
- Modify: `.gitignore`
- Test: `tests/test_stopcheck.py`

**Interfaces:**
- Produces (`bitgetbot/models.py`, 모두 `@dataclass(frozen=True)`):
  - `PositionSnapshot(symbol: str, side: Literal["long","short"], size: float, entry_price: float, mark_price: float | None, liq_price: float | None, leverage: float | None, margin_mode: str | None)`
  - `PlanOrder(symbol: str, plan_type: str, trigger_price: float, trigger_type: str, size: float | None, order_id: str)`
  - `Check(name: str, ok: bool | None, detail: str)` — `ok=None`은 "알 수 없음"
  - `ContractSpec(symbol: str, min_qty: float, size_step: float, min_notional_usdt: float)`
- Produces (`bitgetbot/stopcheck.py`):
  - `MAX_LIQ_FRACTION = 0.7`
  - `evaluate_protection(pos: PositionSnapshot, plans: list[PlanOrder], max_liq_fraction: float = MAX_LIQ_FRACTION) -> list[Check]` — 검사 이름은 정확히 `margin_isolated`, `stop_exists`, `stop_trigger_mark`, `stop_side`, `stop_inside_liq`, `stop_covers_size`.
  - `overall(checks: list[Check]) -> Literal["PASS","FAIL","UNKNOWN"]` — 하나라도 `False`면 FAIL, 아니면 하나라도 `None`이면 UNKNOWN, 아니면 PASS.

- [ ] **Step 1: 골격 파일 작성**

`requirements.txt`:
```
ccxt==4.5.84
httpx==0.28.1
PyYAML==6.0.2
pytest==8.3.5
```
`pyproject.toml`: 프로젝트명 `bitgetbot`, `requires-python = ">=3.13"`, `[tool.pytest.ini_options] testpaths = ["tests"]`.
`.gitignore`에 추가: `**/config.private.json`, `**/user_data/data/`, `**/user_data/logs/`, `**/user_data/*.sqlite*`, `ledger/`, `.smoke_state.json`. (기존 `user_data/data/` 같은 줄은 루트 기준이라 `freqtrade/user_data/`에 안 걸린다.)
가상환경: `py -3.13 -m venv .venv` → `.venv/Scripts/python -m pip install -r requirements.txt`.

- [ ] **Step 2: 실패하는 테스트 작성** (`tests/test_stopcheck.py`)

```python
from bitgetbot.models import PositionSnapshot, PlanOrder
from bitgetbot.stopcheck import evaluate_protection, overall

LONG = PositionSnapshot("BTCUSDT", "long", 0.0001, 111240.5, 112050.0, 56300.0, 2, "isolated")
STOP_OK = PlanOrder("BTCUSDT", "pos_loss", 108900.0, "mark_price", None, "1")

def by_name(checks): return {c.name: c for c in checks}

def test_long_with_mark_pos_loss_inside_liq_passes():
    checks = evaluate_protection(LONG, [STOP_OK])
    assert overall(checks) == "PASS"
    assert set(by_name(checks)) == {"margin_isolated","stop_exists","stop_trigger_mark","stop_side","stop_inside_liq","stop_covers_size"}

def test_missing_stop_fails():
    c = by_name(evaluate_protection(LONG, []))
    assert c["stop_exists"].ok is False
    assert overall(list(c.values())) == "FAIL"

def test_fill_price_trigger_fails():
    plan = PlanOrder("BTCUSDT", "pos_loss", 108900.0, "fill_price", None, "1")
    assert by_name(evaluate_protection(LONG, [plan]))["stop_trigger_mark"].ok is False

def test_stop_beyond_70pct_of_liq_distance_fails():
    plan = PlanOrder("BTCUSDT", "pos_loss", 70000.0, "mark_price", None, "1")
    assert by_name(evaluate_protection(LONG, [plan]))["stop_inside_liq"].ok is False

def test_short_stop_must_be_above_entry_and_inside_liq():
    short = PositionSnapshot("ETHUSDT", "short", 0.01, 3000.0, 2990.0, 3140.0, 20, "isolated")
    ok = PlanOrder("ETHUSDT", "pos_loss", 3090.0, "mark_price", None, "2")   # 90 <= 0.7*140 = 98
    bad = PlanOrder("ETHUSDT", "pos_loss", 3110.0, "mark_price", None, "3")  # 110 > 98
    wrong_side = PlanOrder("ETHUSDT", "pos_loss", 2900.0, "mark_price", None, "4")
    assert overall(evaluate_protection(short, [ok])) == "PASS"
    assert by_name(evaluate_protection(short, [bad]))["stop_inside_liq"].ok is False
    assert by_name(evaluate_protection(short, [wrong_side]))["stop_side"].ok is False

def test_unknown_liq_gives_unknown_not_crash():
    pos = PositionSnapshot("BTCUSDT", "long", 0.0001, 111240.5, 112050.0, None, 2, "isolated")
    checks = evaluate_protection(pos, [STOP_OK])
    assert by_name(checks)["stop_inside_liq"].ok is None
    assert overall(checks) == "UNKNOWN"

def test_cross_margin_fails():
    pos = PositionSnapshot("BTCUSDT", "long", 0.0001, 111240.5, 112050.0, 56300.0, 2, "crossed")
    assert by_name(evaluate_protection(pos, [STOP_OK]))["margin_isolated"].ok is False

def test_partial_loss_plan_does_not_cover_size():
    plan = PlanOrder("BTCUSDT", "loss_plan", 108900.0, "mark_price", 0.00005, "5")
    assert by_name(evaluate_protection(LONG, [plan]))["stop_covers_size"].ok is False

def test_plans_for_other_symbols_are_ignored():
    other = PlanOrder("ETHUSDT", "pos_loss", 108900.0, "mark_price", None, "6")
    assert by_name(evaluate_protection(LONG, [other]))["stop_exists"].ok is False
```

- [ ] **Step 3: 실패 확인**

Run: `.venv/Scripts/python -m pytest tests/test_stopcheck.py -v`
Expected: FAIL (`ModuleNotFoundError: bitgetbot.models`)

- [ ] **Step 4: `models.py`와 `stopcheck.py` 구현**

손절 후보 = 같은 심볼의 `plan_type in {"pos_loss","loss_plan"}` 주문. 여러 개면 진입가에 가장 가까운 것을 판정에 쓴다. `pos_loss`는 수량과 무관하게 전체를 덮고, `loss_plan`은 `size >= pos.size`일 때만 덮는다. `stop_side`는 롱이면 손절가 < 현재 mark, 숏이면 > 현재 mark(mark가 None이면 진입가로 대신). 추적 손절이 진입가를 넘어 올라간 정상 상태를 실패로 보지 않기 위함. 후보가 없으면 `stop_exists`만 False이고 나머지 손절 관련 검사는 `ok=None`.

- [ ] **Step 5: 통과 확인**

Run: `.venv/Scripts/python -m pytest tests/test_stopcheck.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt .gitignore bitgetbot tests/test_stopcheck.py
git commit -m "feat: add protection checks for exchange stop-loss"
```

---

### Task 2: 비트겟 조회 어댑터

**Files:**
- Create: `bitgetbot/bitget_read.py`
- Test: `tests/test_bitget_read.py`

**Interfaces:**
- Consumes: Task 1 모델.
- Produces:
  - `make_exchange(key: str, secret: str, password: str) -> ccxt.bitget` — 옵션 `{"defaultType": "swap", "uta": False}`, `enableRateLimit=True`.
  - `class BitgetReader(exchange)` (exchange는 ccxt 객체 또는 같은 메서드를 가진 가짜):
    - `fetch_positions() -> list[PositionSnapshot]` — `privateMixGetV2MixPositionAllPosition({"productType":"USDT-FUTURES","marginCoin":"USDT"})`, `data` 목록. 필드: `symbol`, `holdSide`, `total`, `openPriceAvg`, `markPrice`, `liquidationPrice`, `leverage`, `marginMode`. `total`이 0이면 제외.
    - `fetch_position(symbol: str) -> PositionSnapshot | None`
    - `fetch_stop_orders(symbol: str) -> list[PlanOrder]` — `privateMixGetV2MixOrderOrdersPlanPending({"productType":"USDT-FUTURES","planType":"profit_loss","symbol":symbol})`, `data.entrustedList`. 필드: `planType`, `triggerPrice`, `triggerType`, `size`, `orderId`.
    - `fetch_open_order_count() -> int` — `privateMixGetV2MixOrderOrdersPending({"productType":"USDT-FUTURES"})`의 `data.entrustedList` 길이(없으면 0).
    - `fetch_usdt_available() -> float` — `privateMixGetV2MixAccountAccounts({"productType":"USDT-FUTURES"})`에서 `marginCoin == "USDT"`인 항목의 `available`.
    - `fetch_contract(symbol: str) -> ContractSpec` — `publicMixGetV2MixMarketContracts({"productType":"USDT-FUTURES","symbol":symbol})`, 필드 `minTradeNum`, `sizeMultiplier`, `minTradeUSDT`.
    - `account_mode_report() -> dict` — `{"v2_ok": bool, "v3_ok": bool, "v3_data": dict | None, "v3_error": str | None}`. v2는 `privateMixGetV2MixAccountAccounts`, v3는 `privateUtaGetV3AccountSettings`. 예외 메시지에 비밀값이 섞이지 않게 예외 클래스 이름 + 비트겟 오류 코드만 저장.
  - `classify_account(report: dict) -> Literal["classic","uta","unknown"]` — v2 성공 + v3 실패 = classic, v2 실패 + v3 성공 = uta, 그 외 unknown.
  - `num(x) -> float | None` — `None`, `""`, 숫자로 못 바꾸는 값은 None. `liq_price`는 0 이하도 None.

- [ ] **Step 1: 실패하는 테스트 작성** (`tests/test_bitget_read.py`)

가짜 거래소 `FakeEx`를 테스트 안에 정의: 위 메서드 이름을 가진 메서드가 생성자로 받은 응답 dict를 그대로 돌려주거나, 지정된 예외를 던진다. 응답은 비트겟 원문처럼 숫자를 문자열로 둔다.

```python
def test_parse_position_long_isolated():
    ex = FakeEx(positions={"code":"00000","data":[{"symbol":"BTCUSDT","holdSide":"long","total":"0.0001",
        "openPriceAvg":"111240.5","markPrice":"112050","liquidationPrice":"56300","leverage":"2","marginMode":"isolated"}]})
    p = BitgetReader(ex).fetch_position("BTCUSDT")
    assert (p.side, p.size, p.entry_price, p.liq_price, p.leverage, p.margin_mode) == ("long", 0.0001, 111240.5, 56300.0, 2.0, "isolated")

def test_parse_position_blank_liq_is_none():
    ex = FakeEx(positions={"code":"00000","data":[{"symbol":"BTCUSDT","holdSide":"long","total":"0.0001",
        "openPriceAvg":"111240.5","markPrice":"","liquidationPrice":"0","leverage":"2","marginMode":"isolated"}]})
    p = BitgetReader(ex).fetch_position("BTCUSDT")
    assert p.liq_price is None and p.mark_price is None

def test_zero_total_position_is_skipped():
    ex = FakeEx(positions={"code":"00000","data":[{"symbol":"BTCUSDT","holdSide":"long","total":"0"}]})
    assert BitgetReader(ex).fetch_position("BTCUSDT") is None

def test_stop_orders_filtered_to_loss_types():
    ex = FakeEx(plans={"code":"00000","data":{"entrustedList":[
        {"planType":"pos_loss","triggerPrice":"108900","triggerType":"mark_price","size":"","orderId":"9"},
        {"planType":"pos_profit","triggerPrice":"130000","triggerType":"mark_price","size":"","orderId":"10"}]}})
    orders = BitgetReader(ex).fetch_stop_orders("BTCUSDT")
    assert [(o.plan_type, o.trigger_price, o.size, o.order_id) for o in orders] == [("pos_loss", 108900.0, None, "9")]

def test_stop_orders_empty_list_when_data_null():
    assert BitgetReader(FakeEx(plans={"code":"00000","data":None})).fetch_stop_orders("BTCUSDT") == []

def test_classify_account():
    assert classify_account({"v2_ok": True, "v3_ok": False, "v3_data": None, "v3_error": "40084"}) == "classic"
    assert classify_account({"v2_ok": False, "v3_ok": True, "v3_data": {}, "v3_error": None}) == "uta"
    assert classify_account({"v2_ok": True, "v3_ok": True, "v3_data": {}, "v3_error": None}) == "unknown"

def test_account_report_error_has_no_secret():
    ex = FakeEx(v3_raises=Exception('bitget {"code":"40084","msg":"x"} apiKey=SECRETKEY'))
    rep = BitgetReader(ex).account_mode_report()
    assert "SECRETKEY" not in repr(rep)

def test_contract_spec():
    ex = FakeEx(contracts={"code":"00000","data":[{"symbol":"BTCUSDT","minTradeNum":"0.0001","sizeMultiplier":"0.0001","minTradeUSDT":"5"}]})
    assert BitgetReader(ex).fetch_contract("BTCUSDT") == ContractSpec("BTCUSDT", 0.0001, 0.0001, 5.0)
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python -m pytest tests/test_bitget_read.py -v`
Expected: FAIL (`ModuleNotFoundError: bitgetbot.bitget_read`)

- [ ] **Step 3: `bitget_read.py` 구현** (위 Interfaces 그대로. 오류 코드는 예외 문자열에서 `"code":"(\d+)"` 정규식으로만 뽑는다.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/Scripts/python -m pytest tests/test_bitget_read.py -v`
Expected: 8 passed

- [ ] **Step 5: ccxt 메서드 이름 실존 확인**

Run: `.venv/Scripts/python -c "import ccxt; e=ccxt.bitget(); print(all(hasattr(e,n) for n in ['privateMixGetV2MixPositionAllPosition','privateMixGetV2MixOrderOrdersPlanPending','privateMixGetV2MixOrderOrdersPending','privateMixGetV2MixAccountAccounts','publicMixGetV2MixMarketContracts','privateUtaGetV3AccountSettings','privateMixGetV2MixOrderFills']))"`
Expected: `True`

- [ ] **Step 6: Commit**

```bash
git add bitgetbot/bitget_read.py tests/test_bitget_read.py
git commit -m "feat: add read-only Bitget adapter"
```

---

### Task 3: Freqtrade REST 클라이언트

**Files:**
- Create: `bitgetbot/ftclient.py`
- Test: `tests/test_ftclient.py`

**Interfaces:**
- Produces:
  - `class FreqtradeError(Exception)` — 속성 `status_code: int`, `body: str`.
  - `class FreqtradeUnreachable(Exception)`.
  - `class FreqtradeClient(base_url: str, username: str, password: str, transport: httpx.BaseTransport | None = None, timeout: float = 10.0)`, 모든 경로는 `/api/v1/...`, HTTP Basic 인증:
    - `ping() -> bool` (GET `/ping`, 인증 불필요, 연결 실패 시 False)
    - `show_config() -> dict` (GET `/show_config`)
    - `status() -> list[dict]` (GET `/status`)
    - `start() -> dict`, `stop() -> dict`, `pause() -> dict` (POST `/start`, `/stop`, `/pause`)
    - `force_enter(pair: str, side: Literal["long","short"], stake_amount: float, leverage: float, ordertype: str = "market", entry_tag: str | None = None) -> dict` — POST `/forceenter`, JSON `{"pair","side","ordertype","stakeamount","leverage","entry_tag"}`
    - `force_exit(trade_id: int | str, ordertype: str = "market") -> dict` — POST `/forceexit`, JSON `{"tradeid","ordertype"}`
  - 2xx 아니면 `FreqtradeError`, 연결 오류·타임아웃이면 `FreqtradeUnreachable`. 예외 메시지에 비밀번호를 넣지 않는다.

- [ ] **Step 1: 실패하는 테스트 작성** (`tests/test_ftclient.py`, `httpx.MockTransport` 사용)

```python
def test_force_enter_sends_payload_and_basic_auth():
    seen = {}
    def handler(req):
        seen["url"], seen["auth"], seen["json"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"trade_id": 1})
    c = FreqtradeClient("http://127.0.0.1:8080", "u", "p", transport=httpx.MockTransport(handler))
    assert c.force_enter("BTC/USDT:USDT", "long", 7.0, 2, entry_tag="smoke") == {"trade_id": 1}
    assert seen["url"] == "http://127.0.0.1:8080/api/v1/forceenter"
    assert seen["auth"] == "Basic " + base64.b64encode(b"u:p").decode()
    assert seen["json"] == {"pair": "BTC/USDT:USDT", "side": "long", "ordertype": "market", "stakeamount": 7.0, "leverage": 2, "entry_tag": "smoke"}

def test_force_exit_payload():
    # handler가 받은 JSON == {"tradeid": 5, "ordertype": "market"}, 경로 /api/v1/forceexit

def test_non_2xx_raises_ft_error_with_status():
    # handler가 401 반환 → FreqtradeError, status_code == 401, 메시지에 "p"(비밀번호) 없음

def test_unreachable_raises_ft_unreachable():
    # handler가 httpx.ConnectError 발생 → status()가 FreqtradeUnreachable

def test_ping_false_when_unreachable():
    # handler가 httpx.ConnectError → ping() is False
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python -m pytest tests/test_ftclient.py -v`
Expected: FAIL (`ModuleNotFoundError: bitgetbot.ftclient`)

- [ ] **Step 3: `ftclient.py` 구현** (`httpx.Client(base_url=..., auth=(username, password), transport=transport, timeout=timeout)`)

- [ ] **Step 4: 통과 확인**

Run: `.venv/Scripts/python -m pytest tests/test_ftclient.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add bitgetbot/ftclient.py tests/test_ftclient.py
git commit -m "feat: add Freqtrade REST client"
```

---

### Task 4: Freqtrade 설정 + SmokeTest 전략 + 비밀 설정 파일

**Files:**
- Create: `freqtrade/docker-compose.yml`, `freqtrade/user_data/config.json`, `freqtrade/user_data/config.private.example.json`, `freqtrade/user_data/strategies/SmokeTest.py`, `bitgetbot/privcfg.py`
- Test: `tests/test_freqtrade_config.py`, `tests/test_privcfg.py`

**Interfaces:**
- Produces (`bitgetbot/privcfg.py`):
  - `PRIVATE_PATH = Path("freqtrade/user_data/config.private.json")`, `EXAMPLE_PATH = Path("freqtrade/user_data/config.private.example.json")`
  - `init_private(path: Path = PRIVATE_PATH, example: Path = EXAMPLE_PATH) -> bool` — 파일이 없을 때만 예시를 복사하고 `api_server.username`(16자), `api_server.password`(32자), `api_server.jwt_secret_key`(64자)를 `secrets.token_urlsafe`로 채운다. 이미 있으면 건드리지 않고 False. 값은 출력하지 않는다.
  - `load_settings(path: Path = PRIVATE_PATH, base: Path = Path("freqtrade/user_data/config.json")) -> Settings` — `Settings(dry_run: bool, api_url: str, api_user: str, api_pass: str, key: str, secret: str, password: str)`, 두 파일을 Freqtrade처럼 private가 base를 덮어쓰는 방식으로 합친다. `api_url`은 항상 `http://127.0.0.1:8080`. `Settings.__repr__`은 비밀값을 `***`로 가린다.

`config.json` 필수 값(커밋, 비밀값은 빈 문자열):
```json
{
  "max_open_trades": 1, "stake_currency": "USDT", "stake_amount": 7, "tradable_balance_ratio": 0.99,
  "dry_run": true, "dry_run_wallet": 100, "trading_mode": "futures", "margin_mode": "isolated",
  "initial_state": "stopped", "force_entry_enable": true, "timeframe": "1h",
  "entry_pricing": {"price_side": "same", "use_order_book": true, "order_book_top": 1},
  "exit_pricing": {"price_side": "same", "use_order_book": true, "order_book_top": 1},
  "order_types": {"entry": "limit", "exit": "limit", "emergency_exit": "market", "force_entry": "market",
                  "force_exit": "market", "stoploss": "market", "stoploss_on_exchange": true,
                  "stoploss_on_exchange_interval": 60, "stoploss_price_type": "mark"},
  "exchange": {"name": "bitget", "key": "", "secret": "", "password": "",
               "ccxt_config": {"options": {"uta": false}}, "ccxt_async_config": {"options": {"uta": false}},
               "pair_whitelist": ["BTC/USDT:USDT"], "pair_blacklist": []},
  "pairlists": [{"method": "StaticPairList"}],
  "telegram": {"enabled": false, "token": "", "chat_id": ""},
  "api_server": {"enabled": true, "listen_ip_address": "0.0.0.0", "listen_port": 8080, "verbosity": "error",
                 "enable_openapi": false, "jwt_secret_key": "", "CORS_origins": [], "username": "", "password": ""},
  "bot_name": "bitget-ai-bot", "internals": {"process_throttle_secs": 5}
}
```
`config.private.example.json`: `{"dry_run": true, "exchange": {"key": "", "secret": "", "password": ""}, "api_server": {"username": "", "password": "", "jwt_secret_key": ""}}`

`docker-compose.yml`: 서비스 `freqtrade` 하나, 이미지 Global Constraints의 digest, `restart: unless-stopped`, 볼륨 `./user_data:/freqtrade/user_data`, 포트 `"127.0.0.1:8080:8080"`만, 명령 `trade --logfile /freqtrade/user_data/logs/freqtrade.log --db-url sqlite:////freqtrade/user_data/tradesv3.sqlite --config /freqtrade/user_data/config.json --config /freqtrade/user_data/config.private.json --strategy SmokeTest`.

`SmokeTest.py`: `IStrategy` 상속, `INTERFACE_VERSION = 3`, `timeframe = "1h"`, `can_short = False`, `stoploss = -0.06`(2배에서 가격 3%), `minimal_roi = {"0": 100}`, `use_exit_signal = False`, `trailing_stop = False`, `process_only_new_candles = True`. 자동 진입·청산 신호를 절대 내지 않는다(`enter_long`·`exit_long` 열을 0으로). `leverage()`는 2.0 반환.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_freqtrade_config.py` — `config.json`과 `docker-compose.yml`(PyYAML)을 읽어서:
```python
def test_safety_invariants():
    c = load_json("freqtrade/user_data/config.json")
    assert c["trading_mode"] == "futures" and c["margin_mode"] == "isolated"
    assert c["initial_state"] == "stopped"
    assert c["telegram"]["enabled"] is False
    ot = c["order_types"]
    assert ot["stoploss_on_exchange"] is True and ot["stoploss_price_type"] == "mark"
    assert ot["force_exit"] == "market" and ot["emergency_exit"] == "market"
    assert c["exchange"]["ccxt_config"]["options"]["uta"] is False

def test_committed_config_has_no_secrets():
    c = load_json("freqtrade/user_data/config.json")
    assert c["exchange"]["key"] == c["exchange"]["secret"] == c["exchange"]["password"] == ""
    a = c["api_server"]
    assert a["username"] == a["password"] == a["jwt_secret_key"] == ""

def test_compose_publishes_only_localhost_and_pins_digest():
    svc = yaml.safe_load(open("freqtrade/docker-compose.yml", encoding="utf-8"))["services"]["freqtrade"]
    assert svc["ports"] == ["127.0.0.1:8080:8080"]
    assert svc["image"].startswith("freqtradeorg/freqtrade:2026.9@sha256:989aff56")

def test_private_example_defaults_to_dry_run():
    assert load_json("freqtrade/user_data/config.private.example.json")["dry_run"] is True

def test_private_file_is_gitignored():
    out = subprocess.run(["git", "check-ignore", "freqtrade/user_data/config.private.json"], capture_output=True, text=True)
    assert out.returncode == 0
```
`tests/test_privcfg.py` — `tmp_path`에 예시 복사:
```python
def test_init_private_fills_random_api_creds_once(tmp_path): ...
    # 첫 호출 True, username 16자·password 32자·jwt 64자, 두 번째 호출 False이고 파일 내용 그대로
def test_load_settings_merges_private_over_base(tmp_path): ...
    # base dry_run True + private dry_run False → Settings.dry_run is False, key는 private 값
def test_settings_repr_hides_secrets(tmp_path): ...
    # repr(settings)에 key/secret/password/api_pass 값이 없음
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python -m pytest tests/test_freqtrade_config.py tests/test_privcfg.py -v`
Expected: FAIL (파일·모듈 없음)

- [ ] **Step 3: 위 Interfaces대로 파일 작성**

- [ ] **Step 4: 통과 확인**

Run: `.venv/Scripts/python -m pytest tests/test_freqtrade_config.py tests/test_privcfg.py -v`
Expected: 8 passed

- [ ] **Step 5: Freqtrade가 전략을 읽는지 확인** (Docker Desktop 실행 필요)

Run: `.venv/Scripts/python -c "from bitgetbot.privcfg import init_private; init_private()"` 후
`docker compose -f freqtrade/docker-compose.yml run --rm freqtrade list-strategies --config /freqtrade/user_data/config.json --config /freqtrade/user_data/config.private.json`
Expected: 출력 표에 `SmokeTest`가 있고 상태가 `OK`

- [ ] **Step 6: Commit**

```bash
git add freqtrade bitgetbot/privcfg.py tests/test_freqtrade_config.py tests/test_privcfg.py
git commit -m "feat: add Freqtrade smoke config, strategy and private config helper"
```

---

### Task 5: 스모크 테스트 CLI + 장부 + 런북

**Files:**
- Create: `bitgetbot/ledger.py`, `bitgetbot/smoke.py`, `docs/runbooks/smoke-test.md`
- Test: `tests/test_ledger.py`, `tests/test_smoke.py`

**Interfaces:**
- Consumes: Task 1 `evaluate_protection`, `overall`; Task 2 `BitgetReader`, `make_exchange`, `classify_account`; Task 3 `FreqtradeClient`, `FreqtradeUnreachable`, `FreqtradeError`; Task 4 `init_private`, `load_settings`.
- Produces (`bitgetbot/ledger.py`):
  - `LedgerRow` dataclass: `ts_utc: str, symbol: str, side: str, trade_side: str, price: float, size: float, quote_usdt: float, fee_usdt: float, realized_pnl_usdt: float, krw_per_usdt: float, fx_at: str, order_id: str, trade_id: str`
  - `fills_to_rows(fills: list[dict], krw_per_usdt: float, fx_at: str) -> list[LedgerRow]` — 비트겟 v2 fills 필드 `tradeId, orderId, symbol, side, tradeSide, price, baseVolume, quoteVolume, profit, cTime, feeDetail[{feeCoin, totalFee}]`. 수수료는 USDT 항목 `totalFee`의 절댓값(비용은 양수). `ts_utc`는 `cTime`(ms) → ISO8601 `Z`.
  - `append_csv(rows: list[LedgerRow], path: Path) -> int` — 헤더 포함, `trade_id` 중복은 건너뜀, 새로 쓴 행 수 반환.
  - `fetch_krw_per_usdt(get: Callable[[str], dict] | None = None) -> tuple[float, str]` — 업비트 `https://api.upbit.com/v1/ticker?markets=KRW-USDT`의 `trade_price`, 시각 ISO.
- Produces (`bitgetbot/smoke.py`):
  - 순수 함수:
    - `preflight_problems(ft_ok: bool, dry_run: bool, account_kind: str, positions: list[PositionSnapshot], open_orders: int, ft_open_trades: int, usdt_available: float | None) -> list[str]` — 문제 목록(한국어), 비면 통과. 실전일 때만 거래소 항목을 본다: 계정이 `classic`이 아니면, 포지션 ≥1, 미체결 ≥1, 사용 가능 USDT < 10이면 문제. Freqtrade 열린 거래 ≥1은 모드와 무관하게 문제. `ft_ok` False면 "Freqtrade에 연결할 수 없습니다 (Docker Desktop과 `docker compose up -d` 확인)" 하나만.
    - `smoke_stake(spec: ContractSpec, mark_price: float, leverage: float, buffer: float = 1.3) -> float` — `max(spec.min_qty * mark_price, spec.min_notional_usdt) * buffer / leverage`를 소수 둘째 자리 올림. 10 USDT 초과면 `ValueError`.
    - `require_live_confirmation(dry_run: bool, typed: str) -> bool` — dry-run이면 True, 실전이면 `typed.strip() == "LIVE"`일 때만 True.
    - `wait_until(predicate: Callable[[], bool], timeout_s: float, interval_s: float, sleep=time.sleep, clock=time.monotonic) -> bool`
    - `ft_trade_checks(trade: dict, max_liq_fraction: float = 0.7) -> list[Check]` — dry-run용, Freqtrade `/status` 항목의 `open_rate`, `stop_loss_abs`, `liquidation_price`, `is_short`로 `stop_side`·`stop_inside_liq`만 판정.
  - CLI `python -m bitgetbot.smoke <cmd>`, 상태 파일 `.smoke_state.json`(`{"started_ms", "trade_id"}`):
    - `init` — `init_private()`; 이미 있으면 "이미 있음". 사용자가 채울 항목 안내.
    - `preflight` — 모드를 첫 줄에 크게(`=== 모드: 실전(LIVE) ===` / `=== 모드: 모의(DRY-RUN) ===`), 문제 목록 출력, 종료 코드 0/1.
    - `enter` — preflight 다시 통과 필수 → 실전이면 `실전 주문을 냅니다. 계속하려면 LIVE 입력:` → Freqtrade 상태가 stopped면 `start()` → 계약·mark로 `smoke_stake` → `force_enter("BTC/USDT:USDT", "long", stake, 2, "market", "smoke")` → `/status`에 거래가 보일 때까지 60초 대기 → 상태 파일 저장.
    - `check` — 실전: 120초 동안 5초마다 `evaluate_protection` → 표 출력 → PASS면 0. 120초 뒤에도 PASS가 아니면 `손절 확인 실패. 지금 바로 'python -m bitgetbot.smoke exit'을 실행하세요.` 출력 후 1. dry-run: `ft_trade_checks`만 보고 `dry-run: 거래소 손절 확인은 실전에서만 합니다` 출력.
    - `restart-check` — `docker compose -f freqtrade/docker-compose.yml restart freqtrade`를 실행하라고 안내하고 Enter 대기 → ping 90초 대기 → stopped면 `start()` → 30초 뒤 `check`와 같은 판정 + `/status`에 같은 trade_id가 있는지.
    - `exit` — `force_exit(trade_id or "all", "market")` → `/status` 빔 + (실전) 거래소 포지션 None까지 60초 대기 → 이어서 `ledger`.
    - `ledger` — (실전) `privateMixGetV2MixOrderFills({"productType":"USDT-FUTURES","symbol":"BTCUSDT","startTime": started_ms})` → `fills_to_rows` → `ledger/smoke-YYYYMMDD.csv`에 `append_csv`.
  - 어떤 명령도 비밀값을 출력하지 않는다. 거래소에는 조회만 하고, 주문은 전부 Freqtrade REST로.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_ledger.py`:
```python
FILL = {"tradeId":"t1","orderId":"o1","symbol":"BTCUSDT","side":"buy","tradeSide":"open","price":"111240.5",
        "baseVolume":"0.0001","quoteVolume":"11.12405","profit":"0","cTime":"1792396800000",
        "feeDetail":[{"feeCoin":"USDT","totalFee":"-0.00667443"}]}

def test_fill_to_row_values():
    [r] = fills_to_rows([FILL], 1390.1, "2026-10-19T10:00:00Z")
    assert (r.trade_id, r.price, r.size, r.quote_usdt, r.fee_usdt, r.krw_per_usdt) == ("t1", 111240.5, 0.0001, 11.12405, 0.00667443, 1390.1)
    assert r.ts_utc.endswith("Z")

def test_append_csv_skips_duplicate_trade_ids(tmp_path):
    rows = fills_to_rows([FILL], 1390.1, "x")
    p = tmp_path / "l.csv"
    assert append_csv(rows, p) == 1
    assert append_csv(rows, p) == 0

def test_fetch_krw_per_usdt_uses_trade_price():
    rate, at = fetch_krw_per_usdt(get=lambda url: [{"market": "KRW-USDT", "trade_price": 1390.1}])
    assert rate == 1390.1 and at
```
`tests/test_smoke.py`:
```python
POS = PositionSnapshot("BTCUSDT", "long", 0.0001, 111240.5, 112050.0, 56300.0, 2, "isolated")

def test_preflight_ok_live_clean_account():
    assert preflight_problems(True, False, "classic", [], 0, 0, 50.0) == []

def test_preflight_rejects_existing_exposure():
    probs = preflight_problems(True, False, "classic", [POS], 1, 1, 50.0)
    assert len(probs) == 3

def test_preflight_rejects_non_classic_and_low_balance():
    assert len(preflight_problems(True, False, "unknown", [], 0, 0, 5.0)) == 2

def test_preflight_dry_run_ignores_exchange_state():
    assert preflight_problems(True, True, "unknown", [POS], 1, 0, None) == []

def test_preflight_reports_ft_unreachable():
    assert preflight_problems(False, False, "classic", [], 0, 0, 50.0) == ["Freqtrade에 연결할 수 없습니다 (Docker Desktop과 `docker compose up -d` 확인)"]

def test_live_requires_typed_confirmation():
    assert require_live_confirmation(True, "") is True
    assert require_live_confirmation(False, "yes") is False
    assert require_live_confirmation(False, " LIVE ") is True

def test_smoke_stake_meets_minimum_and_cap():
    spec = ContractSpec("BTCUSDT", 0.0001, 0.0001, 5.0)
    assert smoke_stake(spec, 112050.0, 2) == 7.29      # 11.205 * 1.3 / 2 = 7.28325 → 7.29
    with pytest.raises(ValueError):
        smoke_stake(spec, 200000.0, 1)                 # 20 * 1.3 = 26 > 10

def test_wait_until_times_out_with_fake_clock():
    t = [0.0]
    ok = wait_until(lambda: False, 10, 5, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert ok is False and t[0] >= 10

def test_check_fails_when_no_stop_order():
    # check 명령의 판정 함수에 POS와 빈 손절 목록을 넣고 fake clock으로 120초를 흘리면
    # 반환값 1, 출력에 "지금 바로 'python -m bitgetbot.smoke exit'" 포함 (capsys)

def test_ft_trade_checks_dry_run():
    trade = {"open_rate": 111240.5, "stop_loss_abs": 107900.0, "liquidation_price": 56300.0, "is_short": False}
    assert overall(ft_trade_checks(trade)) == "PASS"
```
`test_check_fails_when_no_stop_order`가 가능하도록 `check` 명령 본체를 `run_check(reader, symbol, timeout_s, interval_s, sleep, clock, out) -> int`로 분리한다(가짜 reader 주입).

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python -m pytest tests/test_ledger.py tests/test_smoke.py -v`
Expected: FAIL (모듈 없음)

- [ ] **Step 3: `ledger.py`, `smoke.py` 구현** (명령 디스패치는 `argparse` 서브커맨드)

- [ ] **Step 4: 통과 확인**

Run: `.venv/Scripts/python -m pytest -v`
Expected: 전체 통과(Task 1~5 합계 43 passed)

- [ ] **Step 5: dry-run 종단 확인** (Docker Desktop 실행 필요, 거래소 키 없이)

Run 순서:
1. `docker compose -f freqtrade/docker-compose.yml up -d`
2. `.venv/Scripts/python -m bitgetbot.smoke preflight` → 첫 줄 `=== 모드: 모의(DRY-RUN) ===`, 종료 코드 0
3. `.venv/Scripts/python -m bitgetbot.smoke enter` → 거래 번호 출력
4. `.venv/Scripts/python -m bitgetbot.smoke check` → `dry-run: 거래소 손절 확인은 실전에서만 합니다`, 종료 코드 0
5. `.venv/Scripts/python -m bitgetbot.smoke exit` → `/status` 빔
6. `docker compose -f freqtrade/docker-compose.yml down`
Expected: 각 단계가 위 출력대로, 오류 없음.

- [ ] **Step 6: 런북 작성** (`docs/runbooks/smoke-test.md`, 한국어, 사용자가 그대로 따라 하는 순서)

순서와 내용:
1. 준비물: Docker Desktop 실행, 비트겟 메인 계정, 약 15만원어치 USDT.
2. 비트겟 서브계정 만들기(카피트레이딩 쓰지 않음). 메인 → 서브로 USDT 이체(스모크에는 10 USDT 이상 필요).
3. 집 공인 IP 확인(브라우저에서 IP 확인 사이트 접속).
4. 서브계정 API 키 만들기: 권한은 선물 거래 + 읽기만, 출금·이체는 끔, IP는 3번 주소. 패스프레이즈 기록.
5. `python -m bitgetbot.smoke init` → `freqtrade/user_data/config.private.json`의 `exchange.key/secret/password` 채우기, `dry_run`을 `false`로. 이 파일은 깃에 안 올라감.
6. `docker compose -f freqtrade/docker-compose.yml up -d` → `preflight`(첫 줄이 실전인지 확인, 문제 0개) → `enter`(`LIVE` 입력) → `check`(PASS) → 컨테이너 재시작 → `restart-check`(PASS, 같은 거래 번호) → `exit` → `ledger/` CSV 확인.
7. 실패 시: `check`나 `restart-check`가 실패하면 즉시 `exit`. `exit`도 안 되면 비트겟 웹(PC)에서 서브계정 선물 포지션을 직접 시장가 청산.
8. 정리: `config.private.json`의 `dry_run`을 `true`로, `docker compose ... down`, 비트겟에서 이 임시 API 키 삭제(서버용 키는 계획 6에서 새로 만듦).
9. 결과를 Claude에게 알려 주기: preflight의 계정 모드, check 표, restart-check 결과, Freqtrade 로그의 `set_position_mode` 관련 오류 여부(`freqtrade/user_data/logs/freqtrade.log`에서 검색).

- [ ] **Step 7: Commit**

```bash
git add bitgetbot/ledger.py bitgetbot/smoke.py tests/test_ledger.py tests/test_smoke.py docs/runbooks/smoke-test.md
git commit -m "feat: add smoke-test CLI, ledger export and runbook"
```

---

## 실행 후 사용자 단계 (Claude가 대신 못 함)

실전 스모크 테스트(런북 2~8)는 사용자가 직접 실행한다. Claude는 주문을 내지 않는다. 결과(런북 9)를 받으면 계획 2를 쓴다.
