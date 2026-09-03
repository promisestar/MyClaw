# MyClaw Agent 轨迹评分方案（Trace-based Soft Scoring）

> 目标：在现有「规则硬断言」链路之外，扩展一条基于完整轨迹（trace）的质量评分路径，
> 解决当前 `soft_score` 无区分度的问题。
>
> 结论：**可行，且当前代码与数据已具备全部前提条件。**

---

## 一、现状诊断：问题比"规则简单"更严重

### 1.1 `soft_score` 与 `hard_pass` 信息完全重合

`evals/agent/harness/scorers.py:155`：

```python
soft = 1.0 if not violations else max(0.0, 1.0 - 0.2 * len(violations))
```

`soft_score` 是 `violations` 数量的纯线性函数，而 `hard_pass = (len(violations) == 0)`。
**两者携带的信息完全相同**——`soft_score` 没有提供任何 `hard_pass` 之外的信号。

### 1.2 真实数据验证：通过场景零区分度

取最近一次全量报告 `evals/reports/agent_20260902T020445Z.json`（50 场景）：

| hard_pass | soft_score | 场景数 |
|---|---|---|
| True | **1.0** | 40 |
| False | 0.8 | 8 |
| False | 0.6 | 1 |
| False | 0.0 | 1 |

40 个通过场景的 `soft_score` **唯一值为 `[1.0]`**——「勉强通过」与「漂亮完成」得分完全相同。
报告中的 `avg_soft_score = 0.94` 只是 `gate_pass_rate = 0.8` 的另一种表达，无任何增量信息。

### 1.3 规则断言表达不了的东西

硬断言只能回答"是否"，无法回答"好不好"。以下全是当前框架盲区：

- 调用了 `Write`，但**改错了文件**（工具对了，目标错了）
- 同一文件被 `Read` 了 5 次（冗余）
- 工具报错后**直接放弃或编造结果**，而非重试/换策略
- 最终回答**声称已完成，但轨迹里根本没有对应的工具调用**（幻觉）
- Plan 列了 5 步，实际只执行了 2 步（计划-执行脱节）
- 用 `bash cat` 读文件而不用 `Read`（工具选择不当）

---

## 二、可行性论证：前提条件已全部具备

| 前提 | 现状 | 结论 |
|---|---|---|
| trace 数据是否完整 | `StreamTrace` 存全量 `events`，`tool_finishes` 含完整 `result`，另有 `plan`/`done_content`/`context_usage`/`latency_ms` | ✅ 够用 |
| LLM 调用能力 | `HelloAgentsLLM(model=, api_key=, base_url=)` 支持显式传参 + `LLM_*` 环境变量回退；`invoke()` 为非流式 | ✅ 可复用 |
| 配置加载 | `evals/cli.py:18-20` 已 `load_dotenv(_BACKEND_ROOT / ".env")` | ✅ judge 可直接读配置 |
| judge 能否独立于被测模型 | 构造参数可显式传入，可用 `EVAL_JUDGE_MODEL_ID` 等独立变量 | ✅ 可避免自评偏见 |
| 输入体量是否可控 | 见下方实测 | ✅ 成本极低 |
| 报告结构能否扩展 | JSON 报告，`CaseResult.to_dict()` 单处出口 | ✅ 扩展成本低 |

### 2.1 输入体量实测（关键可行性依据）

50 个场景共 18634 个 SSE 事件，类型分布：

| 事件 | 数量 | 占比 |
|---|---|---|
| `chunk`（文本流） | 17784 | **95.4%** |
| `tool_start` | 207 | 1.1% |
| `tool_finish` | 206 | 1.1% |
| `step_start` / `step_finish` | 191 / 144 | 1.8% |
| `session` / `done` | 48 / 47 | 0.5% |
| `plan_generated` / `error` / `cancelled` | 3 / 3 / 1 | ~0% |

