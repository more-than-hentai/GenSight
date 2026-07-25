# GenSight

AI 생성 이미지 메타데이터 추출 WebUI — AI-generated image metadata extractor web UI.

지정한 디렉토리의 AI 생성 이미지(Stable Diffusion / ComfyUI / NovelAI 등)를 스캔해
프롬프트·네거티브 프롬프트·샘플러·CFG·시드·모델 등의 생성 설정을 추출하고,
게시판에 붙여넣기 좋은 형식(JSON / Markdown / BBCode)으로 복사할 수 있는 로컬 도구입니다.

## Features

- **메타데이터 추출**: A1111/Forge/SD.Next(`parameters`), ComfyUI(워크플로 그래프 파싱), NovelAI, JPEG/WebP EXIF
- **디렉토리 관리**: 웹 UI 설정 메뉴에서 스캔 대상 디렉토리 추가/삭제
- **대량 이미지 대응**: 작업(스캔)별 워커 수 조정, 동시 작업 수 제한, 백그라운드 작업 큐 + 진행률/취소
- **결과 뷰어**: 썸네일 그리드, 프롬프트/파일명/모델 검색, 도구별 필터, 상세 모달
- **게시판용 복사**: JSON / Markdown / BBCode / 프롬프트만 — 클릭 한 번으로 클립보드 복사
- **내보내기**: 스캔 결과 전체를 JSON / CSV로 다운로드
- **다국어**: 한국어 / English / 日本語 (웹 UI에서 즉시 전환)
- **멀티 GPU 준비**: GPU 감지 및 활성화 설정 — 추후 ML 이미지 분석(카테고리/의상/배경/품질 판별)에 사용

## Quick start (venv)

```bash
./run.sh
```

첫 실행 시 `.venv` 생성과 의존성 설치가 자동으로 이루어집니다.
브라우저에서 <http://127.0.0.1:8090> 을 엽니다.

수동 설치:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8090
```

## Docker

```bash
docker compose up --build
```

이미지 디렉토리는 `docker-compose.yml`의 volumes에 read-only로 마운트한 뒤,
웹 UI 설정에서 컨테이너 내부 경로(`/images/...`)를 등록하세요.

## Usage

1. **설정** 탭에서 이미지 디렉토리를 추가합니다.
2. **스캔** 탭에서 디렉토리를 선택하고 워커 수를 조정해 스캔을 시작합니다.
3. **결과** 탭에서 이미지를 클릭 → 원하는 형식으로 복사합니다.

출력 예시 (JSON):

```json
{
  "prompt": "A high-resolution photorealistic ...",
  "negative_prompt": "",
  "Sampler": "Euler simple",
  "CFG scale": "1.0",
  "Seed": "166465958725488",
  "Size": "832x1216",
  "Model hash": "5394fca4fa",
  "Model": "krea2TurboOfficialComfy_krea2TurboNvfp4"
}
```

## Project layout

```
app/            FastAPI backend
  main.py       API routes + static serving
  metadata.py   A1111 / ComfyUI / NovelAI / EXIF parsers
  scanner.py    Background scan jobs, worker pools, job queue
  config.py     JSON settings persistence (data/settings.json)
  gpu.py        nvidia-smi based GPU detection
web/            Frontend (vanilla JS, no build step)
  i18n/         ko / en / ja translations
tests/          pytest unit tests
data/           Runtime data: settings, thumbnail cache (gitignored)
```

## Settings

모든 설정은 웹 UI **설정** 탭에서 관리되며 `data/settings.json`에 저장됩니다.

| 항목 | 설명 |
|---|---|
| 스캔 디렉토리 | 스캔 대상 경로 목록 (이미지 서빙도 이 경로 하위로 제한) |
| 추출 워커 | 메타데이터 추출 스레드 수 (작업별로 재정의 가능) |
| 동시 작업 수 | 동시에 실행되는 스캔 작업 수 |
| GPU | ML 분석에 사용할 GPU 선택, GPU당 동시 작업 수 |

## Roadmap

- [ ] ML 이미지 분석: 카테고리 / 의상 / 배경 태깅 (멀티 GPU 분산)
- [ ] 품질 판별: 저품질·신체 파손(anatomy broken) 이미지 검출
- [ ] 프롬프트 통계 대시보드 (자주 쓴 토큰 / 모델 / 샘플러)
- [ ] 중복 이미지 탐지 (perceptual hash)
- [ ] 결과 영구 저장 (SQLite) 및 증분 스캔

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

## License

MIT
