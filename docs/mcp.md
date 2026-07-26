# MCP 서버 연동

GenSight는 이미지 라이브러리를 AI 클라이언트(Claude Code, Claude Desktop 등)에
노출하는 MCP(Model Context Protocol) 서버를 내장합니다. 웹 UI와 같은 SQLite
라이브러리(`data/gensight.db`)를 읽으므로, 웹에서 스캔한 결과를 AI 클라이언트에서
즉시 검색할 수 있습니다.

## 등록

```bash
claude mcp add gensight -- /path/to/GenSight/.venv/bin/python -m app.mcp_server
```

Claude Desktop의 경우 `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gensight": {
      "command": "/path/to/GenSight/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/GenSight"
    }
  }
}
```

## 인증

웹 UI에서 인증을 활성화하면 MCP 도구도 함께 잠깁니다. 두 가지 방법으로 해제합니다:

1. **login 도구** — AI 클라이언트에서 `login(username, password)` 호출
   (웹과 동일한 계정, scrypt 해시 대조)
2. **환경변수** — MCP 서버 설정에 `GENSIGHT_MCP_PASSWORD`(필요 시
   `GENSIGHT_MCP_USERNAME`)를 지정하면 자동 인증됩니다:

```json
{
  "mcpServers": {
    "gensight": {
      "command": ".../python", "args": ["-m", "app.mcp_server"],
      "env": { "GENSIGHT_MCP_PASSWORD": "..." }
    }
  }
}
```

인증이 비활성(기본)일 때는 모든 도구가 바로 동작합니다.
MCP는 로컬 stdio로 실행되므로(실행자가 이미 파일시스템 접근 권한을 가짐)
관리자/일반 구분 없이 유효한 계정이면 잠금이 해제됩니다.

## 제공 도구

| 도구 | 설명 |
|---|---|
| `login` | 인증 활성화 시 잠금 해제 (웹과 동일 계정) |
| `extract_prompt` | **스캔되지 않은 파일 포함, 임의 이미지에서 프롬프트·설정 추출** |
| `search_images` | 프롬프트/파일명/모델/태그 텍스트 검색 (+도구/즐겨찾기/평점 필터) |
| `get_image_metadata` | 라이브러리에 등록된 이미지의 전체 메타데이터 조회 |
| `get_prompt_stats` | 전체 라이브러리의 프롬프트 토큰/모델/샘플러 사용 통계 |
| `find_similar_images` | perceptual hash 기반 유사 이미지 검색 |
| `find_duplicates` | 시각적으로 동일한(해시 일치) 중복 이미지 그룹 |
| `library_summary` | 라이브러리 요약 (총 개수, 도구별, 즐겨찾기, 태깅 수) |

### `extract_prompt` vs `get_image_metadata`

| | `extract_prompt` | `get_image_metadata` |
|---|---|---|
| 대상 | 디스크의 **아무 이미지 파일** | 라이브러리에 등록된 이미지 |
| 동작 | 파일을 직접 읽어 파싱 (A1111/ComfyUI/NovelAI/EXIF) | DB 조회 (평점·태그·품질 포함) |
| 파일이 없으면 | 라이브러리에 있으면 저장된 값으로 폴백 | 404 |

스캔하지 않은 이미지의 프롬프트가 궁금할 때는 `extract_prompt`를 쓰세요.

## 사용 예 (Claude Code에서)

> "gensight에서 'blue dress' 프롬프트가 들어간 이미지를 찾아서 가장 많이 쓴 모델을 알려줘"

> "~/Downloads/new.png 의 프롬프트랑 시드 알려줘" → `extract_prompt` (스캔 불필요)

MCP 서버는 stdio로 동작하며 웹 서버와 독립적으로 실행됩니다
(웹 UI가 꺼져 있어도 사용 가능).

## 보안 모델

- **로컬 stdio 전용, 읽기 전용.** 실행한 사용자의 파일시스템 권한을 그대로
  물려받으므로 그 사용자에 대한 보안 경계가 아닙니다 — 원격 전송으로
  브리지하지 마세요.
- 인증이 켜져 있으면 잠금 해제는 **계정의 자격증명 버전에 묶이고 호출마다
  재확인**됩니다. 비밀번호 변경·역할 변경·계정 삭제 시 실행 중인 서버가
  즉시 다시 잠깁니다.
- 도구가 돌려주는 프롬프트·태그는 **외부에서 받은 이미지에서 추출한
  신뢰할 수 없는 데이터**입니다. 응답의 `note` 필드가 이를 명시하며,
  텍스트는 필드당 4,000자로 잘립니다. AI 클라이언트는 이 내용을 지시가
  아닌 데이터로 다뤄야 합니다.
- 모든 `limit` 계열 인자는 상한이 강제됩니다.
