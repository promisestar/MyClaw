<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { Tooltip } from 'ant-design-vue'
import { agentApi, type TaskItem } from '@/api/agent'

const props = defineProps<{
  /** 当前会话 ID */
  sessionId: string | null
  /** 是否正在加载（流式对话中）— 用于决定是否自动刷新 */
  loading: boolean
}>()

// ===== 状态 =====
const tasks = ref<TaskItem[]>([])
const collapsed = ref(false)
const refreshing = ref(false)

// ===== 计算属性 =====
const totalCount = computed(() => tasks.value.length)
const completedCount = computed(() =>
  tasks.value.filter(t => t.status === 'completed').length
)
const inProgressCount = computed(() =>
  tasks.value.filter(t => t.status === 'in_progress').length
)
const pendingCount = computed(() =>
  tasks.value.filter(t => t.status === 'pending' && t.blocked_by.length === 0).length
)
const blockedCount = computed(() =>
  tasks.value.filter(t => t.status === 'pending' && t.blocked_by.length > 0).length
)
const failedCount = computed(() =>
  tasks.value.filter(t => t.status === 'failed').length
)

const hasTasks = computed(() => totalCount.value > 0)

/** 头部摘要行 */
const summaryLine = computed(() => {
  if (!hasTasks.value) return ''
  if (completedCount.value === totalCount.value) {
    return `所有任务已完成`
  }
  const parts: string[] = []
  if (inProgressCount.value > 0) parts.push(`${inProgressCount.value} 进行中`)
  if (pendingCount.value > 0) parts.push(`${pendingCount.value} 待开始`)
  if (blockedCount.value > 0) parts.push(`${blockedCount.value} 被阻塞`)
  if (failedCount.value > 0) parts.push(`${failedCount.value} 失败`)
  return parts.join(' · ')
})

/** 进度百分比 */
const progressPercent = computed(() => {
  if (!totalCount.value) return 0
  return Math.round((completedCount.value / totalCount.value) * 100)
})

/** 进度条颜色 */
const progressColor = computed(() => {
  if (failedCount.value > 0) return '#ff4d4f'
  if (progressPercent.value === 100) return '#52c41a'
  if (progressPercent.value >= 70) return '#52c41a'
  if (progressPercent.value >= 30) return '#faad14'
  return '#1677ff'
})

/** 状态图标映射 */
const statusIcons: Record<string, string> = {
  pending: '⏳',
  in_progress: '🔄',
  completed: '✅',
  failed: '❌',
  cancelled: '🚫',
}

// ===== 数据获取 =====
/** 从后端拉取任务进度 */
const refresh = async () => {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const res = await agentApi.getTaskProgress(props.sessionId || undefined)
    tasks.value = res.tasks || []
  } catch (err) {
    // 静默失败，避免打断用户
    console.warn('[TaskPanel] 刷新任务进度失败:', err)
  } finally {
    refreshing.value = false
  }
}

/** 增量更新：从工具调用结果中解析任务列表 */
const updateFromToolResult = (toolName: string, args: Record<string, unknown>, result: string) => {
  if (toolName !== 'task') return

  // 尝试解析 result 中的 data.tasks
  let parsed: Record<string, unknown> | null = null
  try {
    parsed = JSON.parse(result)
  } catch {
    const jsonMatch = result.match(/\{[\s\S]*\}/)
    if (jsonMatch) {
      try {
        parsed = JSON.parse(jsonMatch[0])
      } catch {
        parsed = null
      }
    }
  }

  if (parsed && Array.isArray(parsed.tasks)) {
    // task_list / task_progress 返回完整列表 → 直接替换
    tasks.value = (parsed.tasks as Record<string, unknown>[]).map(t => ({
      id: String(t.id || ''),
      subject: String(t.subject || ''),
      description: String(t.description || ''),
      status: String(t.status || 'pending') as TaskItem['status'],
      blocked_by: Array.isArray(t.blocked_by) ? t.blocked_by.map(String) : [],
      blocks: Array.isArray(t.blocks) ? t.blocks.map(String) : [],
      created_at: Number(t.created_at || 0),
      started_at: t.started_at ? Number(t.started_at) : null,
      completed_at: t.completed_at ? Number(t.completed_at) : null,
      notes: String(t.notes || ''),
    }))
    return
  }

  // 单任务动作：触发后端刷新（最简单可靠）
  const action = String(args.action || '')
  if (['task_create', 'task_start', 'task_complete', 'task_fail', 'task_cancel'].includes(action)) {
    // 延迟刷新，等后端状态稳定
    setTimeout(() => refresh(), 100)
  }
}

