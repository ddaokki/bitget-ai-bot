# 비트겟 선물 스모크 테스트

이 절차는 작은 **실전 주문**으로 진입, 거래소 손절, 재시작 후 복구, 청산을 확인합니다. 실전 단계는 사용자가 직접 실행합니다. Claude는 주문하지 않습니다. 손실이나 수수료가 생길 수 있으며, 손절 주문도 체결을 보장하지 않습니다.

## 1. 준비물과 실행 환경

- Docker Desktop이 정상 실행 중이어야 합니다. Docker 시작 오류가 있으면 먼저 사용자가 해결하세요.
- 비트겟 메인 계정과 약 15만원어치 USDT를 준비하세요. 전액을 주문에 쓰는 것은 아닙니다.
- 스모크는 `BTC/USDT:USDT` 롱, 레버리지 2, 격리 마진, 원웨이, 시장가입니다. 계산된 증거금은 **10 USDT 이하**입니다.
- 수수료와 여유 잔액을 위해 서브계정의 선물 사용 가능 잔액은 **10 USDT 이상**이어야 합니다.
- PC에서 비트겟 서브계정 선물 포지션 화면을 미리 열어 두세요. API 장애 때 직접 청산할 수 있어야 합니다.
- 다른 봇이나 수동 거래를 같은 서브계정에서 동시에 사용하지 마세요. 시작 전 거래소 포지션, 미체결 주문, Freqtrade 열린 거래가 모두 없어야 합니다.

PowerShell에서 저장소 루트로 이동하고 가상환경을 활성화하세요. 아래의 `python`은 이 가상환경의 Python입니다.

```powershell
cd C:\Users\jeong\dev\bitget-ai-bot
.\.venv\Scripts\Activate.ps1
python -m bitgetbot.smoke init
```

활성화가 막히면 실행 정책을 바꾸지 말고, 각 `python`을 `.\.venv\Scripts\python.exe`로 바꿔 실행하세요. `init`은 설정 파일을 한 번만 만들며, 이미 있으면 `이미 있음`을 표시합니다.

### 먼저 모의(DRY-RUN)로 연습하기

`freqtrade/user_data/config.private.json`의 `dry_run`이 `true`인지 확인하세요. 거래소 키는 비워 두어도 됩니다. 자동 생성된 `api_server` 인증값은 유지하세요. 인증값이나 파일 전체를 화면, 로그, 채팅에 붙여 넣지 마세요.

```powershell
docker compose -f freqtrade/docker-compose.yml up -d
python -m bitgetbot.smoke preflight
python -m bitgetbot.smoke enter
python -m bitgetbot.smoke check
python -m bitgetbot.smoke exit
docker compose -f freqtrade/docker-compose.yml down
```

- `preflight` 첫 줄은 `=== 모드: 모의(DRY-RUN) ===`이고 문제 0개여야 합니다.
- `enter`에서 거래 번호가 출력되어야 합니다. 모의 모드에서는 `LIVE`를 입력하지 않습니다.
- `check`는 `dry-run: 거래소 손절 확인은 실전에서만 합니다`와 검사 표를 출력하고 성공해야 합니다.
- `exit`는 Freqtrade 열린 거래 0개를 확인합니다. 모의 체결을 실전 장부로 저장하지 않습니다.
- 모의 검사는 Freqtrade의 손절 방향과 청산 거리만 확인합니다. 거래소 손절 존재, 표시가격 발동, 수량 보호는 실전에서 확인해야 합니다.
- 명령이 실패하면 다음 진입 단계로 넘어가지 마세요. 실행 중인 Freqtrade와 로컬 파일의 모드가 다르면 주문이 차단됩니다.

## 2. 비트겟 서브계정 만들기

메인 계정에서 스모크 전용 서브계정을 만드세요. 카피트레이딩은 사용하지 않습니다. USDT를 메인 계정에서 서브계정으로 이체하고, **USDT 선물 사용 가능 잔액 10 USDT 이상**을 확인하세요. 필요한 경우 서브계정 내 선물 계정으로 옮기세요.

기존 포지션과 주문이 없는 전용 계정을 사용하세요. 계정이 classic인지 CLI의 `preflight` 결과로 확인합니다. `uta` 또는 `unknown`이면 실전 진입하지 마세요.

## 3. 집 공인 IP 확인

