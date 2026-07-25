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

## 라이브러리 (SQLite 영구 저장)

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/library?q&tool&favorite&min_rating&group&sort&offset&limit` | 라이브러리 검색/필터 (sort: recent/oldest/rating/name) |
| GET | `/api/library/item?path=...` | 단일 항목 상세 |
| PATCH | `/api/library/item` | `{"path", "rating"?, "favorite"?, "group_name"?}` |
| GET | `/api/library/similar?path&max_distance&limit` | 유사 이미지 (dHash 해밍 거리) |
| GET | `/api/library/duplicates` | 동일 해시 중복 그룹 |
| GET | `/api/library/summary` | 총계/도구별/즐겨찾기/태깅 수 |
| GET | `/api/stats/prompts?top` | 프롬프트 토큰·모델·샘플러 통계 |

## 폴더 감시 / 그룹 / 태깅

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/watches` | 감시 목록 + 감시 스레드 상태 |
| POST | `/api/watches` | `{"directory", "recursive"?, "poll_interval"?}` |
| PATCH | `/api/watches/{id}` | `{"enabled"?, "poll_interval"?}` |
| DELETE | `/api/watches/{id}` | 감시 삭제 |
| GET/POST | `/api/groups` | 분류 규칙 목록/추가 `{"name","pattern","is_regex"?,"target"?}` |
| DELETE | `/api/groups/{id}` | 규칙 삭제 |
| POST | `/api/groups/apply?overwrite=` | 규칙 일괄 적용 |
| GET | `/api/tagger/status` | WD Tagger 가용성/진행 상태 |
| POST | `/api/tagger/run` | 태깅 시작 `{"limit"?}` (ML 미설치 시 409) |
| POST | `/api/tagger/cancel` | 태깅 취소 |

MCP 서버는 [mcp.md](mcp.md) 참조.

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
