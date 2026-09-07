<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Card, List, Empty, message, Popconfirm, Modal, Spin, Tag, Collapse, CollapsePanel } from 'ant-design-vue'
import { toolLogsApi, type LogFileInfo, type LogEntry } from '@/api/tool-logs'
import { getToolConfig } from '@/utils/toolDisplay'
import { DeleteOutlined, ClockCircleOutlined } from '@ant-design/icons-vue'

const logFiles = ref<LogFileInfo[]>([])
const loading = ref(false)

const modalOpen = ref(false)
const modalLoading = ref(false)
const modalTitle = ref('')
const modalEntries = ref<LogEntry[]>([])
const expandedTools = ref<Set<string>>(new Set())
const activeSessions = ref<string[]>([])
const activeTraces = ref<string[]>([])

interface TraceGroup {
  key: string
  traceId: string
  entries: LogEntry[]
  startTime: string
  totalDurationMs: number
  errorCount: number
}

interface SessionGroup {
  key: string
  sessionId: string
  label: string
  traces: TraceGroup[]
  entryCount: number
  startTime: string
}

const loadLogFiles = async () => {
  loading.value = true
  try {
    const res = await toolLogsApi.list()
    logFiles.value = res.files
  } catch {
    message.error('加载日志文件列表失败')
  } finally {
    loading.value = false
  }
}

const openLogFile = async (file: LogFileInfo) => {
  modalOpen.value = true
  modalTitle.value = file.file_name
  modalLoading.value = true
  modalEntries.value = []
  expandedTools.value = new Set()
  activeSessions.value = []
  activeTraces.value = []
  try {
    const res = await toolLogsApi.get(file.date_str)
    modalEntries.value = res.entries
    // 默认展开最近一个会话及其最近一条 trace，便于立刻阅读
    const groups = buildSessionGroups(res.entries)
    if (groups.length > 0) {
      activeSessions.value = [groups[0].key]
      if (groups[0].traces.length > 0) {
        activeTraces.value = [groups[0].traces[0].key]
      }
    }
  } catch {
    message.error('加载日志内容失败')
    modalOpen.value = false
  } finally {
    modalLoading.value = false
  }
}

const deleteLogFile = async (dateStr: string) => {
  try {
    await toolLogsApi.delete(dateStr)
    message.success('日志文件已删除')
    await loadLogFiles()
  } catch {
    message.error('删除日志文件失败')
  }
}

const formatSize = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const formatDate = (isoStr: string) => {
  return new Date(isoStr).toLocaleString('zh-CN')
}

const formatTime = (isoStr: string) => {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  if (Number.isNaN(d.getTime())) return isoStr
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

const statusLabel = (status: string) => {
  if (status === 'error') return { text: '失败', color: 'error' as const }
  if (status === 'timeout') return { text: '超时', color: 'warning' as const }
  if (status === 'retry') return { text: '重试', color: 'processing' as const }
  return { text: '完成', color: 'success' as const }
}

const formatResult = (result: string | undefined, resultLen?: number) => {
  if (!result) return ''
  const suffix =
    typeof resultLen === 'number' && resultLen > result.length
      ? `\n…（原始结果 ${resultLen} 字符，日志已截断）`
      : ''
  return result + suffix
}

/** 日志详情用：比聊天卡片更完整，避免把可读内容截得太短 */
const formatArgsDetail = (args: Record<string, unknown> | undefined) => {
  if (!args || Object.keys(args).length === 0) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(args)) {
    let displayValue: string
    if (typeof value === 'string') {
      displayValue = value.length > 2000 ? value.slice(0, 2000) + '…' : value
    } else if (value !== null && typeof value === 'object') {
      displayValue = JSON.stringify(value, null, 2)
      if (displayValue.length > 2000) {
        displayValue = displayValue.slice(0, 2000) + '…'
      }
    } else {
      displayValue = String(value)
    }
    parts.push(`${key}: ${displayValue}`)
  }
  return parts.join('\n')
}

const argsSummary = (args: Record<string, unknown> | undefined) => {
  if (!args || Object.keys(args).length === 0) return ''
  const preferred = ['path', 'command', 'query', 'url', 'pattern', 'file']
  for (const key of preferred) {
    const val = args[key]
    if (typeof val === 'string' && val.trim()) {
      return val.length > 72 ? val.slice(0, 72) + '…' : val
    }
  }
  const first = Object.entries(args)[0]
  if (!first) return ''
  const text = typeof first[1] === 'string' ? first[1] : JSON.stringify(first[1])
  const line = `${first[0]}=${text}`
  return line.length > 72 ? line.slice(0, 72) + '…' : line
}

