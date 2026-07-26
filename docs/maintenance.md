# 유지보수 가이드

## 데이터 디렉토리 구조

```
data/
├── settings.json   # 웹 UI 설정 (디렉토리, 워커, GPU, 언어)
├── thumbs/         # 썸네일 캐시 (WebP, 자동 재생성 가능)
└── uploads/        # 드래그&드롭으로 업로드된 개별 분석 이미지
```

- **백업**: `settings.json`만 백업하면 됩니다. `thumbs/`는 캐시라 삭제해도
  다음 조회 시 자동으로 다시 생성됩니다.
- **캐시 정리**: 디스크 용량이 부족하면 `rm -rf data/thumbs data/uploads`.
- 환경변수 `GENSIGHT_DATA_DIR`로 데이터 디렉토리 위치를 바꿀 수 있습니다.

## 로그

두 가지가 있습니다 — 목적이 다릅니다.

| | 애플리케이션 로그 | 감사 로그 |
|---|---|---|
| 위치 | `data/gensight-app.log` (10MB × 5 로테이션) | SQLite `audit` 테이블 |
| 내용 | 진행률·속도·워커 수·경고 등 동작 추적 | 상태를 바꾼 동작의 영구 기록 |
| 조회 | `tail -f data/gensight-app.log` | 웹 UI **감사 로그** 탭, `/api/audit` |
| 수명 | 로테이션으로 사라짐 | 10만 건까지 보존 후 오래된 것부터 정리 |

`./run.sh start`의 콘솔 출력(`data/gensight.log`)에는 uvicorn 액세스 로그가
함께 들어갑니다. 레벨 조정:

```bash
GENSIGHT_LOG_LEVEL=DEBUG ./run.sh restart
```

기록되는 진행 로그 예:

```
scan 7726072b0902 queued: /imgs (workers=3); 0 running, 1 queued, max 2
scan 7726072b0902: enumerated 12 image file(s) in 0.0s
scan 7726072b0902: 12/12 (100%) 2 img/s, eta 0s
scan 7726072b0902 done: 12/12 processed, 12 with metadata, 6.8s (2 img/s)
tagger using 1 GPU session(s) on device(s) [0], 1 worker(s) each (1 total)
quality analysis started: 500 image(s), 10 worker(s)
watch sweep /imgs: 3 new/changed file(s) ingested (120 known, ...) in 0.4s
```

스캔 중 개별 파일 오류는 서버를 중단시키지 않고 해당 결과의 `error` 필드에
기록됩니다. 썸네일 생성 실패는 `WARNING` 후 플레이스홀더로 응답합니다.

### 감사 로그

계정·설정·스캔·삭제·정리·태깅 등 상태 변경 동작을 누가 언제 했는지 남깁니다
(`actor`는 인증이 켜져 있을 때 세션 사용자, 아니면 `system`). 감사 기록 실패가
원래 동작을 막지는 않습니다. 관리자 전용이며 CSV로 내보낼 수 있습니다.

### 워커 / 동시성 상태

감사 로그 탭 상단에서 실행 중·대기 중 스캔 수, 활성 추출 워커 수, 감시 스레드
상태를 실시간으로 확인할 수 있습니다 (`GET /api/status/workers`).

## 테스트

```bash
.venv/bin/python -m pytest tests/ -v
```

## 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| 스캔 시작 시 400 `directory not found` | 경로 오타 또는 서버(컨테이너)에서 보이지 않는 경로. Docker라면 볼륨 마운트 확인 |
| 결과에 `error: PermissionError` | 해당 파일 읽기 권한 없음. 파일은 건너뛰고 스캔은 계속됩니다 |
| 썸네일이 회색으로 표시 | 손상된 이미지 파일 — 원본 확인 |
| 이미지 포함 복사가 텍스트만 복사됨 | 클립보드 이미지 쓰기는 HTTPS 또는 localhost에서만 동작. `127.0.0.1`로 접속하세요 |
| 서버 재시작 후 결과가 사라짐 | 스캔 결과는 현재 메모리에만 보관됩니다(로드맵: SQLite 영구 저장). 필요한 결과는 JSON/CSV로 내보내 두세요 |
| GPU가 목록에 없음 | `nvidia-smi` 동작 확인. Docker라면 `nvidia-container-toolkit` + GPU 예약 설정 필요 |

## 보안 주의사항

- GenSight에는 인증이 없습니다. 기본값(127.0.0.1) 외 인터페이스로
  바인딩할 때는 방화벽/리버스 프록시(Basic Auth 등)를 함께 사용하세요.
- 이미지 서빙은 등록된 디렉토리 + 스캔한 디렉토리 + 업로드 폴더
  하위 경로로 제한됩니다 (경로 탈출 방지를 위해 `resolve()` 후 검증).