**95.4% 是 chunk 文本流，而它与 `done_content` 重复，必须剔除。**
剔除后有效信号仅 **850 个事件、207 次工具调用**，单场景工具调用 **1–9 次**。

→ 压缩后单场景 judge 输入约 **1–3k token**，全量 50 场景约 10 万 token 输入。
用轻量模型一次评测成本几乎可忽略。

### 2.2 附带发现：`step_start` 含 `max_steps`

`src/api/chat.py:199-207` 的 `step_start` 带 `step` 与 `max_steps`，
可直接算出**步数效率**（用了几步 / 上限几步），这是零成本的轨迹质量指标。

---

## 三、方案总览：三层评分，而非只加一个 LLM judge

只加 LLM judge 是过度设计——大量质量信号其实可以**确定性**算出，且零成本、可复现、能进 CI。
因此设计为三层，按性价比自上而下：

| 层 | 名称 | 手段 | 成本 | 可复现 | 能否进 CI 红线 |
|---|---|---|---|---|---|
| **L2.1** | 轨迹级确定性指标 | 规则（基于全轨迹，非仅 violations） | 零 | ✅ 完全 | ✅ 可以 |
| **L2.2** | LLM-as-judge | rubric 打分 | 低 | ⚠️ temperature=0 下较稳 | ❌ 不可以 |
| **L2.3** | 执行验证 | 跑测试 / 校验落盘结果 | 中 | ✅ 完全 | ✅ 可以 |

**先做 L2.1**——它投入最小，却能立刻让 `soft_score` 具备区分度。
L2.2 解决 L2.1 覆盖不到的语义质量。L2.3 最强但只适用于可验证场景。

---

## 四、L2.1：轨迹级确定性指标（优先实施）

从 `StreamTrace` 直接算出，无需 LLM。全部为 `0.0–1.0` 归一化的**扣分项或得分项**：

| 指标 | 计算方式 | 发现的问题 |
|---|---|---|
| `redundant_calls` | 同一 `(tool, 关键参数)` 出现次数 > 1 的比例 | 重复读同一文件 |
| `error_recovery_rate` | 工具报错后是否重试/换工具（而非直接 done） | 遇错即弃 |
| `silent_failure` | 轨迹有 error，但最终回答未提及 | 隐瞒失败 |
| `plan_execution_coverage` | 实际工具调用覆盖的 plan 项数 / plan 总项数 | 计划-执行脱节 |
| `step_efficiency` | `steps_used / max_steps` | 接近上限 = 险些失控 |
| `empty_result_calls` | 工具返回空/无变化仍继续调用的次数 | 空转 |
| `tool_selection` | 用 `bash cat/grep` 替代 `Read/search_content` 的次数 | 工具选择不当 |
| `context_growth` | `context_usage` 增长速率 | 上下文膨胀 |
| `claimed_not_done` | 回答声称写入，但轨迹无对应写工具 | **幻觉**（强信号） |

> `claimed_not_done` 用关键词匹配（"已写入/已保存/已修改" + 无 `Write`/`Edit` 调用），
> 是最有价值的红线候选——它抓的是"撒谎"，比任何质量分都重要。

**soft_score 改造**：`soft = w1 * rule_score + w2 * (1 - penalty)`，
其中 penalty 由上述指标加权得出。通过场景因此能拉开差距。

---

## 五、L2.2：LLM-as-judge 详细设计

### 5.1 架构原则：judge 与硬断言彻底解耦

```
StreamTrace ──► TraceDigest（压缩、脱敏）──► LLM Judge ──► JudgeResult
                                                              │
Scenario.expect ──► score_scenario() ──► CaseResult.hard_pass ─┤
                                                              ▼
                                                    CaseResult.judge（新字段）
```

**三条硬约束**：

1. **judge 分数绝不参与 `hard_pass`**。红线必须确定性，LLM 有随机性。
2. **`soft_score` 保留为规则分**，judge 结果写入独立的 `judge_score` 字段。
   → 向后兼容现有脚本；且可对比两者做一致性分析，验证 judge 是否真的有效。
