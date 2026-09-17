<script setup lang="ts">
/**
 * 轨迹页：左侧「用户对话」轮次列表，右侧单轮 span 树。
 *
 * 右侧以「模型调用」为分组，组内是一条卡片链路：
 * REQUEST 请求 → RESPONSE 响应 → 工具调用 → 工具调用，
 * 并在分组顶部叠加横向时间瀑布条（TraceWaterfall）。
 */
import { computed, onMounted, ref } from 'vue'
import { Card, Empty, Select, Spin, Tag, message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import {
  traceApi,
  type TraceDateInfo,
  type TraceModelSpan,
  type TraceToolSpan,
  type TraceTurnSummary,
  type TraceTurnTree,
  type TraceTurnsResponse,
} from '@/api/trace'
import TraceSpanCard from '@/components/TraceSpanCard.vue'
import TraceResultBlock from '@/components/TraceResultBlock.vue'
import TraceWaterfall from '@/components/TraceWaterfall.vue'
import { getToolConfig } from '@/utils/toolDisplay'
import {
  attributionLabel,
  formatClockTime,
  formatDateTime,
  formatDuration,
  formatTokens,
  parseTs,
  resultSizeLabel,
  shortId,
  summarizeArgs,
  summarizeResult,
  uncachedTokens,
  type WaterfallItem,
} from '@/utils/traceFormat'

const dates = ref<TraceDateInfo[]>([])
const currentDate = ref('')
const sessions = ref<TraceTurnsResponse['sessions']>([])
const listLoading = ref(false)
const treeLoading = ref(false)
const activeTraceId = ref('')
const tree = ref<TraceTurnTree | null>(null)

const totalTurns = computed(() =>
  sessions.value.reduce((sum, session) => sum + session.turns.length, 0),
)

// ══════════════════ 数据加载 ══════════════════

const loadDates = async () => {
  try {
    const res = await traceApi.dates()
    dates.value = res.dates
    if (!currentDate.value || !dates.value.some((d) => d.date_str === currentDate.value)) {
      currentDate.value = dates.value[0]?.date_str || ''
    }
  } catch {
    message.error('加载轨迹日期失败')
  }
}

const loadTurns = async () => {
  if (!currentDate.value) {
    sessions.value = []
    tree.value = null
    activeTraceId.value = ''
    return
  }
  listLoading.value = true
  try {
    const res = await traceApi.turns(currentDate.value)
    sessions.value = res.sessions
    const flat = res.sessions.flatMap((session) => session.turns)
    const latest = flat.length ? flat[flat.length - 1] : undefined
    if (latest) {
      await selectTurn(latest)
    } else {
      activeTraceId.value = ''
      tree.value = null
    }
  } catch {
    message.error('加载轮次列表失败')
    sessions.value = []
  } finally {
    listLoading.value = false
  }
}

const selectTurn = async (turn: TraceTurnSummary) => {
  activeTraceId.value = turn.trace_id
  treeLoading.value = true
  try {
    tree.value = await traceApi.turn(turn.trace_id, currentDate.value)
  } catch {
    message.error('加载轨迹详情失败')
    tree.value = null
  } finally {
    treeLoading.value = false
  }
}

const refreshAll = async () => {
  await loadDates()
  await loadTurns()
}

onMounted(async () => {
  await loadDates()
  await loadTurns()
})

// ══════════════════ 展示辅助 ══════════════════

const turnStatusText = (status: string) => {
  if (status === 'error') return '失败'
  if (status === 'done') return '已完成'
  return status || '未知'
}

const turnStatusColor = (status: string) => (status === 'error' ? 'error' : 'success')

/** prompt_preview 缺失时的降级文案：区分「旧日志未采集」与「未关联会话」 */
const promptFallback = (turn: { session_id: string }) =>
  turn.session_id ? '旧日志未采集提问' : '未关联会话'

/** 归属推断说明（attribution !== 'exact' 时常驻展示，替代仅 hover 可见的 tooltip） */
const attributionNotice = computed(() => {
  const current = tree.value
  if (!current || current.attribution === 'exact') return ''
  const base =
    '本轮的「模型调用 — 工具调用」归属由时间窗推断（旧日志缺少 iteration 字段），可能与实际轮次存在偏差。'
  if (current.attribution === 'unassigned') {
    return '部分工具调用未能归属到具体轮次，已列在下方「未归属工具」。'
  }
  if (current.attribution === 'mixed') {
    return `${base}部分工具调用未能归属到具体轮次，已列在下方「未归属工具」。`
  }
  return base
})

const spanStatusText = (span: TraceModelSpan) => {
  if (span.status === 'error') return '失败'
  if (span.status === 'no_usage') return '无用量'
  return '已完成'
}

const spanStatusColor = (span: TraceModelSpan) => {
  if (span.status === 'error') return 'error'
  if (span.status === 'no_usage') return 'warning'
  return 'success'
}

const requestChips = (span: TraceModelSpan): string[] => {
  const meta = span.request
  if (!meta) return []
  const chips: string[] = []
  if (meta.system_count !== undefined) chips.push(`System ${meta.system_count}`)
  if (meta.message_count !== undefined) chips.push(`消息 ${meta.message_count}`)
  if (meta.tool_count !== undefined) chips.push(`工具定义 ${meta.tool_count}`)
  return chips
}

const responseChips = (span: TraceModelSpan): string[] => {
  const meta = span.response
  if (!meta) return []
  const chips: string[] = []
  if (meta.reasoning_tokens) chips.push(`Reasoning ${meta.reasoning_tokens}`)
  if (meta.content_len !== undefined) chips.push(`内容 ${meta.content_len}`)
  if (meta.tool_call_count !== undefined) chips.push(`工具调用 ${meta.tool_call_count}`)
  if (meta.finish_reason) chips.push(meta.finish_reason)
  return chips
}

const toolChips = (tool: TraceToolSpan): string[] => {
  const chips: string[] = [formatDuration(tool.duration_ms)]
  if (tool.result_len || tool.result) {
    chips.push(resultSizeLabel(tool.result_len, tool.result))
  }
  if (tool.retry_count) chips.push(`重试 ${tool.retry_count} 次`)
  if (tool.error_type) chips.push(tool.error_type)
  return chips
}

const toolStatus = (tool: TraceToolSpan): 'ok' | 'error' | 'warn' => {
  if (tool.status === 'error' || tool.status === 'timeout') return 'error'
  if (tool.retry_count) return 'warn'
  return 'ok'
}

const spanWaterfallItems = (span: TraceModelSpan): WaterfallItem[] => {
  const items: WaterfallItem[] = [
    {
      key: `llm-${span.seq}-${span.start_ts}`,
      title: `模型调用 #${span.seq ?? '-'}`,
      kind: 'llm',
      start_ts: span.start_ts,
      end_ts: span.end_ts,
      duration_ms: span.duration_ms,
      status: span.status,
    },
  ]
  for (const tool of span.tools) {
    items.push({
      key: `tool-${tool.tool_call_id || tool.start_ts}`,
      title: getToolConfig(tool.tool_name).name,
      kind: 'tool',
      start_ts: tool.start_ts,
      end_ts: tool.end_ts,
      duration_ms: tool.duration_ms,
      status: tool.status,
    })
  }
  return items
}

const sideSpanItems = (spans: TraceModelSpan[]): WaterfallItem[] =>
  spans
    .filter((span) => parseTs(span.start_ts) > 0)
    .map((span) => ({
      key: `side-${span.call_site}-${span.start_ts}`,
      title: span.call_site || '旁路调用',
      kind: 'side' as const,
      start_ts: span.start_ts,
      end_ts: span.end_ts,
      duration_ms: span.duration_ms,
      status: span.status,
    }))
</script>

<template>
  <div class="trace-view">
    <div class="trace-header">
      <div>
        <h1>轨迹</h1>
        <p>按会话与请求轮次查看 span 树：模型调用 → 工具调用</p>
      </div>
      <div class="header-actions">
        <Select v-model:value="currentDate" style="width: 150px" @change="loadTurns">
          <Select.Option v-for="item in dates" :key="item.date_str" :value="item.date_str">
            {{ item.date_str }}
          </Select.Option>
        </Select>
        <button class="icon-btn" title="刷新" @click="refreshAll">
          <ReloadOutlined />
        </button>
      </div>
    </div>

    <div class="trace-body">
      <!-- 左：用户对话（轮次列表） -->
      <div class="turn-panel">
        <div class="panel-title">
          用户对话
          <span class="panel-sub">{{ totalTurns }} 轮</span>
        </div>
        <Spin :spinning="listLoading">
          <div v-if="sessions.length" class="session-list">
            <div v-for="session in sessions" :key="session.session_id || 'no-session'" class="session-block">
              <div class="session-head">
                <span class="session-id" :title="session.session_id">
                  {{ shortId(session.session_id) }}
                </span>
                <span class="session-meta">{{ session.turn_count }} 轮</span>
              </div>
              <button
                v-for="turn in session.turns"
                :key="turn.trace_id"
                type="button"
                class="turn-item"
                :class="{ active: turn.trace_id === activeTraceId }"
                @click="selectTurn(turn)"
              >
                <div class="turn-line">
                  <span class="turn-title">
                    第{{ turn.index }}轮: {{ turn.prompt_preview || promptFallback(turn) }}
                  </span>
                  <Tag :color="turnStatusColor(turn.status)" class="turn-tag">
                    {{ turnStatusText(turn.status) }}
                  </Tag>
                </div>
                <div class="turn-line turn-line-sub">
                  <span>{{ formatClockTime(turn.start_ts) }}</span>
                  <span>{{ turn.model_calls }}次模型调用 · {{ turn.tool_calls }}次工具</span>
                </div>
              </button>
            </div>
          </div>
          <Empty
            v-else-if="!listLoading"
            class="panel-empty"
            :description="currentDate ? '该日期暂无轨迹记录' : '暂无轨迹日志'"
          />
        </Spin>
      </div>

      <!-- 右：单轮 span 树 -->
      <div class="tree-panel">
        <Spin :spinning="treeLoading" tip="加载中...">
          <template v-if="tree">
            <div class="turn-head">
              <div class="turn-head-main">
                <span class="turn-head-title">
                  第{{ tree.index }}轮: {{ tree.prompt_preview || promptFallback(tree) }}
                </span>
                <Tag :color="turnStatusColor(tree.status)">
                  {{ turnStatusText(tree.status) }}
                </Tag>
                <Tag v-if="tree.attribution !== 'exact'" color="warning">
                  {{ attributionLabel(tree.attribution) }}
                </Tag>
                <Tag v-for="agent in tree.agents" :key="agent">{{ agent }}</Tag>
              </div>
              <div v-if="attributionNotice" class="attribution-notice">
                {{ attributionNotice }}
              </div>
              <div class="turn-head-metrics">
                <span :title="formatDateTime(tree.start_ts)">{{ formatClockTime(tree.start_ts) }}</span>
                <span>· {{ formatDuration(tree.duration_ms) }}</span>
                <span>· {{ tree.model_calls }}次模型调用</span>
                <span>· {{ tree.tool_calls }}次工具</span>
                <span>· {{ formatTokens(tree.usage.total_tokens) }} tokens</span>
              </div>
            </div>

            <!-- 模型调用分组 -->
            <Card
              v-for="span in tree.model_spans"
              :key="`model-${span.seq}-${span.start_ts}`"
              class="model-card"
              :body-style="{ padding: '12px 14px' }"
            >
              <div class="model-head">
                <span class="model-title">模型调用 #{{ span.seq ?? '-' }}</span>
                <span class="model-model">{{ span.model }}</span>
                <span class="model-metrics">
                  <span>{{ formatClockTime(span.start_ts) }}</span>
                  <span>· {{ formatDuration(span.duration_ms) }}</span>
                  <span>· 输入 {{ formatTokens(span.usage.prompt_tokens) }}</span>
                  <span>
                    · 未缓存
                    {{ formatTokens(uncachedTokens(span.usage.prompt_tokens, span.usage.cached_tokens)) }}
                  </span>
                  <span>· 缓存命中 {{ formatTokens(span.usage.cached_tokens) }}</span>
                  <span>· 输出 {{ formatTokens(span.usage.completion_tokens) }}</span>
                </span>
                <Tag :color="spanStatusColor(span)">{{ spanStatusText(span) }}</Tag>
              </div>

              <TraceWaterfall :items="spanWaterfallItems(span)" />

              <div class="chain">
                <TraceSpanCard
                  label="REQUEST 请求"
                  tone="request"
                  :title="span.model"
                  :chips="requestChips(span)"
                  :placeholder="span.request ? '' : '旧日志未采集请求信息'"
                />

                <span class="chain-arrow">→</span>

                <TraceSpanCard
                  label="RESPONSE 响应"
                  tone="response"
                  :chips="responseChips(span)"
                >
                  <p v-if="span.response?.content_preview">
                    {{ summarizeResult(span.response.content_preview, 140) }}
                  </p>
                  <p v-else-if="span.response" class="muted">（无内容预览）</p>
                  <p v-else class="muted">旧日志未采集响应内容</p>
                </TraceSpanCard>

                <template v-for="tool in span.tools" :key="tool.tool_call_id || tool.start_ts">
                  <span class="chain-arrow">→</span>
                  <TraceSpanCard
                    :label="getToolConfig(tool.tool_name).name"
                    tone="tool"
                    :icon="getToolConfig(tool.tool_name).icon"
                    :title="summarizeArgs(tool.args)"
                    :chips="toolChips(tool)"
                    :status="toolStatus(tool)"
                    expandable
                  >
                    <TraceResultBlock v-if="tool.result" :text="tool.result" />
                    <p v-else class="muted">（无结果）</p>
                  </TraceSpanCard>
                </template>
              </div>

              <div v-if="span.error" class="span-error">{{ span.error }}</div>
            </Card>

            <Card v-if="!tree.model_spans.length" class="model-card">
              <Empty description="该轮次没有主循环模型调用记录" />
            </Card>

            <!-- 旁路调用（压缩 / 摘要 / 画像） -->
            <Card v-if="tree.side_spans.length" class="model-card" title="旁路调用">
              <TraceWaterfall :items="sideSpanItems(tree.side_spans)" />
              <div class="side-list">
                <div v-for="span in tree.side_spans" :key="`side-${span.call_site}-${span.start_ts}`" class="side-row">
                  <span class="side-site">{{ span.call_site }}</span>
                  <span class="side-model">{{ span.model }}</span>
                  <span class="side-meta">
                    {{ formatClockTime(span.start_ts) }} · {{ formatDuration(span.duration_ms) }}
                    · {{ formatTokens(span.usage.total_tokens) }} tokens
                  </span>
                </div>
              </div>
            </Card>

            <!-- 未归属工具 -->
            <Card v-if="tree.unassigned_tools.length" class="model-card" title="未归属工具">
              <div class="chain">
                <TraceSpanCard
                  v-for="tool in tree.unassigned_tools"
                  :key="`unassigned-${tool.tool_call_id || tool.start_ts}`"
                  :label="getToolConfig(tool.tool_name).name"
                  tone="tool"
                  :icon="getToolConfig(tool.tool_name).icon"
                  :title="summarizeArgs(tool.args)"
                  :chips="toolChips(tool)"
                  :status="toolStatus(tool)"
                  expandable
                >
                  <TraceResultBlock v-if="tool.result" :text="tool.result" />
                  <p v-else class="muted">（无结果）</p>
                </TraceSpanCard>
              </div>
            </Card>
          </template>

          <Empty v-else-if="!treeLoading" class="tree-empty" description="请选择左侧的某一轮对话" />
        </Spin>
      </div>
    </div>
  </div>
</template>

<style scoped>
.trace-view {
  height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 24px;
  box-sizing: border-box;
  gap: 16px;
}

.trace-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

.trace-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
  color: var(--color-text);
}

