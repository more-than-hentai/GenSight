# 설치 및 업그레이드 가이드

## 요구 사항

| 항목 | 최소 사양 |
|---|---|
| Python | 3.10 이상 (3.12 권장) |
| OS | Linux / macOS / Windows(WSL 권장) |
| GPU | 선택 사항 — NVIDIA GPU + 드라이버 (추후 ML 분석 기능용) |
| Docker | 선택 사항 — Docker Engine 24+ / Docker Compose v2 |

## venv 설치 (권장)

```bash
git clone <repository-url> GenSight
cd GenSight
./run.sh
```

`run.sh`는 첫 실행 시 `.venv` 생성과 의존성 설치를 자동으로 수행한 뒤
서버를 시작합니다. 브라우저에서 <http://127.0.0.1:8090> 을 엽니다.

수동 설치:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8090
```

### 포트/호스트 변경

```bash
HOST=0.0.0.0 PORT=9000 ./run.sh
```

> ⚠️ `0.0.0.0` 바인딩은 같은 네트워크의 다른 기기에서 접근을 허용합니다.
> GenSight는 인증 기능이 없으므로 신뢰할 수 있는 네트워크에서만 사용하세요.

## Docker 설치

```bash
docker compose up --build -d
```

이미지 디렉토리는 `docker-compose.yml`에 read-only로 마운트합니다:

```yaml
    volumes:
      - ./data:/opt/gensight/data
      - /home/me/ai-images:/images/ai-images:ro
```

이후 웹 UI 설정에서 **컨테이너 내부 경로**(`/images/ai-images`)를 등록하거나,
스캔 탭에서 해당 경로를 직접 입력합니다.

GPU를 사용할 예정이면 `nvidia-container-toolkit` 설치 후
`docker-compose.yml`의 GPU 예약 주석을 해제하세요.

## 업그레이드

### venv

```bash
git pull
.venv/bin/pip install -r requirements.txt   # 의존성 변경 반영
# 서버 재시작
```

### Docker

```bash
git pull
docker compose up --build -d
```

설정(`data/settings.json`)과 썸네일 캐시는 `data/` 볼륨에 있으므로
업그레이드해도 유지됩니다.

## 제거

```bash
# venv 방식: 프로젝트 디렉토리 삭제로 끝
rm -rf GenSight

# Docker 방식
docker compose down --rmi local
```