3. **judge 默认关闭**，通过 `--judge` 显式启用。

### 5.2 TraceDigest：压缩与脱敏

```python
@dataclass
class TraceDigest:
    scenario_id: str
    user_request: str          # Scenario.message
    mode: str
    tool_calls: List[ToolCallDigest]   # 序号/工具名/参数摘要/结果摘要/是否报错
    steps_used: int; max_steps: int
    plan: Optional[List[str]]
    final_answer: str
    errors: List[str]
    cancelled: bool
    latency_ms: float
```

压缩规则：

- **丢弃全部 `chunk`**（占 95.4%，与 `done_content` 重复）
- 单个 `result` 截断至 800 字符，参数截断至 300 字符，超出标注 `[截断 N 字符]`
- `final_answer` 截断至 2000 字符

**脱敏（防锚定偏见）**：digest 中**不得包含** `violations`、`hard_pass`、
`expect` 的规则断言内容。judge 若知道"已通过"，会系统性抬高评分。
judge 只应看到：用户请求 + rubric + 轨迹事实。

### 5.3 评分维度与 rubric

五维度，各 1–5 分：

| 维度 | 含义 |
|---|---|
| `task_completion` | 是否真正达成用户目标（而非"做了动作"） |
| `tool_efficiency` | 有无冗余调用、是否选对工具 |
| `trajectory_quality` | 步骤顺序是否合理、是否盲目试错 |
| `error_recovery` | 遇错是否重试/换策略，而非放弃或编造 |
| `response_quality` | 最终回答是否准确、完整、无幻觉 |

**rubric 是 judge 质量的上限**，比选什么模型更重要。YAML 场景可覆盖：

```yaml
  - id: craft_search_then_edit
    # ... 既有字段 ...
    judge:
      enabled: true
      weights:
        task_completion: 0.35
        tool_efficiency: 0.25
        trajectory_quality: 0.20
        error_recovery: 0.10
        response_quality: 0.10
      rubric: |
        好的轨迹应：先用 search_content 定位符号，再定点 Edit；
        不得全文重写文件；修改后应确认结果。
        若直接用 Write 覆盖整个文件，tool_efficiency 不应高于 2 分。
```

未配置 `judge` 的场景使用通用 rubric。门控类场景（如 `ask_readonly_gate`）
建议 `judge.enabled: false`——规则判据已充分，judge 只会引入噪声。

### 5.4 judge 输出契约

要求 LLM 返回严格 JSON（解析失败降级为 `judge_score=None`，**不让评测崩掉**）：

```json
{
  "scores": {
    "task_completion":    {"score": 4, "reason": "..."},
    "tool_efficiency":    {"score": 3, "reason": "..."},
    "trajectory_quality": {"score": 4, "reason": "..."},
    "error_recovery":     {"score": 5, "reason": "..."},
    "response_quality":   {"score": 4, "reason": "..."}
  },
  "overall": 3.95,
  "summary": "先检索后定点修改，路径正确；但对同一文件重复读取 3 次。",
  "red_flags": []
}
```

`red_flags` 用于记录严重问题（幻觉、隐瞒失败、破坏性操作），
即使 `overall` 不低也应单独告警——这是 judge 相对规则最大的增量价值。

### 5.5 去偏与可复现

| 风险 | 对策 |
|---|---|
| **自评偏见** | judge 模型须独立于被测模型；`EVAL_JUDGE_MODEL_ID` 单独配置，**若与被测模型同名则告警** |
| **随机性** | `temperature=0`；报告记录 judge 模型名与版本 |
| **长度偏见** | rubric 中明示"简洁不啰嗦也是优点"，禁止以回答长度论优劣 |
| **锚定偏见** | digest 剔除 violations / hard_pass / expect 规则 |
| **解析失败** | JSON 解析失败或字段缺失 → `judge_score=None` + 记 warning，不崩、不影响 hard_pass |
| **缺失配置** | 无 judge 配置 → 跳过并告警，**不静默降级、不让评测失败** |