.trace-header p {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.icon-btn {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s ease;
}

.icon-btn:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.trace-body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 16px;
}

/* ── 左：轮次列表 ── */
.turn-panel {
  width: 320px;
  flex: 0 0 320px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.panel-title {
  padding: 10px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  border-bottom: 1px solid var(--color-border);
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.panel-sub {
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-tertiary);
}

/* Spin 包裹层需要参与 flex 高度分配，列表才能独立滚动 */
.turn-panel :deep(.ant-spin-nested-loading),
.turn-panel :deep(.ant-spin-container) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.turn-panel :deep(.ant-spin-container) {
  overflow-y: auto;
}

.session-list {
  padding: 6px 6px 12px;
}

.session-block + .session-block {
  margin-top: 10px;
}

.session-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 6px 6px 4px;
}

.session-id {
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  font-size: 11px;
  color: var(--color-text-tertiary);
}

.session-meta {
  font-size: 11px;
  color: var(--color-text-tertiary);
}

.turn-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.turn-item:hover {
  background: var(--surface-1);
}

.turn-item.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.turn-line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.turn-line-sub {
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-text-tertiary);
  gap: 10px;
}

.turn-title {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.turn-tag {
  margin-inline-end: 0 !important;
  flex: 0 0 auto;
}

.panel-empty {
  padding: 32px 16px;
}

/* ── 右：span 树 ── */
.tree-panel {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding-right: 4px;
}

.turn-head {
  margin-bottom: 14px;
}

.turn-head-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.turn-head-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
}