// ===== 生命周期 =====
watch(() => props.sessionId, (newSid) => {
  if (newSid) {
    refresh()
  } else {
    tasks.value = []
  }
})

watch(() => props.loading, (isLoading, wasLoading) => {
  // 对话结束时刷新
  if (wasLoading && !isLoading) {
    refresh()
  }
})

onMounted(() => {
  if (props.sessionId) {
    refresh()
  }
})

// 暴露方法供父组件调用
defineExpose({
  refresh,
  updateFromToolResult,
})
</script>

<template>
  <Transition name="task-panel-slide">
    <div v-if="hasTasks" class="task-panel" :class="{ collapsed }">
      <!-- 头部 -->
      <div class="task-panel-header" @click="collapsed = !collapsed">
        <span class="task-panel-icon">📋</span>
        <span class="task-panel-title">任务列表</span>
        <span class="task-panel-count">{{ completedCount }}/{{ totalCount }}</span>
        <!-- 进度条 -->
        <div class="task-panel-progress">
          <div
            class="task-panel-progress-bar"
            :style="{ width: progressPercent + '%', backgroundColor: progressColor }"
          ></div>
        </div>
        <span class="task-panel-summary">{{ summaryLine }}</span>
        <Tooltip :title="collapsed ? '展开' : '折叠'">
          <span class="task-panel-toggle">{{ collapsed ? '▲' : '▼' }}</span>
        </Tooltip>
      </div>

      <!-- 任务列表（展开时显示） -->
      <div v-if="!collapsed" class="task-panel-body">
        <div
          v-for="task in tasks"
          :key="task.id"
          :class="['task-panel-item', task.status]"
        >
          <span class="task-item-icon">{{ statusIcons[task.status] || '❓' }}</span>
          <span class="task-item-id">[{{ task.id }}]</span>
          <span class="task-item-subject" :title="task.subject">{{ task.subject }}</span>
          <span v-if="task.status === 'pending' && task.blocked_by.length" class="task-item-blocked">
            ← 等待: {{ task.blocked_by.join(', ') }}
          </span>
          <span v-if="task.notes && task.status !== 'pending'" class="task-item-notes" :title="task.notes">
            {{ task.notes.length > 30 ? task.notes.slice(0, 30) + '...' : task.notes }}
          </span>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.task-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  margin-bottom: 8px;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  transition: all 0.2s ease;
}

.task-panel.collapsed {
  border-radius: 6px;
}

/* 头部 */
.task-panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s ease;
}

.task-panel-header:hover {
  background: rgba(0, 0, 0, 0.02);
}

.task-panel-icon {
  font-size: 14px;
  line-height: 1;
}

.task-panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  flex-shrink: 0;
}

.task-panel-count {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-secondary);
  font-family: ui-monospace, 'SF Mono', Monaco, monospace;
  flex-shrink: 0;
}

/* 进度条 */
.task-panel-progress {
  flex: 1;
  height: 4px;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 2px;
  overflow: hidden;
  min-width: 60px;
  max-width: 200px;
}

.task-panel-progress-bar {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s ease, background-color 0.3s ease;
}

.task-panel-summary {
  font-size: 11px;
  color: var(--color-text-secondary);
  flex-shrink: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 300px;
}

.task-panel-toggle {
  font-size: 10px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
  padding: 0 4px;
}

/* 任务列表 */
.task-panel-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px 8px 8px;
  border-top: 1px dashed var(--color-border);
  max-height: 240px;
  overflow-y: auto;
}

.task-panel-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  transition: background 0.15s ease;
}

.task-panel-item:hover {
  background: rgba(0, 0, 0, 0.03);
}

.task-panel-item.completed {
  opacity: 0.55;
}

.task-panel-item.failed .task-item-subject {
  color: var(--color-primary);
}

.task-item-icon {
  font-size: 13px;
  flex-shrink: 0;
}

.task-item-id {
  font-family: ui-monospace, 'SF Mono', Monaco, monospace;
  font-size: 11px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.task-item-subject {
  flex: 1;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-item-blocked {
  font-size: 11px;
  color: var(--color-primary);
  flex-shrink: 0;
}

.task-item-notes {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-style: italic;
  flex-shrink: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}

/* 过渡动画 */
.task-panel-slide-enter-active,
.task-panel-slide-leave-active {
  transition: all 0.25s ease;
}

.task-panel-slide-enter-from,
.task-panel-slide-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
