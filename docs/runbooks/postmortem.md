# 거래 사후 분석(포스트모템) 기록

설계: `docs/superpowers/specs/2026-10-22-postmortem-archive-design.md`

## 무엇을 하나
- 패널이 켜져 있으면 백그라운드 스레드가 60초마다 실행 중인 거래 봇의 닫힌 거래를 읽는다(`GET show_config`, `GET trades`만, 주문 없음).
- 새로 닫힌 거래마다 `reports/postmortem/<mode>-<trade_id>-<open_ts>.json`과 같은 이름의 `.md`를 쓴다. 사실(거래·시세·일정)을 먼저 쓰고, 그다음 `claude -p --safe-mode`(웹 검색만)로 그 구간의 사건을 조사해 채운다.
- 목록은 `reports/postmortem/index.json`. 패널 맨 아래 오른쪽 "리포트 목록"(`/reports`)에서 읽기 전용으로 본다.
- 스모크 점검 거래와 30일보다 오래된 거래는 건너뛴다.

## 상태 읽기
- `status: pending` 조사 대기(`attempts`, `next_retry_utc`, `errors[].kind` 확인).
- `status: complete` 완료. `verdict.by: "llm"`은 모델의 추정이다.
- `status: incomplete` 5번 실패해서 사실만 남김. 다시 시도하지 않는다. 다시 하려면 그 JSON을 지우면 다음 주기에 처음부터 만든다(30일 안의 거래만).
- 오류 종류: `timeout`(10분 초과), `quota`(구독 사용량 한도, 1시간 뒤), `unavailable`(`claude` 실행 파일 없음), `parse`/`error`, `market`(비트겟 공개 시세 실패), `interrupted`(패널이 조사 중에 꺼짐).

## 직접 실행 (저장소 루트에서)
```
.venv\Scripts\python.exe -m bitgetbot.postmortem once
.venv\Scripts\python.exe -m bitgetbot.postmortem index
```
`once`는 패널 스레드와 같은 잠금(`reports/postmortem/.lock`)을 쓰므로 동시에 돌아도 리포트가 겹치지 않는다.

## 오라클 VM
- `claude`를 설치하고(`~/.local/bin/claude` 또는 `CLAUDE_PATH`), `claude setup-token`으로 만든 `CLAUDE_CODE_OAUTH_TOKEN`을 `bitget-panel.service`의 환경으로 넣어야 조사가 된다. 토큰은 서비스 환경 파일에만 두고 저장소에 넣지 않는다.
- `claude`가 없으면 리포트는 사실만 남고 5번 시도 뒤 `incomplete`가 된다.
- PC에서 VM으로 옮길 때 `reports/`를 함께 복사하면 이어진다(키에 모드와 진입 시각이 들어 있어 겹치지 않음).
