# MyClaw 统一评测包

本目录合并了 **Agent 能力评测** 与 **Memory/RAG 离线检索评测**，统一入口为 `python -m evals`。

设计说明：

- Agent 分层评测 → [docs/MyClaw_Agent能力评测框架.md](../../docs/MyClaw_Agent能力评测框架.md)
- Memory/RAG 召回 → [docs/MyClaw实现详解/MyClaw_Memory实现文档.md](../../docs/MyClaw实现详解/MyClaw_Memory实现文档.md)、[MyClaw_RAG实现文档.md](../../docs/MyClaw实现详解/MyClaw_RAG实现文档.md)

---

## 目录结构

```
backend/evals/
  cli.py / __main__.py     # 统一 CLI：python -m evals --channel ...
  run_agent.py             # Agent L2 场景
  run_memory.py / run_rag.py / run_rag_expanded.py
  metrics.py / schema.py / io_utils.py / paths.py
  agent/
    harness/               # SSE 客户端、打分器、类型
    suites/scenarios/      # L2 YAML 场景
    fixtures/mini_repo/    # 最小评测工作区
  datasets/
    memory/                # corpus.jsonl + queries.jsonl
    rag/                   # corpus/*.md + queries.jsonl
  reports/                 # 运行产物（默认 gitignore）
```

L0 确定性单测位于 `backend/tests/eval/`（Agent 契约）与 `backend/tests/evals/`（检索指标）。

---

## 快速开始

### L0 Agent 契约（无 LLM，CI）

```bash
cd backend
uv sync --group dev
uv run pytest tests/eval -q
```

### L2 Agent 场景（需已启动后端 + LLM）

1. 复制夹具并在前端/API 授权工作区：

```bash
# 示例：复制到临时目录后授权该路径
xcopy /E /I evals\agent\fixtures\mini_repo %TEMP%\myclaw_eval_ws
```

2. 启动后端，再跑场景：

```bash
uv run python -m evals --channel agent --suite core --workspace "%TEMP%\myclaw_eval_ws"
uv run python -m evals --channel agent --ids ask_readonly_gate,plan_generate,bash_sandbox
```

报告默认写到 `evals/reports/agent_<timestamp>.json`。

### Memory / RAG 离线检索（需 Qdrant + Embedding）

```bash
cd backend

# 指标单测（不连 Qdrant）
python -m pytest tests/evals/test_metrics.py -q

# 首次或语料变更后：强制重建评测库并跑全部检索通道
python -m evals --channel retrieval --reseed

# 单通道
python -m evals --channel memory --reseed
python -m evals --channel rag --reseed
python -m evals --channel rag_expanded --reseed

# 调整 K 与阈值
python -m evals --channel memory --ks 1,3,5 --top-k 10 --score-threshold 0.3
python -m evals --channel rag --ks 1,3,5,10 --score-threshold 0.3
```

---

## CLI 参数一览

| 参数 | 适用通道 | 说明 |
|------|----------|------|
| `--channel` | 全部 | `agent` / `memory` / `rag` / `rag_expanded` / `retrieval`（或 `all`，等同 retrieval） |
| `--base-url` | agent | 后端地址，默认 `http://127.0.0.1:8000` |
| `--suite` | agent | 场景标签，默认 `core` |
| `--ids` | agent | 逗号分隔场景 id |
| `--workspace` | agent | 已授权工作区路径 |
| `--ks` | 检索 | Hit/Recall/Precision 的 K 列表，默认 `1,3,5,10` |
| `--top-k` | 检索 | 检索 limit，默认 `max(ks)` |
| `--score-threshold` | 检索 | Memory 默认 0.3；RAG 默认 None |
| `--reseed` | 检索 | 清空评测 collection 并重建语料 |
| `--enable-mqe` / `--disable-mqe` | rag_expanded | MQE 开关 |
| `--enable-hyde` / `--disable-hyde` | rag_expanded | HyDE 开关 |
| `-v` / `--verbose` | 全部 | Debug 日志 |

---

## Memory / RAG 检索说明

### 环境依赖

| 依赖 | 说明 |
|------|------|
| Qdrant | 可连上的实例，环境变量与线上一致（如 `QDRANT_URL`） |
| Embedding | `src.rag.embedding` 单例 |
| Python 包 | 与 backend 一致（`qdrant-client`、`sentence-transformers` 等） |

CLI 启动时会自动 `load_dotenv(backend/.env)`。

**隔离写入**：

| 通道 | Collection | 其它 |
|------|------------|------|
| Memory | `helloclaw_eval_memory` | `source=eval` |
| RAG | `helloclaw_eval_rag` | `rag_namespace=eval` |

### 指标定义

| 指标 | 含义 |
|------|------|
| **Hit@K** | top-K 是否至少命中一个相关项 |
| **Recall@K** | top-K 命中的相关项数 / \|R\| |
| **Precision@K** | top-K 命中的相关项数 / K |
| **MRR** | 第一个相关项秩次的倒数 |

详细标注约定与已知局限见原 Memory/RAG 文档章节；本脚手架只读评测 API，不修改 Agent 主对话链路。

---

## 退出码

- **Agent L2**：全部场景 hard_pass 为 `0`，否则 `1`
- **检索**：Qdrant/embedding 不可用或运行异常为 `1`；指标跑完即为 `0`（不设通过线）
