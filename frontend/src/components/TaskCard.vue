<script setup lang="ts">
import { computed } from 'vue'
import { Tag } from 'ant-design-vue'
import { LoadingOutlined } from '@ant-design/icons-vue'

/** 任务状态类型 */
type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'cancelled'

interface Props {
  /** 工具入参 */
  args: Record<string, unknown>
  /** 工具结果文本（原始字符串） */
  result?: string
  /** 工具执行状态 */
  status: 'running' | 'done' | 'error'
  /** 是否展开 */
  expanded: boolean
}

const props = withDefaults(defineProps<Props>(), {
  result: '',
})

const emit = defineEmits<{
  (e: 'toggle'): void
}>()

/** 解析 task 工具的 action 参数 */
const action = computed<string>(() => {
  return String(props.args?.action || '').trim()
})

/** 解析 subject 参数 */
const subject = computed<string>(() => {
  return String(props.args?.subject || '').trim()
})

/** 解析 task_id 参数 */
const taskId = computed<string>(() => {
  return String(props.args?.task_id || '').trim()
})

/** 解析 depends_on 参数 */
const dependsOn = computed<string>(() => {
  const raw = props.args?.depends_on
  if (typeof raw === 'string') return raw
  if (Array.isArray(raw)) return raw.join(', ')
  return ''
})

/** 从 result 中尝试解析 data 字段（后端 ToolResponse.success 的 data） */
const parsedData = computed<Record<string, unknown> | null>(() => {
  if (!props.result) return null
  // 后端返回的 result 是文本形式，可能包含 JSON
  try {
    // 尝试直接解析为 JSON
    return JSON.parse(props.result)
  } catch {
    // 尝试从文本中提取 JSON 块
    const jsonMatch = props.result.match(/\{[\s\S]*\}/)
    if (jsonMatch) {
      try {
        return JSON.parse(jsonMatch[0])
      } catch {
        return null
      }
    }
    return null
  }
})

/** 解析出的任务列表（task_list 动作返回） */
const taskList = computed<Array<{
  id: string
  subject: string
  status: TaskStatus
  blocked_by: string[]
}> | null>(() => {
  const data = parsedData.value
  if (!data) return null
  const tasks = data.tasks
  if (!Array.isArray(tasks)) return null
  return tasks.map((t: Record<string, unknown>) => ({
    id: String(t.id || ''),
    subject: String(t.subject || ''),
    status: String(t.status || 'pending') as TaskStatus,
    blocked_by: Array.isArray(t.blocked_by) ? t.blocked_by.map(String) : [],
  }))
})

/** 是否为列表/进度类动作（需要展示完整快照） */
const isListAction = computed(() => {
  return action.value === 'task_list' || action.value === 'task_progress'
})

/** 是否为创建类动作 */
const isCreateAction = computed(() => action.value === 'task_create')

/** 是否为完成类动作 */
const isCompleteAction = computed(() => action.value === 'task_complete')

/** 是否为失败类动作 */
const isFailAction = computed(() => action.value === 'task_fail')

/** 是否为取消类动作 */
const isCancelAction = computed(() => action.value === 'task_cancel')

/** 是否为开始类动作 */
const isStartAction = computed(() => action.value === 'task_start')

/** 卡片标题 */
const cardTitle = computed(() => {
  const actionMap: Record<string, string> = {
    task_create: '创建任务',
    task_start: '开始任务',
    task_complete: '完成任务',
    task_fail: '任务失败',
    task_cancel: '取消任务',
    task_list: '任务列表',
    task_progress: '任务进度',
  }
  return actionMap[action.value] || '任务操作'
})

/** 卡片图标 */
const cardIcon = computed(() => {
  if (isCreateAction.value) return '➕'
  if (isCompleteAction.value) return '✅'
  if (isFailAction.value) return '❌'
  if (isCancelAction.value) return '🚫'
  if (isStartAction.value) return '▶️'
  if (isListAction.value) return '📋'
  return '📋'
})

/** 状态图标映射 */
const statusIcons: Record<TaskStatus, string> = {
  pending: '⏳',
  in_progress: '🔄',
  completed: '✅',
  failed: '❌',
  cancelled: '🚫',
}

/** 简短预览文本（折叠状态下显示） */
const previewText = computed(() => {
  if (isListAction.value && taskList.value) {
    return `${taskList.value.length} 个任务`
  }
  if (isCreateAction.value && subject.value) {
    let text = subject.value
    if (dependsOn.value) text += `（依赖: ${dependsOn.value}）`
    return text
  }
  if (taskId.value) {
    return `[${taskId.value}] ${subject.value}`.trim()
  }
  return ''
})
</script>

