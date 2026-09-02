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

场景定义在 `agent/suites/scenarios/core.yaml`，当前 **50 个**，覆盖：

| 分组 | 标签 | 数量 | 关注点 |
|------|------|------|--------|
| Ask 只读门控 | `ask` / `gate` | 8 | 写/删/命令/定时任务/记忆写入均不得成功 |
| Plan 两阶段 | `plan` | 5 | `plan_generated`、TODO 条数、未确认不落盘 |
| Craft 文件与代码 | `craft` | 9 | 检索→编辑、新建、JSON/CSV、修 bug、写脚本并执行 |
| Bash 与安全 | `safety` | 4 | `COMMAND_BLOCKED` / `DIRECTORY_NOT_ALLOWED` / 正常命令可执行 |
| 工作区边界 | `workspace` | 3 | 未授权路径报错、相对路径读到夹具内容 |
| 记忆与跨会话 | `memory` | 4 | `memory_add` / `memory_search` / `session_search` |
| RAG | `rag` | 3 | `add_text` → `search` / `ask` / `stats` |
| 任务与编排 | `orchestration` | 4 | `task` / `subagent`（含并行）/ `automation` |
| 联网与浏览器 | `net` | 5 | `web_search` / `web_fetch` / `http_request` / `browser` |
| 技能与 MCP | `skill` / `mcp` | 3 | `Skill` / `skill_manage` / `mcp` |
| 健壮性 | `robustness` | 2 | 文件不存在时优雅恢复、长任务中途取消 |

> 依赖外部服务（联网、Playwright、Qdrant、MCP）的场景，硬断言只校验「工具被调用 + 有实质回复」，
> 不校验返回内容，缺 key 或依赖缺失时不会误判失败。

1. 复制夹具并在前端/API 授权工作区：

```bash
# 示例：复制到临时目录后授权该路径
xcopy /E /I evals\agent\fixtures\mini_repo %TEMP%\myclaw_eval_ws
```

夹具内容：`sample_app.py`（入口）、`src/utils.py`（价格计算，含一个待修 bug）、
`config/app_config.json`、`data/sales.csv`、`CHANGELOG.md`、`docs/architecture.md`、
`notes/*.md`（供 RAG / 总结场景使用）、`README.md`。

2. 启动后端，再跑场景：

```bash
uv run python -m evals --channel agent --suite core --workspace "%TEMP%\myclaw_eval_ws"
uv run python -m evals --channel agent --ids ask_readonly_gate,plan_generate,bash_sandbox
```

场景 YAML 需要 PyYAML（已加入 `dev` 依赖组）；若缺失会打印告警并回退到
`suites/__init__.py` 里的 7 个内置兜底场景，**不会静默只跑兜底**。

`cancel_mid_run` 场景通过新增的 `cancel_after_s` 字段实现：流式开始 N 秒后
自动调用 `POST /api/chat/cancel`，断言收到 `cancelled` 事件。

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
python -m evals --channel memory_reranked          # 同 collection，勿再 reseed
python -m evals --channel rag --reseed
python -m evals --channel rag_expanded --reseed

# Memory：有无重排 A/B（同一语料；首次 --reseed，第二次不要 reseed）
python -m evals --channel memory --reseed --ks 1,3,5,10 --top-k 10
python -m evals --channel memory_reranked --ks 1,3,5,10 --top-k 10 --candidate-k 20
# 或一次跑齐（内部只 seed 一次）
python -m evals --channel retrieval --reseed

# 调整 K 与阈值
python -m evals --channel memory --ks 1,3,5 --top-k 10 --score-threshold 0.3
python -m evals --channel rag --ks 1,3,5,10 --score-threshold 0.3
```

对比时看两份报告：`reports/memory_*.md` 与 `reports/memory_reranked_*.md` 的 **MRR / Hit@1 / Hit@3**（重排主要改善排序，大 K 的 Recall 变化通常较小）。

---

## CLI 参数一览

| 参数 | 适用通道 | 说明 |
|------|----------|------|
| `--channel` | 全部 | `agent` / `memory` / `memory_reranked` / `rag` / `rag_expanded` / `retrieval`（或 `all`） |
| `--base-url` | agent | 后端地址，默认 `http://127.0.0.1:8000` |
| `--suite` | agent | 场景标签，默认 `core` |
| `--ids` | agent | 逗号分隔场景 id |
| `--workspace` | agent | 已授权工作区路径 |
| `--ks` | 检索 | Hit/Recall/Precision 的 K 列表，默认 `1,3,5,10` |
| `--top-k` | 检索 | 检索 limit，默认 `max(ks)` |
| `--score-threshold` | 检索 | Memory 默认 0.3；RAG 默认 None |
| `--candidate-k` | memory_reranked | 重排前向量候选池，默认 `MEMORY_RERANK_CANDIDATE_K` 或 20 |
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
| Memory / Memory 重排 | `helloclaw_eval_memory` | `source=eval`；`memory` 与 `memory_reranked` 共用语料，对比时只 `--reseed` 一次 |
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