집 인터넷에 연결한 PC 브라우저로 공인 IP 확인 사이트에 접속하세요. 표시된 공인 IP를 기록하세요. PC의 `192.168.*` 같은 내부 IP가 아닙니다. VPN이나 다른 네트워크로 바꾸거나 공인 IP가 바뀌면 API 접근이 막힐 수 있습니다.

## 4. 서브계정 API 키 만들기

반드시 스모크용 **서브계정**에서 API 키를 만드세요.

- 권한은 **선물 거래 + 읽기만** 켭니다.
- **출금·이체 권한은 끕니다.**
- IP 제한에는 3번에서 확인한 집 공인 IP를 넣습니다.
- API 키, 시크릿, 패스프레이즈를 안전하게 기록합니다. `exchange.password`에는 계정 로그인 비밀번호가 아닌 **API 패스프레이즈**를 넣습니다.

비밀값은 아래의 로컬 private 파일에만 저장하세요. 터미널 명령 인수, 환경변수, 스크린샷, 채팅, Git에 남기지 마세요.

## 5. 실전 설정 채우기

```powershell
python -m bitgetbot.smoke init
```

로컬 편집기로 `freqtrade/user_data/config.private.json`을 열어 다음을 수정하세요.

- `exchange.key`: 서브계정 API 키
- `exchange.secret`: API 시크릿
- `exchange.password`: API 패스프레이즈
- `dry_run`: 문자열이 아닌 JSON 불리언 `false`

자동 생성된 `api_server.username/password/jwt_secret_key`는 유지하세요. 이 파일은 Git에서 제외됩니다. 커밋되는 `config.json`에 비밀값을 넣지 마세요. API는 호스트 `127.0.0.1:8080`에서만 접근하며 외부로 공개하지 않습니다.

설정 파일을 바꿔도 이미 실행 중인 컨테이너의 모드는 즉시 바뀌지 않습니다. 모의 연습 컨테이너를 `down`한 뒤 실전 컨테이너를 올리세요.

## 6. 실전 진입부터 청산까지

### 컨테이너와 사전 점검

```powershell
docker compose -f freqtrade/docker-compose.yml up -d
python -m bitgetbot.smoke preflight
```

첫 줄이 정확히 `=== 모드: 실전(LIVE) ===`인지 확인하세요. **계정 모드 `classic`, 문제 0개**가 필수입니다. 연결 오류는 Docker Desktop과 컨테이너 상태부터 확인하세요. 예상 증거금이 10 USDT를 넘거나 잔액을 읽지 못해도 차단됩니다.

### 진입과 손절 확인

```powershell
python -m bitgetbot.smoke enter
```

CLI가 사전 점검을 다시 합니다. `실전 주문을 냅니다. 계속하려면 LIVE 입력:`이 나오면 모드와 계정을 다시 확인하고, 직접 `LIVE`를 입력하세요. 그 외 입력은 취소입니다. 주문은 Freqtrade REST로만 전송됩니다.

거래 번호를 기록하세요. 확인 대기는 최대 60초입니다. 시간 초과나 연결 오류가 나도 주문이 이미 접수되었을 수 있습니다. **`enter`를 반복하지 말고 `exit` 및 비트겟 포지션 화면으로 상태를 확인하세요.**

```powershell
python -m bitgetbot.smoke check
```

최대 120초 동안 5초 간격으로 조회합니다. 검사 표의 종합 결과가 **PASS**여야 합니다. `UNKNOWN`은 성공이 아닙니다.

- 격리 마진인지 확인합니다.
- 같은 심볼의 모든 손절 후보를 출력합니다. 주문 번호, 계획 유형, 발동 가격, 발동 기준을 확인하세요. 중복 후보도 숨기지 않습니다.
- 손절 발동 기준은 `mark_price`여야 합니다.
- 손절 방향, 수량 보호, `|진입가 − 손절가| ≤ 0.7 × |진입가 − 청산가|`를 확인합니다.
- 표가 PASS여도 예상치 못한 중복 주문이 보이면 성공으로 넘기지 말고 청산 후 원인을 확인하세요.

### 재시작 확인

```powershell
python -m bitgetbot.smoke restart-check
```

CLI가 다음 명령을 안내하고 Enter를 기다립니다. **CLI는 Docker를 직접 실행하지 않습니다.** 별도의 PowerShell 창을 같은 저장소 루트에서 열어 다음을 직접 실행하세요.

```powershell
docker compose -f freqtrade/docker-compose.yml restart freqtrade
```