const toolKey = (sessionKey: string, traceKey: string, index: number, entry: LogEntry) =>
  `${sessionKey}|${traceKey}|${index}|${entry.tool_call_id || entry.timestamp}`

const isToolExpanded = (key: string) => expandedTools.value.has(key)

const toggleTool = (key: string) => {
  const next = new Set(expandedTools.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedTools.value = next
}

const buildSessionGroups = (entries: LogEntry[]): SessionGroup[] => {
  // 保持文件顺序：同一 session / trace 首次出现的位置决定组顺序（新日志通常在末尾）
  const sessionOrder: string[] = []
  const sessionMap = new Map<string, {
    sessionId: string
    label: string
    traceOrder: string[]
    traces: Map<string, LogEntry[]>
  }>()

  for (const entry of entries) {
    const sessionId = (entry.session_id || '').trim()
    const sessionKey = sessionId || '__no_session__'
    const traceId = (entry.trace_id || '').trim() || '__no_trace__'

    if (!sessionMap.has(sessionKey)) {
      sessionOrder.push(sessionKey)
      sessionMap.set(sessionKey, {
        sessionId,
        label: sessionId || '未关联会话',
        traceOrder: [],
        traces: new Map(),
      })
    }
    const session = sessionMap.get(sessionKey)!
    if (!session.traces.has(traceId)) {
      session.traceOrder.push(traceId)
      session.traces.set(traceId, [])
    }
    session.traces.get(traceId)!.push(entry)
  }

  // 倒序：最近的会话 / 请求优先展示
  return [...sessionOrder].reverse().map((sessionKey) => {
    const session = sessionMap.get(sessionKey)!
    const traces: TraceGroup[] = [...session.traceOrder].reverse().map((traceId) => {
      const list = session.traces.get(traceId)!
      return {
        key: `${sessionKey}::${traceId}`,
        traceId: traceId === '__no_trace__' ? '' : traceId,
        entries: list,
        startTime: list[0]?.timestamp || '',
        totalDurationMs: list.reduce((sum, e) => sum + (e.duration_ms || 0), 0),
        errorCount: list.filter((e) => e.status === 'error' || e.status === 'timeout').length,
      }
    })
    return {
      key: sessionKey,
      sessionId: session.sessionId,
      label: session.label,
      traces,
      entryCount: traces.reduce((n, t) => n + t.entries.length, 0),
      startTime: traces[0]?.startTime || '',
    }
  })
}

const sessionGroups = computed(() => buildSessionGroups(modalEntries.value))

onMounted(() => {
  loadLogFiles()
})
</script>

<template>
  <div class="tool-logs-view">
    <div class="tool-logs-header">
      <div>
        <h1>工具日志</h1>
        <p>按会话与请求轮次查看工具调用记录</p>
      </div>
    </div>

    <div class="tool-logs-content">
      <Card v-if="logFiles.length > 0" class="tool-logs-card">
        <List :data-source="logFiles" :loading="loading">
          <template #renderItem="{ item }">
            <List.Item class="log-item">
              <List.Item.Meta>
                <template #title>
                  <span class="log-title">{{ item.file_name }}</span>
                </template>
                <template #description>
                  <span class="log-meta">
                    {{ item.entry_count }} 条记录 · {{ formatSize(item.size_bytes) }}
                    <span class="log-time">
                      <ClockCircleOutlined /> {{ formatDate(item.modified_at) }}
                    </span>
                  </span>
                </template>
              </List.Item.Meta>
              <template #actions>
                <button class="open-btn" @click="openLogFile(item)">打开</button>
                <Popconfirm
                  title="确定删除此日志文件？"
                  description="删除后不可恢复"
                  ok-text="删除"
                  cancel-text="取消"
                  ok-type="danger"
                  @confirm="deleteLogFile(item.date_str)"
                >
                  <button class="delete-btn" title="删除">
                    <DeleteOutlined />
                  </button>
                </Popconfirm>
              </template>
            </List.Item>
          </template>
        </List>
      </Card>

      <Card v-else class="empty-card">
        <Empty description="暂无工具调用日志">
          <p class="empty-hint">与 Agent 对话将自动记录工具调用日志</p>
        </Empty>
      </Card>
    </div>

    <Modal
      v-model:open="modalOpen"
      :title="modalTitle"
      width="920px"
      :footer="null"
      destroy-on-close
      class="tool-logs-modal"
    >
      <Spin :spinning="modalLoading" tip="加载中...">
        <div v-if="sessionGroups.length > 0" class="log-groups">
          <Collapse v-model:activeKey="activeSessions" :bordered="false" class="session-collapse">
            <CollapsePanel
              v-for="session in sessionGroups"
              :key="session.key"
            >
              <template #header>
                <div class="session-header">
                  <span class="session-label">{{ session.label }}</span>
                  <span class="session-meta">
                    {{ session.traces.length }} 轮 · {{ session.entryCount }} 次调用
                    <template v-if="session.startTime"> · {{ formatTime(session.startTime) }}</template>
                  </span>
                </div>
              </template>

              <Collapse
                v-model:activeKey="activeTraces"
                :bordered="false"
                class="trace-collapse"
              >
                <CollapsePanel
                  v-for="trace in session.traces"
                  :key="trace.key"
                >
                  <template #header>
                    <div class="trace-header">
                      <span class="trace-id">
                        {{ trace.traceId ? `trace ${trace.traceId}` : '无 trace' }}
                      </span>
                      <span class="trace-meta">
                        {{ formatTime(trace.startTime) }}
                        · {{ trace.entries.length }} 次
                        · {{ Math.round(trace.totalDurationMs) }}ms
                      </span>
                      <Tag v-if="trace.errorCount > 0" color="error" class="trace-tag">
                        {{ trace.errorCount }} 失败
                      </Tag>
                    </div>
                  </template>

                  <div class="tool-list">
                    <div
                      v-for="(entry, index) in trace.entries"
                      :key="toolKey(session.key, trace.key, index, entry)"
                      :class="['tool-card', entry.status]"
                    >
                      <div
                        class="tool-header"
                        @click="toggleTool(toolKey(session.key, trace.key, index, entry))"
                      >
                        <span class="tool-index">#{{ index + 1 }}</span>
                        <span class="tool-icon">{{ getToolConfig(entry.tool_name).icon }}</span>
                        <div class="tool-title">
                          <span class="tool-name">{{ getToolConfig(entry.tool_name).name }}</span>
                          <span class="tool-raw-name">{{ entry.tool_name }}</span>
                          <span v-if="argsSummary(entry.args)" class="tool-summary">
                            {{ argsSummary(entry.args) }}
                          </span>
                        </div>
                        <Tag :color="statusLabel(entry.status).color" class="tool-tag">
                          {{ statusLabel(entry.status).text }}
                        </Tag>
                        <span class="tool-duration">{{ entry.duration_ms }}ms</span>
                        <span class="collapse-indicator">
                          {{ isToolExpanded(toolKey(session.key, trace.key, index, entry)) ? '▼' : '▶' }}
                        </span>
                      </div>

                      <div
                        v-if="isToolExpanded(toolKey(session.key, trace.key, index, entry))"
                        class="tool-details"
                      >
                        <div class="tool-meta-row">
                          <span v-if="entry.agent_name">Agent：{{ entry.agent_name }}</span>
                          <span v-if="entry.retry_count">重试：{{ entry.retry_count }}</span>
                          <span v-if="entry.retry_attempt">第 {{ entry.retry_attempt }} 次尝试</span>
                          <span v-if="entry.error_type">错误：{{ entry.error_type }}</span>
                          <span>{{ formatDate(entry.timestamp) }}</span>
                        </div>

                        <div v-if="entry.args && Object.keys(entry.args).length > 0" class="tool-args">
                          <div class="tool-detail-label">入参</div>
                          <pre class="tool-detail-content">{{ formatArgsDetail(entry.args) }}</pre>
                        </div>

                        <div v-if="entry.result" class="tool-result-wrapper">
                          <div class="tool-detail-label">
                            结果
                            <span v-if="entry.result_len" class="result-len">{{ entry.result_len }} 字符</span>
                          </div>
                          <pre class="tool-detail-content">{{ formatResult(entry.result, entry.result_len) }}</pre>
                        </div>
                      </div>
                    </div>
                  </div>
                </CollapsePanel>
              </Collapse>
            </CollapsePanel>
          </Collapse>
        </div>
        <Empty v-else-if="!modalLoading" description="该日志文件无有效记录" />
      </Spin>
    </Modal>
  </div>
</template>

<style scoped>
.tool-logs-view {
  min-height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 24px;
  box-sizing: border-box;
}

.tool-logs-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
}