<template>
  <div :class="['task-card', status]">
    <!-- 卡片头部 -->
    <div class="task-card-header" @click="status !== 'running' && emit('toggle')">
      <span class="task-card-icon">{{ cardIcon }}</span>
      <span class="task-card-title">{{ cardTitle }}</span>
      <span v-if="previewText" class="task-card-preview">{{ previewText }}</span>
      <Tag v-if="status === 'running'" color="processing" class="task-card-tag">
        <LoadingOutlined /> 执行中
      </Tag>
      <Tag v-else-if="status === 'done'" color="success" class="task-card-tag">完成</Tag>
      <Tag v-else-if="status === 'error'" color="error" class="task-card-tag">失败</Tag>
      <span v-if="status !== 'running'" class="collapse-indicator">
        {{ expanded ? '▼' : '▶' }}
      </span>
    </div>

    <!-- 展开后显示详情 -->
    <div v-if="expanded" class="task-card-details">
      <!-- 创建类：显示 subject / description / depends_on -->
      <template v-if="isCreateAction">
        <div v-if="subject" class="detail-row">
          <span class="detail-label">任务标题</span>
          <span class="detail-value">{{ subject }}</span>
        </div>
        <div v-if="args.description" class="detail-row">
          <span class="detail-label">详细描述</span>
          <span class="detail-value">{{ args.description }}</span>
        </div>
        <div v-if="dependsOn" class="detail-row">
          <span class="detail-label">依赖任务</span>
          <span class="detail-value">{{ dependsOn }}</span>
        </div>
      </template>

      <!-- 列表/进度类：渲染任务快照 -->
      <template v-else-if="isListAction && taskList">
        <div class="task-snapshot-list">
          <div
            v-for="t in taskList"
            :key="t.id"
            :class="['task-snapshot-item', t.status]"
          >
            <span class="snapshot-icon">{{ statusIcons[t.status] || '❓' }}</span>
            <span class="snapshot-id">[{{ t.id }}]</span>
            <span class="snapshot-subject">{{ t.subject }}</span>
            <span class="snapshot-status">({{ t.status }})</span>
            <span v-if="t.blocked_by.length" class="snapshot-blocked">
              ← 等待: {{ t.blocked_by.join(', ') }}
            </span>
          </div>
        </div>
      </template>

      <!-- 其他动作：显示入参和原始结果 -->
      <template v-else>
        <div v-if="taskId" class="detail-row">
          <span class="detail-label">任务ID</span>
          <span class="detail-value">{{ taskId }}</span>
        </div>
        <div v-if="args.notes" class="detail-row">
          <span class="detail-label">备注</span>
          <span class="detail-value">{{ args.notes }}</span>
        </div>
      </template>

      <!-- 原始结果文本（折叠区底部，用于调试） -->
      <div v-if="result && !isListAction" class="detail-result">
        <div class="detail-label">结果</div>
        <pre class="detail-result-content">{{ result.length > 500 ? result.slice(0, 500) + '...' : result }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.task-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  transition: all 0.2s ease;
  margin-top: 8px;
}

/* 执行中 - 龙虾红主题 */
.task-card.running {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.task-card.running .task-card-icon,
.task-card.running .task-card-title {
  color: var(--color-primary);
}

/* 完成状态 */
.task-card.done {
  border-color: var(--color-border);
  background: var(--color-surface);
}

/* 失败状态 */
.task-card.error {
  border-color: var(--color-primary);
  background: #fff1f0;
}

.task-card.error .task-card-icon,
.task-card.error .task-card-title {
  color: var(--color-primary);
}

.task-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.task-card-header:hover {
  opacity: 0.8;
}

.task-card-icon {
  font-size: 14px;
  line-height: 1;
}

.task-card-title {
  font-weight: 500;
  color: var(--color-text);
  flex-shrink: 0;
}

.task-card-preview {
  color: var(--color-text-secondary);
  font-size: 12px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-card-tag {
  font-size: 11px;
  padding: 0 6px;
  line-height: 18px;
  border-radius: 4px;
}

.collapse-indicator {
  font-size: 10px;
  color: var(--color-text-secondary);
  margin-left: auto;
  transition: transform 0.2s ease;
}

.task-card-details {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border);
}

.detail-row {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
  align-items: flex-start;
}

.detail-label {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-weight: 500;
  flex-shrink: 0;
  min-width: 60px;
}

.detail-value {
  font-size: 12px;
  color: var(--color-text);
  word-break: break-word;
}

/* 任务快照列表 */
.task-snapshot-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.task-snapshot-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  background: rgba(0, 0, 0, 0.02);
}

.task-snapshot-item.completed {
  opacity: 0.6;
}

.task-snapshot-item.failed {
  color: var(--color-primary);
}

.snapshot-icon {
  font-size: 13px;
}

.snapshot-id {
  font-family: ui-monospace, 'SF Mono', Monaco, monospace;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.snapshot-subject {
  flex: 1;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.snapshot-status {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.snapshot-blocked {
  font-size: 11px;
  color: var(--color-primary);
}

.detail-result {
  margin-top: 8px;
}

.detail-result-content {
  margin: 4px 0 0;
  padding: 8px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 4px;
  font-size: 12px;
  color: var(--color-text);
  max-height: 150px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, 'SF Mono', Monaco, 'Andale Mono', monospace;
}
</style>