재시작 명령 실행 후 원래 CLI 창에서 Enter를 누르세요. API 연결을 최대 90초 기다립니다. Freqtrade가 stopped이면 시작하고, 30초 후 손절과 **진입 때 기록한 동일한 거래 번호**를 확인합니다. 결과는 PASS여야 합니다.

### 청산과 장부

```powershell
python -m bitgetbot.smoke exit
```

저장된 거래 번호로 Freqtrade 시장가 청산을 요청합니다. 저장된 번호가 없으면 Freqtrade의 `all`로 청산하므로, 반드시 전용 계정·봇에서만 사용하세요. Freqtrade 열린 거래가 없고 비트겟 `BTCUSDT` 포지션도 없어질 때까지 최대 60초 기다립니다. 이후 장부를 저장합니다.

`ledger/smoke-YYYYMMDD.csv`를 확인하세요. 날짜는 이번 진입 시작 시각의 UTC 날짜입니다. `.smoke_state.json`에는 시작 시각과 거래 번호가 저장됩니다. 청산과 장부 확인이 끝날 때까지 이 파일을 삭제하지 마세요.

- CSV에는 체결 시각(UTC), 심볼, 매수·매도, 진입·청산, 가격, 수량, USDT 거래금액, USDT 수수료, 실현 손익, USDT/KRW 환율과 조회 시각, 주문·체결 번호가 들어갑니다.
- USDT 수수료는 양수 비용으로 저장합니다. 다른 코인 수수료는 USDT로 임의 환산하지 않습니다.
- 환율은 업비트 `KRW-USDT`의 **장부 조회 시점 가격**이며, 체결 시점 환율이나 실제 환전 환율이 아닙니다. 세무 신고를 확정하는 장부가 아닙니다.
- 체결 반영 지연, 환율 조회 실패, CSV 저장 실패가 나면 청산 상태를 먼저 확인한 뒤 다음 명령을 다시 실행하세요. 기존 체결 번호는 중복 저장하지 않습니다.

```powershell
python -m bitgetbot.smoke ledger
```

## 7. 실패하면 즉시 청산

`check` 또는 `restart-check` 실패 시 즉시 실행하세요.

```powershell
python -m bitgetbot.smoke exit
```

`exit`도 실패하거나 포지션이 남으면, **비트겟 웹(PC)에서 스모크 서브계정의 선물 포지션을 직접 시장가 청산하세요.** 계정, 심볼, 수량을 확인하고 청산 완료까지 확인하세요. API와 Docker 문제를 고치는 동안 포지션을 방치하지 마세요.

청산 후 남은 손절·미체결 주문도 비트겟 화면에서 직접 확인하세요. CLI는 거래소 주문을 직접 생성·취소·수정하지 않습니다. 장부 오류와 청산 실패는 별개이므로 거래소 포지션 화면으로 최종 상태를 확인하세요.

## 8. 정리

포지션과 주문이 정리된 뒤 진행하세요.

1. `freqtrade/user_data/config.private.json`의 `dry_run`을 `true`로 바꿉니다.
2. 다음 명령으로 컨테이너를 내립니다. 파일 변경만으로 실행 중인 봇이 모의 모드로 바뀌지 않습니다.

```powershell
docker compose -f freqtrade/docker-compose.yml down
```

3. 비트겟에서 이번 **임시 API 키를 삭제**합니다. 서버용 키는 계획 6에서 새로 만듭니다.
4. CSV를 보관합니다. private 파일, 거래 DB, 로그, 장부를 공개 저장소에 올리지 마세요.

## 9. 결과를 Claude에게 알려 주기

비밀값과 원본 private 설정을 보내지 말고 다음만 알려 주세요.

- `preflight`의 모드와 계정 모드
- `check` 검사 표와 손절 후보 목록
- `restart-check` 결과 및 같은 거래 번호 유지 여부
- `exit` 청산 확인과 CSV 저장 여부
- `freqtrade/user_data/logs/freqtrade.log`에서 `set_position_mode` 관련 오류 유무

로그를 공유하기 전 API 키, 시크릿, 패스프레이즈, API 인증값과 개인정보를 제거하세요. 필요하면 다음 명령으로 로컬에서 해당 오류만 검색하고, 비밀값을 지운 요약만 전달하세요.

```powershell
Select-String -Path freqtrade/user_data/logs/freqtrade.log -Pattern set_position_mode
```
