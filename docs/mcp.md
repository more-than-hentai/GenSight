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

## 제공 도구

| 도구 | 설명 |
|---|---|
| `search_images` | 프롬프트/파일명/모델/태그 텍스트 검색 (+도구/즐겨찾기/평점 필터) |
| `get_image_metadata` | 절대 경로로 단일 이미지의 전체 생성 메타데이터 조회 |
| `get_prompt_stats` | 전체 라이브러리의 프롬프트 토큰/모델/샘플러 사용 통계 |
| `find_similar_images` | perceptual hash 기반 유사 이미지 검색 |
| `find_duplicates` | 시각적으로 동일한(해시 일치) 중복 이미지 그룹 |
| `library_summary` | 라이브러리 요약 (총 개수, 도구별, 즐겨찾기, 태깅 수) |

## 사용 예 (Claude Code에서)

> "gensight에서 'blue dress' 프롬프트가 들어간 이미지를 찾아서 가장 많이 쓴 모델을 알려줘"

MCP 서버는 stdio로 동작하며 웹 서버와 독립적으로 실행됩니다
(웹 UI가 꺼져 있어도 사용 가능).
