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

uvicorn 표준 출력으로 로그가 나갑니다. 스캔 중 개별 파일 오류는
서버를 중단시키지 않고 해당 결과의 `error` 필드에 기록됩니다.
썸네일 생성 실패는 `WARNING` 로그 후 플레이스홀더 이미지로 응답합니다.

```bash
# systemd 등으로 운영 시 로그 레벨 조정
.venv/bin/uvicorn app.main:app --port 8090 --log-level warning
```

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