.tool-logs-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
}

.tool-logs-header p {
  margin: 0;
  color: #999;
}

.tool-logs-content {
  flex: 1;
  overflow-y: auto;
}

.tool-logs-card {
  max-width: 800px;
}

.log-item {
  padding: 16px 0;
}

.log-title {
  font-weight: 500;
  font-family: monospace;
}

.log-meta {
  color: #999;
  font-size: 13px;
}

.log-time {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 12px;
  color: #bbb;
}

.open-btn {
  padding: 0 8px;
  height: 22px;
  font-size: 12px;
  line-height: 20px;
  border: none;
  background: transparent;
  color: #333;
  cursor: pointer;
  transition: color 0.2s ease;
}

.open-btn:hover {
  color: #ff4d4f;
}

.delete-btn {
  padding: 4px 8px;
  border: none;
  background: transparent;
  color: #333;
  cursor: pointer;
  transition: color 0.2s ease;
}

.delete-btn:hover {
  color: #ff4d4f;
}

.empty-card {
  max-width: 400px;
  margin: 60px auto;
}

.empty-hint {
  color: #bbb;
  font-size: 13px;
  margin-top: 8px;
}

.log-groups {
  max-height: 72vh;
  overflow-y: auto;
  padding-right: 4px;
}

.session-collapse :deep(.ant-collapse-item),
.trace-collapse :deep(.ant-collapse-item) {
  border-bottom: 1px solid #f0f0f0;
}

