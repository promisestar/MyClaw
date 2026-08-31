# Memory / RAG 离线检索评测脚手架

本目录提供 **向量检索召回质量** 的离线定量评测：用合成标注集写入隔离 Qdrant collection，调用与线上一致的检索 API，计算 Hit@K / Recall@K / Precision@K / MRR，并落盘报告。

它评的是「给定 query，相关记忆或文档是否出现在 top-K」，**不是**端到端问答准确率，也不覆盖 Session Recall（SQLite FTS）、MQE/HyDE/CrossEncoder 扩展检索、或画像聚合里的 `query=category` 路径。

---

## 目录结构

```
backend/evals/
  cli.py / __main__.py   # python -m evals
  metrics.py             # 纯函数指标（可单测）
  seed_memory.py / seed_rag.py
  run_memory.py / run_rag.py
  datasets/
    memory/corpus.jsonl + queries.jsonl
    rag/corpus/*.md + queries.jsonl
  reports/               # 运行产物（默认 gitignore）
```

---

## 环境依赖

在 `backend` 目录下运行，需与日常开发相同的：

| 依赖 | 说明 |
|------|------|
| Qdrant | 可连上的实例（本地或云端），环境变量与线上一致（如 `QDRANT_URL` / `QDRANT_API_KEY`） |
| Embedding | `src.rag.embedding` 单例；可用 `EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME` 等配置 |
| Python 包 | 与 backend 一致（`qdrant-client`、`sentence-transformers` 等） |

CLI 启动时会自动 `load_dotenv(backend/.env)`。请用项目虚拟环境运行（推荐 `uv run python -m evals ...`），否则可能出现「嵌入模型不可用 / qdrant-client 未安装」。

若未配置 `QDRANT_URL`，客户端会默认连 `localhost:6333`；本机未起 Qdrant（或端口被其它服务占用返回 502）时评测会失败。已配置云端 URL + API Key 时无需本地 Docker。

**隔离写入（不会改生产库名）**：

| 通道 | Collection | 其它 |
|------|------------|------|
| Memory | `helloclaw_eval_memory` | 写入 `source=eval`；id 映射见 `reports/memory_id_map.json` |
| RAG | `helloclaw_eval_rag` | `rag_namespace=eval` |

生产默认 Memory collection（如 `helloclaw_memory`）与日常 RAG namespace **不会被本脚手架清空**，除非你主动改代码里的常量。

---

## 快速开始

```bash
cd backend

# 指标单测（不连 Qdrant）
python -m pytest tests/evals/test_metrics.py -q

# 首次或语料变更后：强制重建评测库并跑全通道
python -m evals --channel all --reseed

# 仅 Memory / 仅 RAG
python -m evals --channel memory --reseed
python -m evals --channel rag --reseed

# 调整 K 与阈值
python -m evals --channel memory --ks 1,3,5 --top-k 10 --score-threshold 0.3
python -m evals --channel rag --ks 1,3,5,10 --score-threshold 0.3
```

成功时 stdout 会打印各通道的 MRR / Hit@3 / Recall@5 摘要；详细结果在 `evals/reports/{channel}_{timestamp}.json` 与同名 `.md`。

退出码：Qdrant/embedding 不可用或运行异常为 `1`；指标跑完即为 `0`（**不设通过线**，避免误当 CI 门禁）。

---

## 指标定义

对每条 query，设相关集合为 \(R\)，检索返回有序 ID 列表（截断到检索 `limit`，评测时再按各 K 截断）：

| 指标 | 含义 |
|------|------|
| **Hit@K** | top-K 是否至少命中一个相关项（0/1） |
| **Recall@K** | top-K 命中的相关项数 / \|R\| |
| **Precision@K** | top-K 命中的相关项数 / K |
| **MRR** | 第一个相关项秩次的倒数；未命中为 0 |

报告中的 `mean_*` 为各 query 宏平均。默认评估 \(K \in \{1,3,5,10\}\)，检索 `limit` 默认为 \(\max(K)\)。

**阈值默认**：

- Memory：`score_threshold=0.3`（与线上 `search_memories` / 自动注入常用阈值一致）
- RAG：`score_threshold=None`（与 `search_vectors` 常用调用一致）；可用 CLI 覆盖

与线上 **自动注入** 的对应关系：注入默认 `top_k=3`，因此看报告里的 **Hit@3 / Recall@3** 更贴近「用户一开口能否自动带上相关记忆」。工具侧 `memory_search` 常用更大 top_k，可对照 Hit@5 / Hit@10。

---

## 标注约定

### Memory

- `datasets/memory/corpus.jsonl`：每行 `stable_key`、`content`、`category`
- `datasets/memory/queries.jsonl`：每行 `query`、`relevant_keys`（可选 `category` 过滤、`query_id`）
- Seed 时 `add_memory` 返回的真实 `memory_id` 写入 `reports/memory_id_map.json`；评测时把 `relevant_keys` 解析成 ID 再比对其检索结果中的 `id`

增补步骤：先加语料行 → 改/加查询标注 → `--reseed` 重建 id map。

### RAG

- `datasets/rag/corpus/*.md`：主题分明的短文档
- `datasets/rag/queries.jsonl`：`relevant_docs` 填文件名（如 `qdrant_ops.md`）
- **命中单位为文档级**：chunk 命中后按 `source_path` 归一化为 basename，再与标注比对。这样切块参数变化时标注仍可用

增补步骤：新增 `.md` → 写相关查询 → `--reseed`。

---

## 已知局限

1. **合成集规模小**：适合冒烟与相对对比（换模型、改阈值），不能替代大规模人工标注集。
2. **中文 + 默认 MiniLM**：绝对分数可能偏低；换中文 embedding 后应 `--reseed` 并重新比基线。
3. **RAG 文档级 vs chunk 级**：同一文档多个 chunk 会折叠成一个文档键；Precision@K 分母仍是 K，解释时注意。
4. **检索会触发 Memory 访问强化**：仅作用于评测 collection，可忽略。
5. **未评扩展检索**：MQE/HyDE/重排不在本脚手架范围内；需要时可另开 runner 调用 `search_vectors_expanded`。

---

## 与实现文档的关系

- Memory 通道说明：`docs/MyClaw实现详解/MyClaw_Memory实现文档.md`
- RAG 通道说明：`docs/MyClaw实现详解/MyClaw_RAG实现文档.md`

本脚手架只读评测 API，不修改 Agent 主对话链路。
