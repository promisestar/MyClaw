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

场景定义在 `agent/suites/scenarios/core.yaml`，当前 **100 个**，覆盖：

| 分组 | 标签 | 数量 | 关注点 |
|------|------|------|--------|
| Ask 只读门控 | `ask` / `gate` | 8 | 写/删/命令/定时任务/记忆写入均不得成功 |
| Plan 两阶段 | `plan` | 5 | `plan_generated`、TODO 条数、未确认不落盘 |
| Craft 文件与代码 | `craft` | 18 | 检索→编辑、新建、JSON/CSV、修 bug、脚本、代码审查/测试/重构/调试 |
| Bash 与安全 | `safety` | 11 | 危险命令拦截、提示注入、数据外传、密钥泄露、提权 |
| 工作区边界 | `workspace` | 4 | 未授权路径报错、相对路径读到夹具内容 |
| 记忆与跨会话 | `memory` | 9 | 写入/检索/跨会话/分类过滤/会话检索 |
| RAG | `rag` | 7 | 文本/文件入库、ask/search/stats、命名空间隔离、clear |
| 任务与编排 | `orchestration` | 13 | task 全 action、subagent、automation 全 action |
| 联网与浏览器 | `net` | 11 | web 检索/抓取/http/浏览器导航/截图/JS 求值 |
| 技能与 MCP | `skill` / `mcp` | 5 / 3 | 技能创建/写文件/删除、MCP 资源/调用 |
| 上下文治理 | `guard` | 5 | ContextGuard 委托、截断、副作用工具永不委托 |
| 多模态 | `multimodal` | 4 | 图片理解、xlsx/pdf 提取、看图落盘 |
| 代码能力 | `code` | 7 | 审查、单元测试、重构、错误处理、调试、类型标注 |
| 健壮性 | `robustness` | 7 | 文件缺失恢复、取消、矛盾指令、模糊请求、注入对抗 |

> 依赖外部服务（联网、Playwright、Qdrant、MCP、VLM）的场景，硬断言只校验「工具被调用 + 有实质回复」，
> 不校验返回内容，缺 key 或依赖缺失时不会误判失败。

##### 场景里的工具名怎么写

场景断言写的是「父工具名」，SSE 实际上报的可能是别名或子工具名，由
`evals/agent/harness/tool_aliases.py` 做归一化后再匹配，因此下面这些写法都有效：

| 场景里可写 | 实际匹配 |
|---|---|
| `bash` / `execute_command` | 两者互配（后端注册名是 `execute_command`） |
| `calculator` / `python_calculator` | 两者互配 |
| `memory` / `memory_search` / `memory_add` | `memory` 是注册工具，后两者是其 action，均匹配 `memory` |
| `rag` | 额外匹配 `rag_add_text` / `rag_search` / `rag_ask` / `rag_stats` |
| `Read` / `Write` / `Edit` / `Skill` | 大小写不敏感匹配 |
| `mcp` | 额外匹配 `mcp_*` 子工具与已知网关名 |

新增工具时若断言始终不生效，优先检查这张别名表是否需要同步。

1. 复制夹具并在前端/API 授权工作区：

```bash
# 示例：复制到临时目录后授权该路径
xcopy /E /I evals\agent\fixtures\mini_repo %TEMP%\myclaw_eval_ws
```

夹具内容：`sample_app.py`（入口）、`src/utils.py`（价格计算，含一个待修 bug）、
`config/app_config.json`、`data/sales.csv`、`CHANGELOG.md`、`docs/architecture.md`、
`notes/*.md`（供 RAG / 总结场景使用）、`README.md`。

2. 启动后端，再跑场景：

```powershell
uv run python -m evals --channel agent --suite core --workspace "$env:TEMP\myclaw_eval_ws"
uv run python -m evals --channel agent --ids ask_readonly_gate,plan_generate,bash_sandbox
```

`--workspace` 应为已授权的夹具副本。runner 会在**每个场景开始前**用 `fixtures/mini_repo`
覆盖还原工作区文件（保留 `.myclaw` 授权元数据），避免前序 Craft/门控失败污染后续断言。
所有场景在 `timeout_s` 到期时会调用 `/api/chat/cancel`，防止单场景长时间挂死。

场景 YAML 需要 PyYAML（已加入 `dev` 依赖组）；若缺失、YAML 语法错误或字段缺失，
都会打印告警并回退到 `suites/__init__.py` 里的 7 个内置兜底场景，**不会静默只跑兜底**。

`cancel_mid_run` 场景通过 `cancel_after_s` 字段实现：流式开始 N 秒后
自动调用 `POST /api/chat/cancel`，断言收到 `cancelled` 事件（与场景 `timeout_s` 取较早者）。

> ⚠️ 后端 `/api/chat/cancel` 作用于**全局当前活跃令牌**（无 session 维度），
> 因此评测必须串行执行（runner 已是串行）。runner 在流结束后会立即撤销待触发的
> canceller 并等待其收尾，避免迟到的 cancel 误伤下一个场景。

报告默认写到 `evals/reports/agent_<timestamp>.json`。

#### 轨迹评分（soft_score 与 LLM-as-judge）

L2 场景除了硬断言（`hard_pass`，只回答「是否合规」），还输出**轨迹质量分**：

- **`soft_score`（规则分，默认开启）**：`hard_pass` 之外叠加 `trace_metrics` 的确定性
  折扣，回答「做得好不好」。此前 `soft_score` 是 violations 数量的线性函数、与
  `hard_pass` 信息完全重合（40 个通过场景分数全为 1.0），现已改造为
  `rule_score × (1 - trace_penalty)`，其中 `trace_penalty` 来自 `harness/trace_metrics.py`
  的 9 个确定性指标（幻觉、隐瞒失败、重复调用、工具误用、plan 覆盖、步数超支等）。
  核心价值：抓「回答声称已写入但轨迹无写工具」这类**幻觉**，纯规则、可进 CI 红线。

- **`judge_score`（LLM-as-judge，默认关闭）**：`harness/judge.py` 基于脱敏轨迹打分
  （5 个维度：任务完成度/工具效率/轨迹质量/错误恢复/回答质量，1–5 分）。用
  `--judge` 启用；`--judge-repeat N` 多次打分检验自一致性。judge 分**绝不参与
  `hard_pass`**，只作参考，与 `soft_score` 并存便于对比。

```powershell
# 启用 LLM-as-judge（模型独立配置，缺省回退 LLM_*）
uv run python -m evals --channel agent --suite core --judge --judge-repeat 3
```

judge 模型由 `EVAL_JUDGE_MODEL_ID` / `EVAL_JUDGE_API_KEY` / `EVAL_JUDGE_BASE_URL`
配置，缺省回退 `LLM_*`；与被测模型同名时会告警（自评偏见）。judge 缺配置、调用
异常或 JSON 解析失败时**只降级为 `judge=n/a`，绝不让评测失败**。

场景级 judge 配置写在 `core.yaml` 的 `judge:` 段（可 `enabled: false` 关闭门控类
场景、`rubric:` 追加专属要求、`weights:` 调整维度权重），详情见
`docs/MyClaw_Agent轨迹评分方案.md`。

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
| `--judge` | agent | 启用 LLM-as-judge 轨迹评分（默认关闭，仅参考，不影响 hard_pass） |
| `--judge-repeat` | agent | 每条轨迹重复 judge 次数，检验自一致性（默认 1） |
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