.session-collapse :deep(.ant-collapse-header),
.trace-collapse :deep(.ant-collapse-header) {
  padding: 10px 8px !important;
  align-items: center !important;
}

.session-collapse :deep(.ant-collapse-content-box) {
  padding: 4px 0 8px 8px !important;
}

.trace-collapse :deep(.ant-collapse-content-box) {
  padding: 4px 0 8px 4px !important;
}

.session-header,
.trace-header {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  width: 100%;
  padding-right: 8px;
}

.session-label {
  font-weight: 600;
  font-family: ui-monospace, Consolas, monospace;
  color: #222;
}

.session-meta,
.trace-meta {
  color: #999;
  font-size: 12px;
}

.trace-id {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px;
  color: #444;
}

.trace-tag {
  margin-inline-end: 0 !important;
}

.tool-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tool-card {
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}

.tool-card.error {
  border-color: #ffccc7;
  background: #fff8f7;
}

.tool-card.timeout {
  border-color: #ffe58f;
  background: #fffbe6;
}

.tool-card.retry {
  border-color: #91caff;
  background: #f0f7ff;
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  cursor: pointer;
  user-select: none;
}

.tool-header:hover {
  background: rgba(0, 0, 0, 0.02);
}

.tool-index {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  color: #bbb;
  min-width: 28px;
}

.tool-icon {
  font-size: 15px;
  line-height: 1;
}

.tool-title {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.tool-name {
  font-weight: 500;
  color: #222;
}

.tool-raw-name {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  color: #999;
}

.tool-summary {
  font-size: 12px;
  color: #888;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 360px;
}

.tool-tag {
  margin-inline-end: 0 !important;
}

.tool-duration {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  color: #999;
  min-width: 56px;
  text-align: right;
}

.collapse-indicator {
  color: #bbb;
  font-size: 11px;
  width: 14px;
  text-align: center;
}

.tool-details {
  padding: 0 12px 12px;
  border-top: 1px dashed #f0f0f0;
}

.tool-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding-top: 10px;
  margin-bottom: 8px;
  font-size: 12px;
  color: #999;
}

.tool-args,
.tool-result-wrapper {
  margin-bottom: 8px;
}

.tool-result-wrapper:last-child {
  margin-bottom: 0;
}

.tool-detail-label {
  font-size: 11px;
  color: #999;
  margin-bottom: 4px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 8px;
}

.result-len {
  font-weight: 400;
  color: #bbb;
}

.tool-detail-content {
  margin: 0;
  padding: 8px 10px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.55;
  color: #333;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 280px;
  overflow-y: auto;
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
}
</style>
