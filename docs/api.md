# API 레퍼런스

Base URL: `http://127.0.0.1:8090`
자동 문서: `/docs` (Swagger UI), `/redoc`

## 설정

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/settings` | 현재 설정 조회 |
| PUT | `/api/settings` | 설정 부분 수정 (JSON patch 형태) |
| POST | `/api/settings/directories` | 디렉토리 등록 `{"path": "..."}` |
| DELETE | `/api/settings/directories?path=...` | 디렉토리 등록 해제 |
| GET | `/api/gpus` | GPU 목록 + CPU 코어 수 |
| GET | `/api/i18n/{lang}` | UI 번역 리소스 |

## 스캔 / 분석

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/scan` | 스캔 시작 `{"directory", "recursive"?, "workers"?}` — 미등록 경로 허용 |
| POST | `/api/analyze` | 단일 이미지 업로드 분석 (multipart `file`, 최대 100 MB) |
| GET | `/api/jobs` | 작업 목록 |
| GET | `/api/jobs/{id}` | 작업 상태 |
| POST | `/api/jobs/{id}/cancel` | 작업 취소 |
| DELETE | `/api/jobs/{id}` | 작업 삭제 |

## 결과

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/jobs/{id}/results?offset&limit&q&tool` | 페이지네이션 + 검색/필터 |
| GET | `/api/jobs/{id}/result?file=...` | 단일 결과 상세 (raw 포함) |
| GET | `/api/jobs/{id}/export?format=json\|csv` | 전체 결과 다운로드 |
| GET | `/api/image?path=...&thumb=true\|false` | 이미지/썸네일 서빙 (허용 경로 하위만) |

## 결과 객체

```json
{
  "file": "/abs/path/img.png",
  "filename": "img.png",
  "tool": "a1111 | comfyui | novelai | unknown",
  "prompt": "...",
  "negative_prompt": "...",
  "params": {
    "Sampler": "Euler simple",
    "Steps": "8",
    "CFG scale": "1.0",
    "Seed": "166465958725488",
    "Size": "832x1216",
    "Model hash": "5394fca4fa",
    "Model": "krea2TurboOfficialComfy_krea2TurboNvfp4"
  },
  "error": null
}
```

오류 응답은 항상 `{"detail": "메시지"}` JSON 형태입니다
(처리되지 않은 예외도 전역 핸들러가 동일 형식으로 변환).