### 5.6 CLI 与报告

```bash
python -m evals --channel agent --suite core --judge
python -m evals --channel agent --ids craft_search_then_edit --judge --judge-repeat 3
```

报告新增：

```json
{
  "summary": {
    "gate_pass_rate": 0.80,
    "avg_soft_score": 0.94,
    "judge": {
      "model": "glm-4-plus",
      "scored": 42,
      "skipped": 8,
      "avg_judge_score": 3.71,
      "red_flags_count": 3
    }
  }
}
```

单场景结果新增 `judge` 子对象（维度分、理由、red_flags）。

---

## 六、judge 自身的校准（不可省略）

**未经验证的 judge 是自欺欺人**。必须建立黄金集：

1. **构建黄金集**：人工标注 15–20 条 trace 为 好 / 中 / 差 三档，覆盖各分组。
2. **自一致性**：同一 trace 用 judge 跑 3 次，看分数方差；完全一致率应 > 80%。
3. **与人工相关性**：算 judge 分与人工分的 Spearman 相关系数，**目标 > 0.6**。
   低于 0.6 时，**改 rubric，而不是换模型**——rubric 才是瓶颈。
4. **判别力检验**：黄金集里"好"与"差"两组的 judge 均值应有显著差距（如 ≥ 1.0 分）。
5. **回归**：judge 模型升级后重跑黄金集，防止评分漂移。

---

## 七、实施路径

| 阶段 | 内容 | 依赖 | 产出 |
|---|---|---|---|
| **P1** | L2.1 轨迹指标（9 个指标 + soft_score 改造） | 无 | `evals/agent/harness/trace_metrics.py` |
| **P2** | judge 基础设施：Digest / JudgeResult / CLI / 报告字段 | P1 | `evals/agent/harness/judge.py` |
| **P3** | rubric 编排：通用 rubric + 20 个重点场景定制 | P2 | YAML `judge` 段 |
| **P4** | 校准：黄金集 + 相关性检验 + 调 rubric | P3 | 校准报告 |
| **P5** | L2.3 执行验证（对可验证场景跑断言/测试） | P1 | 按需 |

**建议 P1 与 P2 合并交付**——L2.1 立刻给 `soft_score` 区分度，judge 提供语义层补充。

工作量估计：P1 约 0.5 天，P2 约 1 天，P3 约 1 天，P4 约 1 天（含人工标注）。

---

## 八、风险与局限（诚实清单）

1. **judge 不能进 CI 红线**。它只能用于趋势观察与人工诊断。
2. **judge 会漂移**：模型版本更新后分数不可直接跨版本比较，需靠黄金集回归。
3. **rubric 质量决定一切**：写得含糊的 rubric 会让 judge 输出接近随机数，且**看起来很正常**。
4. **judge 无法验证代码真能跑**：它只看轨迹。要真正验证，必须靠 L2.3 执行验证
   （写完脚本后真跑一遍、检查文件内容）。这是比 judge 更强的信号，但只适用于可验证场景。
5. **成本会随频率累积**：单次评测很便宜，但若接入每日 CI 需注意调用量。
6. **门控类场景开 judge 会引入噪声**：应显式关闭。

---

## 九、验收标准

- [ ] 通过场景的 `soft_score` **不再全部为 1.0**，标准差 > 0.05
- [ ] `avg_soft_score` 相对 `gate_pass_rate` 提供增量信息（二者不再可互相推导）
- [ ] judge 在黄金集上与人工分的 Spearman 相关 > 0.6
- [ ] judge 跑 3 次自一致率 > 80%
- [ ] judge 缺失配置/调用失败/JSON 解析失败时，评测**正常完成**且 hard_pass 不受影响
- [ ] `pytest tests/evals -q` 全绿（新增 judge 相关契约测试）