.turn-head-metrics {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* 归属推断常驻说明（旧日志缺少 iteration 字段） */
.attribution-notice {
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: rgba(250, 173, 20, 0.1);
  border: 1px solid rgba(250, 173, 20, 0.35);
  color: #ad6800;
  font-size: 12px;
  line-height: 1.5;
}

[data-theme='dark'] .attribution-notice {
  color: #faad14;
  background: rgba(250, 173, 20, 0.12);
  border-color: rgba(250, 173, 20, 0.3);
}

.model-card {
  margin-bottom: 14px;
  border-radius: var(--radius-md);
}

.model-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.model-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.model-model {
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.model-metrics {
  flex: 1;
  min-width: 0;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
  font-size: 12px;
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  color: var(--color-text-secondary);
}

.chain {
  display: flex;
  align-items: stretch;
  gap: 6px;
  flex-wrap: wrap;
}

.chain-arrow {
  align-self: center;
  color: var(--color-text-tertiary);
  font-size: 13px;
}

.span-error {
  margin-top: 8px;
  font-size: 12px;
  color: #ff4d4f;
  word-break: break-word;
}

.muted {
  color: var(--color-text-tertiary);
}

.side-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.side-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 12px;
}

.side-site {
  min-width: 132px;
  color: var(--color-text-secondary);
}

.side-model {
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  color: var(--color-text-secondary);
}

.side-meta {
  color: var(--color-text-tertiary);
}

.tree-empty {
  margin-top: 80px;
}
</style>
