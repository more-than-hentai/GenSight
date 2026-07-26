# 아키텍처 및 프레임워크 검토

## 현재 구조

```
app/
├── main.py          앱 조립: 미들웨어(인증), 예외 핸들러, 라우터 등록, 정적 서빙
├── routers/         API 계층 (FastAPI APIRouter 단위)
│   ├── system.py    설정 / 디렉토리 / GPU / i18n
│   ├── scan.py      스캔 작업 / 단일 분석 / 내보내기
│   ├── library.py   라이브러리 / 통계 / 감시 / 그룹 / 태거 / 품질
│   ├── media.py     이미지·썸네일 서빙 (경로 검증)
│   ├── trash.py     휴지통 / 파일 정리
│   └── auth.py      인증
├── db.py            SQLite 영속 계층 (WAL, 마이그레이션)
├── scanner.py       스캔 작업 큐 + 워커 풀
├── watcher.py       폴더 감시 (watchdog + 폴링)
├── metadata.py      A1111/ComfyUI/NovelAI/EXIF 파서
├── imghash.py       dHash perceptual hash
├── quality.py       품질 휴리스틱 + 일괄 분석 작업
├── stats.py         프롬프트/파라미터 통계
├── tagger.py        WD Tagger (선택 ML)
├── files.py         파일 조작: 휴지통, 정리, 경로 검증 헬퍼
├── auth.py          scrypt 해시 + 세션 스토어
├── config.py        settings.json 영속
└── mcp_server.py    MCP stdio 서버
web/                 프론트엔드 (바닐라 JS, 빌드 단계 없음)
```

## 프레임워크 검토 결론

### 백엔드 — FastAPI 유지 (추가 프레임워크 불필요)

이미 FastAPI가 웹 프레임워크입니다. 추가 검토했던 항목과 결론:

| 후보 | 결론 |
|---|---|
| SQLAlchemy (ORM) | **미도입** — 테이블 4개, 쿼리가 단순해 `sqlite3` + 얇은 DAO(db.py)로 충분. 스키마 변경은 `_MIGRATIONS`의 ALTER 목록으로 관리 |
| Celery/RQ (작업 큐) | **미도입** — 단일 프로세스 로컬 도구라 스레드 기반 큐로 충분. 분산이 필요해지면 재검토 |
| 구조 개선 | **적용** — 단일 main.py를 `routers/` 모듈로 분리 (이번 릴리스) |

### 프론트엔드 — 단계적 마이그레이션 경로

현재 바닐라 JS(단일 `app.js`, 빌드 없음)는 "클론 후 바로 실행"이라는 장점이
있지만 코드가 ~1,500줄에 도달해 한계에 근접했습니다.

**권장 로드맵:**

1. **Phase A (다음 릴리스): ESM 모듈 분리** — 빌드 단계 없이 `web/js/`
   아래 api/i18n/library/scan/settings/modal 모듈로 분할. 위험도 낮음.
2. **Phase B (UI 복잡도 증가 시): Vue 3 + Vite + Pinia** — 조건부 도입.
   트리거: 가상 스크롤 갤러리, 복잡한 다중 선택/드래그, 실시간
   웹소켓 업데이트 같은 요구가 생길 때. `web/`은 Vite 프로젝트가 되고
   FastAPI는 빌드 산출물(`web/dist`)을 서빙. Dockerfile에 Node 빌드
   스테이지 추가.

React가 아닌 Vue를 권장하는 이유: 템플릿 문법이 현재 HTML 구조를 거의
그대로 옮길 수 있어 마이그레이션 비용이 가장 낮고, 단일 파일 컴포넌트가
현재 탭 구조와 1:1 대응됩니다.

## 스레딩 모델

- FastAPI sync 핸들러 → anyio 워커 스레드
- 스캔: JobManager 디스패처 1 + 작업별 ThreadPoolExecutor
- 감시: watcher 스레드 1 (+watchdog observer 스레드)
- 품질/태깅: 실행 시 전용 스레드 (+GPU별 워커)
- SQLite: 스레드별 연결, WAL 모드, busy_timeout 30s,
  초기화(스키마/WAL 전환)는 전역 락으로 직렬화

