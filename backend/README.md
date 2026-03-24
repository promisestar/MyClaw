# HelloClaw Backend

AI Agent backend powered by HelloAgents + FastAPI.

## Quick Start

```bash
uv sync
uv run uvicorn src.main:app --reload --port 8000
```

## API Endpoints

- `GET /health` - Health check
- `POST /api/chat/send` - SSE chat
- `GET /api/session/list` - List sessions
- `GET /api/config/list` - List configs
- `GET /api/memory/list` - List memories
