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
| GET | `/api/library?...&directory=...&content_rating=...&sort=key1,key2,key3` | 라이브러리 검색 — `sort`는 다중 정렬 체인 (recent/oldest/mtime_desc/mtime_asc/rating/rating_asc/quality/quality_desc/name/name_desc/size_desc/size_asc), `content_rating`은 PG/PG-13/R/X/unrated |
| GET | `/api/library/export?format=json\|csv&필터...` | 필터 적용 라이브러리 내보내기 |
| POST | `/api/library/cleanup` | 누락 파일 행을 아카이브로 옮기고 고아 썸네일 캐시 정리 (읽을 수 없는 파일의 행은 보존) |
| GET | `/api/image?path=...&thumb=true\|false` | 이미지/썸네일 서빙 (허용 경로 하위만) |

> 구 버전의 `/api/jobs/{id}/results`, `/api/jobs/{id}/export`는
> 라이브러리로 통합되어 제거되었습니다. 작업(`/api/jobs`)은 진행률
> 추적과 취소 용도로만 유지됩니다.

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

## 품질 분석 / 휴지통 / 정리 / 인증

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/quality/status` | 품질 분석 대기 수/진행 상태 |
| POST | `/api/quality/run` | 휴리스틱 품질 분석 시작 `{"limit"?}` |
| POST | `/api/quality/cancel` | 분석 취소 |
| POST | `/api/trash` | 파일을 휴지통으로 이동 `{"path"}` |
| GET | `/api/trash` | 휴지통 목록 |
| POST | `/api/trash/{id}/restore` | 복구 (평점/즐겨찾기/태그 유지) |
| DELETE | `/api/trash/{id}` | 개별 영구 삭제 |
| DELETE | `/api/trash` | 휴지통 비우기 (영구 삭제) |
| POST | `/api/organize` | 템플릿 기반 파일 이동 `{"target_root","template","dry_run",필터...}` |
| GET | `/api/auth/status` | 인증 활성/로그인 여부/역할 |
| POST | `/api/auth/login` `logout` `setup` `disable` | 세션 관리 (scrypt 해시, disable은 관리자 세션+비밀번호 필요) |
| GET/POST | `/api/auth/users` | 사용자 목록/추가 `{"username","password","role":"admin"\|"user"}` (관리자 전용) |
| DELETE | `/api/auth/users/{username}` | 사용자 삭제 (본인·마지막 관리자 삭제 불가) |

## 기록 정리 / 아카이브 (관리자 전용)

파괴적 유지보수는 별도 프리픽스 `/api/admin/...`에 있습니다 — 일반 사용자 허용
목록에 없으므로 **기본 거부**이고, 라우터에 관리자 검사 의존성이 추가로 걸려
있습니다. 어느 것도 파일을 삭제하지 않습니다.

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/admin/library/purge/preview` | `{"root","recursive"?,"mode":"all"\|"missing"}` → 분류 카운트·소실될 작업량·겹치는 등록/감시/스캔 경고 + 단발성 `token` |
| POST | `/api/admin/library/purge` | `{"token"}` → 아카이브로 이동. 미리보기 이후 다른 쓰기가 있었으면 **409** |
| GET | `/api/admin/library/archive` | 보관 건수·최고 오래된 항목·만료 건수·배치 목록 |
| POST | `/api/admin/library/archive/restore` | `{"batch_id"}` → 배치 복구 (태그·품질·그룹·평점 전부) |
| POST | `/api/admin/library/archive/prune` | `{"all"?}` → 만료분(기본) 또는 전체 영구 삭제 |

거부 조건:

- 토큰 만료(5분)·재사용·미리보기 이후 `data_version` 변경 → 409
- `mode=missing`인데 **읽기 실패** 행이 있음 → 400 (마운트 해제·권한 오류를
  파일 없음으로 오판하는 것을 막음)
- 대상 경로에 실행 중 스캔 또는 활성 감시 → 409

보존 기간은 설정 `archive.retention_days`(기본 30, `0` = 수동 정리만)이며 감시
루프가 시간당 1회 만료분을 정리합니다. 정리한 경로를 재스캔하면 아카이브 행이
자동 복원되어 태거를 다시 돌리지 않습니다.

### 역할 (인증 활성화 시)

- **admin**: 전체 접근.
- **user** (제한 계정, 외부 노출용): 허용 — `/api/library*`(cleanup 제외),
  `/api/stats`, `/api/analyze`, `/api/image`, `/api/i18n`.
  그 외 `/api/*`는 전부 **403** — 설정/스캔/작업/GPU/감시/그룹/휴지통/정리/
  품질·태깅 실행/사용자 관리/`/api/admin/*` 등 경로 입력·시스템 상태 변경
  엔드포인트.

허용·거부 판정은 문자열 접두어가 아닌 **경로 세그먼트 경계**로 매칭합니다 —
`/api/library-admin/...`이 `/api/library` 허용 규칙을 통과하거나
`/api/library/cleanup-all`이 거부 규칙을 우회하는 일이 없습니다.

`/api/image` 추가 제약:

- 지원 이미지 확장자가 아닌 파일은 **역할과 무관하게 403** — 스캔 루트에
  함께 있는 `.env`, 키 파일, 덤프 등이 노출되지 않습니다.
- 비관리자는 **라이브러리 DB에 등록된 이미지만** 조회 가능 — 스캔되지 않은
  파일은 같은 디렉토리에 있어도 403.

계정을 추가/교체(비밀번호 변경·역할 변경)하면 해당 사용자의 기존 세션이
모두 무효화됩니다. `/api/analyze`는 확장자뿐 아니라 **실제 디코딩 검증**을
통과해야 저장되며, 아이덴티티당 10분에 60건으로 제한됩니다(초과 시 429).

라이브러리 검색에 `quality=` 필터(issues/low/unrated), `sort=quality` 추가.
인증 활성화 시 `/api/auth/*` 외 모든 `/api/*`는 세션 쿠키 필요(401).

## 감사 로그 / 상태 / 그룹 프리셋

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/audit?action&actor&q&since&offset&limit` | 감사 로그 조회 (`action`은 접두어 매칭) |
| GET | `/api/audit/export` | 감사 로그 CSV |
| GET | `/api/status/workers` | 스캔 슬롯·활성 워커·감시 스레드 상태 |
| GET | `/api/groups/presets` | 프리셋 규칙 미리보기 |
| POST | `/api/groups/install-preset?preset=standard\|example` | 프리셋 설치 (동일 이름은 교체) |

감사 대상 동작: `app.start`, `auth.*`(login/logout/enable/disable/user_upsert/
user_delete), `settings.*`, `scan.start|cancel|finish`, `analyze.upload`,
`watch.*`, `group.*`, `tagger.run|cancel|finish`, `quality.run|cancel|finish`,
`trash.*`, `organize.apply`, `library.cleanup`, `library.purge_preview`,
`library.purge`, `library.archive_restore`, `library.archive_prune`.

MCP 서버는 [mcp.md](mcp.md), 구조/프레임워크 검토는 [architecture.md](architecture.md) 참조.

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