## 계정 저장소

로그인 계정(사용자명/salt/scrypt 해시/역할)은 **SQLite `users` 테이블**에
저장됩니다 (`data/gensight.db`) — `settings.json`이 아닙니다. 인증
on/off 토글만 `settings.json`의 `auth.enabled`에 남아 있습니다.

이전에는 계정이 `settings.json`에 있었는데, JSON 파일의 읽기-수정-쓰기
방식은 동시 요청 간 갱신 유실 위험이 있고, salt/해시가 지원 요청용으로
공유될 수 있는 설정 파일에 노출되는 문제가 있었습니다. 계정 쓰기는 이제
나머지 앱과 동일하게 SQLite 트랜잭션으로 처리되며, 특히:

- **버전 증분**은 `INSERT ... ON CONFLICT DO UPDATE SET version = users.version + 1`
  한 문장으로 SQLite가 직접 계산합니다 — Python에서 미리 읽어 계산한 값을
  다시 쓰는 방식은 동시 쓰기 시 두 요청이 같은 "다음 버전"을 계산해
  세션 무효화가 누락될 수 있었습니다.
- **마지막 관리자 보호**는 `DELETE`/`UPSERT` 문 자체의 `WHERE` 절에 내장돼
  있어, 두 관리자가 동시에 서로를 삭제/강등해도 SQLite의 쓰기 직렬화
  덕분에 한쪽만 성공합니다 (`tests/test_users_db.py`의 스레드 기반
  테스트로 검증).
- 구버전 `settings.json` 계정은 최초 접근 시 **전량 원자적 트랜잭션**으로
  DB에 이관되고(`db.import_legacy_users`), 성공 후에만 JSON 필드를
  비웁니다 — 중간에 실패해도 일부만 이관되는 일이 없습니다.

## UI 헤더 반응형 처리

헤더는 `flex-wrap`을 써서 탭 내비게이션(`nav`, 넘치면 자체 스크롤)과
계정 컨트롤(`.header-right`: 사용자 칩·로그아웃·테마·언어)이 좁은
화면에서 각각 별도 줄로 접히도록 구성돼 있습니다. 이전에는 두 그룹이
줄바꿈 없이 한 줄에 강제로 들어가 있어, 375px 폭 같은 좁은 화면에서
로그아웃 버튼 등이 뷰포트 밖으로 밀려나 스크롤 없이는 닿을 수 없었습니다.

## 콘텐츠 등급 (후방주의)

WD Tagger의 등급 헤드(카테고리 9: general/sensitive/questionable/explicit)를
civitai식 PG/PG-13/R/X로 매핑해 `content_rating` 컬럼에 저장합니다
(`tagger.extract_predictions`). 태깅 실행 시 태그와 함께 자동으로 채워지며,
라이브러리에서 등급 필터·R/X 블러(클릭 시 표시)가 적용됩니다.
대형 LLM(예: Qwen 35B)은 이 용도에 불필요합니다 — 분류 전용 태거가
훨씬 빠르고 정확합니다.

## VL 캡셔닝 / 추천 프롬프트 (로드맵)

danbooru 태그 형식의 "역프롬프트"는 이미 WD Tagger 태그로 DB화되어
검색 대상에 포함되고, 상세 모달의 "태그를 프롬프트로 복사"로 바로 쓸 수
있습니다. krea2 스타일의 자연어 캡션이 필요해지면 VL 캡셔닝 모델
(JoyCaption, Florence-2, Qwen-VL 등)을 tagger.py와 같은 job 패턴으로
추가하고 `caption` 컬럼에 저장하는 것을 권장합니다.

## 품질 판별의 한계와 확장 지점

현재는 PIL 휴리스틱(블러/노출/대비/해상도) + 생성 설정 검사입니다.
**신체 파손(anatomy) 검출은 ML 모델이 필요**하며, `quality.py`의
job/queue 구조는 tagger.py와 동일한 형태이므로 ONNX 기반 detector
(예: hand/anatomy anomaly 모델)를 같은 패턴으로 끼워 넣으면 됩니다.
GPU 분배 로직은 tagger.py의 세션-퍼-GPU 구현을 재사용하세요.
