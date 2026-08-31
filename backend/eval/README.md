# MyClaw Agent Eval

分层评测包。设计说明见仓库文档：

→ [docs/MyClaw_Agent能力评测框架.md](../../docs/MyClaw_Agent能力评测框架.md)

## 快速开始

### L0（无 LLM，CI）

```bash
cd backend
uv sync --group dev
uv run pytest tests/eval -q
```

### L2（需已启动后端 + LLM）

1. 复制夹具并在前端/API 授权工作区：

```bash
# 示例：复制到临时目录后授权该路径
xcopy /E /I eval\fixtures\mini_repo %TEMP%\myclaw_eval_ws
```

2. 启动后端，再跑场景：

```bash
uv run python -m eval.runner --suite core --workspace "%TEMP%\myclaw_eval_ws"
uv run python -m eval.runner --ids ask_readonly_gate,plan_generate,bash_sandbox
```

报告默认写到 `backend/eval_reports/`。

## 目录

| 路径 | 说明 |
|------|------|
| `harness/` | SSE 客户端、打分器、类型 |
| `suites/scenarios/` | L2 YAML 场景 |
| `fixtures/mini_repo/` | 最小评测工作区 |
| `runner.py` | L2 CLI |
| `../tests/eval/` | L0 pytest |
