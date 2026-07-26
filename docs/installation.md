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
./run.sh start
```

`run.sh`는 첫 실행 시 `.venv` 생성과 의존성 설치를 자동으로 수행합니다.
브라우저에서 <http://127.0.0.1:8090> 을 엽니다.

| 명령 | 동작 |
|---|---|
| `./run.sh` | 포그라운드 실행 (개발용, Ctrl+C 종료) |
| `./run.sh start` | 백그라운드 시작 — PID `data/gensight.pid`, 로그 `data/gensight.log` |
| `./run.sh stop` | 정상 종료 (10초 후 강제 종료 폴백) |
| `./run.sh restart` | 재시작 |
| `./run.sh reload` | graceful 재시작 (uvicorn은 핫 리로드 시그널 미지원) |
| `./run.sh status` | 상태 + 헬스체크 (종료 코드: 실행 중 0, 정지 3) |

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

## WD Tagger (자동 태깅 + 콘텐츠 등급) — 선택 설치

태그와 PG/PG-13/R/X 등급은 WD Tagger가 채웁니다. 기본 설치에는 포함되지
않으므로 필요할 때만 추가하세요.

```bash
.venv/bin/pip install -r requirements-ml.txt
```

CUDA 런타임은 pip으로 함께 설치되므로 **시스템 CUDA 툴킷은 필요 없습니다**.
`./run.sh`가 해당 라이브러리를 `LD_LIBRARY_PATH`에 넣어주므로,
GPU를 쓰려면 서버를 `./run.sh start`로 기동하세요. 설정 → WD Tagger의
상태 표시에 `GPU` 또는 `CPU`가 나옵니다.

> `onnxruntime-gpu`는 CUDA 12 계열(<1.23)로 고정돼 있습니다. 1.23 이상은
> `libcudart.so.13`을 요구해서, 드라이버가 CUDA 13을 보고해도 CUDA 13
> 런타임이 없으면 import 자체가 실패합니다.
>
> CPU 전용 호스트라면 `requirements-ml.txt`의 `onnxruntime-gpu`와 세 개의
> `nvidia-*` 줄을 `onnxruntime>=1.19` 하나로 바꾸면 됩니다.

## Docker 설치

```bash
docker compose up --build -d
```

### sudo 없이 실행 (일반 사용자)

Docker 소켓은 기본적으로 `root:docker` 소유라 일반 사용자는 `sudo`가 필요합니다.
사용자를 `docker` 그룹에 넣으면 sudo 없이 쓸 수 있습니다:

```bash
sudo usermod -aG docker "$USER"
```

**그룹 변경은 새 로그인 세션부터 적용됩니다.** 현재 셸에서 바로 확인하려면:

```bash
newgrp docker        # 또는 로그아웃 후 재로그인
docker ps            # sudo 없이 동작하면 완료
```

> ⚠️ **`docker` 그룹은 사실상 root 권한과 동등합니다.** 그룹 구성원은
> 호스트 파일시스템을 컨테이너에 마운트해 root로 접근할 수 있습니다.
> 신뢰하는 계정에만 부여하세요. 이를 피하려면 [Docker rootless 모드](
> https://docs.docker.com/engine/security/rootless/)를 사용하는 방법도
> 있습니다.

이미지 디렉토리는 `docker-compose.yml`에 read-only로 마운트합니다:

```yaml
    volumes:
      - ./data:/opt/gensight/data
      - /home/me/ai-images:/images/ai-images:ro
```

이후 웹 UI 설정에서 **컨테이너 내부 경로**(`/images/ai-images`)를 등록하거나,
스캔 탭에서 해당 경로를 직접 입력합니다.

### CUDA 13 호스트

CUDA 13에서는 pip 런타임 경로를 쓸 수 없습니다 — PyPI의
`nvidia-cuda-runtime-cu13` / `nvidia-cublas-cu13`은 0.0.1 스텁입니다.
NVIDIA 공식 베이스 이미지를 쓰는 전용 프로파일을 사용하세요:

```bash
docker compose --profile cuda13 up -d --build
```

`Dockerfile.cuda13`이 `nvidia/cuda:13.0.1-cudnn-runtime-ubuntu24.04` 위에
`onnxruntime-gpu>=1.23`(CUDA 13 빌드)을 설치합니다. 이미지 약 3.55GB.

포트 충돌 시 `GENSIGHT_PORT=8095 docker compose ...`로 바꿀 수 있습니다.

> **GPU를 실제로 쓰려면 호스트에 `nvidia-container-toolkit`이 필요합니다.**
> 패키지 설치만으로는 부족하고 **데몬에 런타임을 등록**해야 합니다 —
> 등록 전에는 `could not select device driver "nvidia"`로 컨테이너가 뜨지 않습니다.
> ```bash
> sudo apt-get install -y nvidia-container-toolkit
> sudo nvidia-ctk runtime configure --runtime=docker   # /etc/docker/daemon.json 생성
> sudo systemctl restart docker                        # 실행 중이던 컨테이너는 재시작됩니다
> ```
> 확인: `docker info | grep Runtimes`에 `nvidia`가 보이면 정상입니다.
>
> 등록했는데도 `AMD CDI spec not found` 같은 오류가 나면 Docker의 디바이스
> 드라이버 자동 감지가 오작동하는 경우입니다. compose의 `deploy.resources`
> 블록을 CDI 형식으로 바꾸세요:
> ```yaml
>     devices: ["nvidia.com/gpu=all"]
> ```
>
> 설정 → WD Tagger 상태에 `GPU`로 표시되면 정상입니다(장치가 안 보이면 `CPU`).

호스트 서버와 동시에 띄워 시험하려면 포트·데이터 경로를 분리하세요:

```bash
GENSIGHT_PORT=8095 GENSIGHT_DATA=/tmp/gs-test docker compose --profile cuda13 up -d
```

### Docker에서 WD Tagger 사용

기본 이미지에는 ML 의존성이 **포함되지 않습니다**(CUDA 런타임 때문에 약 2GB
증가). 필요하면 빌드 인자로 켜세요:

```bash
docker compose build --build-arg WITH_ML=true
```

또는 `docker-compose.yml`의 `WITH_ML: "true"`로 바꿉니다. GPU를 쓰려면
`nvidia-container-toolkit` 설치 후 같은 파일의 GPU 예약 주석도 해제하세요.
태거 모델은 `HF_HOME`이 데이터 볼륨을 가리키므로 컨테이너를 다시 만들어도
재다운로드하지 않습니다.

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
