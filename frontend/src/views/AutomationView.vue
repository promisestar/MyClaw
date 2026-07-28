<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import {
  Card, Button, Empty, message, Popconfirm, Modal, Input, Tag, Tooltip,
  Table, Switch, Select, InputNumber,
} from 'ant-design-vue'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, ReloadOutlined,
  ClockCircleOutlined, HistoryOutlined, CheckCircleOutlined,
  CloseCircleOutlined, ScheduleOutlined,
} from '@ant-design/icons-vue'
import {
  automationApi,
  type AutomationTask, type AutomationRun,
  type CreateAutomationPayload,
} from '@/api/automation'
import dayjs from 'dayjs'

// 任务列表
const tasks = ref<AutomationTask[]>([])
const loading = ref(false)

// 创建/编辑弹窗
const editorOpen = ref(false)
const editorLoading = ref(false)
const editingId = ref<string | null>(null)
const form = ref({
  name: '',
  prompt: '',
  schedule_type: 'once' as 'once' | 'interval' | 'rrule',
  // once
  once_datetime: '',
  // interval
  interval_seconds: 3600,
  // rrule
  rrule: 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
  webhook_url: '',
  enabled: true,
  max_runs: null as number | null,
  max_duration_minutes: null as number | null,
})

// 运行历史弹窗
const runsOpen = ref(false)
const runsLoading = false
const runs = ref<AutomationRun[]>([])
const runsTaskName = ref('')

// 启停 loading
const toggleLoading = ref<string | null>(null)

const loadTasks = async () => {
  loading.value = true
  try {
    tasks.value = await automationApi.list()
  } catch {
    message.error('加载定时任务列表失败')
  } finally {
    loading.value = false
  }
}

const openCreate = () => {
  editingId.value = null
  form.value = {
    name: '',
    prompt: '',
    schedule_type: 'once',
    once_datetime: dayjs().add(1, 'hour').format('YYYY-MM-DDTHH:mm'),
    interval_seconds: 3600,
    rrule: 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
    webhook_url: '',
    enabled: true,
    max_runs: null,
    max_duration_minutes: null,
  }
  editorOpen.value = true
}

const openEdit = (task: AutomationTask) => {
  editingId.value = task.id
  form.value = {
    name: task.name,
    prompt: task.prompt,
    schedule_type: task.schedule_type,
    once_datetime: (task.schedule_config.datetime as string) || dayjs().add(1, 'hour').format('YYYY-MM-DDTHH:mm'),
    interval_seconds: (task.schedule_config.interval_seconds as number) || 3600,
    rrule: (task.schedule_config.rrule as string) || 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
    webhook_url: task.webhook_url || '',
    enabled: task.enabled,
    max_runs: task.max_runs,
    max_duration_minutes: task.max_duration_minutes,
  }
  editorOpen.value = true
}

const buildScheduleConfig = (): Record<string, unknown> => {
  const type = form.value.schedule_type
  if (type === 'once') {
    // 转为带时区的 ISO 字符串（本地时区）
    const dt = dayjs(form.value.once_datetime)
    return { datetime: dt.isValid() ? dt.toISOString() : '' }
  }
  if (type === 'interval') {
    return { interval_seconds: form.value.interval_seconds }
  }
  if (type === 'rrule') {
    return { rrule: form.value.rrule }
  }
  return {}
}

const saveTask = async () => {
  if (!form.value.name.trim()) {
    message.warning('请输入任务名称')
    return
  }
  if (!form.value.prompt.trim()) {
    message.warning('请输入任务 prompt')
    return
  }

  const scheduleConfig = buildScheduleConfig()
  if (form.value.schedule_type === 'once' && !scheduleConfig.datetime) {
    message.warning('请输入有效的执行时间')
    return
  }

  editorLoading.value = true
  const payload: CreateAutomationPayload = {
    name: form.value.name.trim(),
    prompt: form.value.prompt.trim(),
    schedule_type: form.value.schedule_type,
    schedule_config: scheduleConfig,
    webhook_url: form.value.webhook_url.trim() || null,
    enabled: form.value.enabled,
    max_runs: form.value.max_runs,
    max_duration_minutes: form.value.max_duration_minutes,
  }

  try {
    if (editingId.value) {
      await automationApi.update(editingId.value, payload)
      message.success('任务已更新')
    } else {
      await automationApi.create(payload)
      message.success('任务已创建')
    }
    editorOpen.value = false
    await loadTasks()
  } catch (error) {
    const e = error as { response?: { data?: { detail?: unknown } } }
    const detail = e.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : '保存任务失败'
    message.error(msg)
  } finally {
    editorLoading.value = false
  }
}

const toggleEnabled = async (task: AutomationTask) => {
  toggleLoading.value = task.id
  try {
    if (task.enabled) {
      await automationApi.disable(task.id)
    } else {
      await automationApi.enable(task.id)
    }
    await loadTasks()
  } catch {
    message.error('操作失败')
  } finally {
    toggleLoading.value = null
  }
}

const deleteTask = async (taskId: string) => {
  try {
    await automationApi.delete(taskId)
    message.success('任务已删除')
    await loadTasks()
  } catch {
    message.error('删除失败')
  }
}

const openRuns = async (task: AutomationTask) => {
  runsOpen.value = true
  runsTaskName.value = task.name
  runs.value = []
  try {
    runs.value = await automationApi.runs(task.id, 20)
  } catch {
    message.error('加载运行历史失败')
  }
}

const formatDate = (isoStr: string | null) => {
  if (!isoStr) return '—'
  return dayjs(isoStr).format('YYYY-MM-DD HH:mm:ss')
}

const scheduleTypeText = (type: string) => {
  const map: Record<string, string> = { once: '一次性', interval: '间隔', rrule: 'RRULE' }
  return map[type] || type
}

const scheduleTypeColor = (type: string) => {
  const map: Record<string, string> = { once: 'blue', interval: 'green', rrule: 'purple' }
  return map[type] || 'default'
}

const describeSchedule = (task: AutomationTask) => {
  if (task.schedule_type === 'once') {
    return formatDate(task.schedule_config.datetime as string)
  }
  if (task.schedule_type === 'interval') {
    const s = task.schedule_config.interval_seconds as number
    if (s >= 86400) return `每 ${Math.floor(s / 86400)} 天`
    if (s >= 3600) return `每 ${Math.floor(s / 3600)} 小时`
    if (s >= 60) return `每 ${Math.floor(s / 60)} 分钟`
    return `每 ${s} 秒`
  }
  if (task.schedule_type === 'rrule') {
    return task.schedule_config.rrule as string
  }
  return '—'
}

const taskColumns = [
  { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
  { title: '调度', key: 'schedule', width: 200 },
  { title: '状态', key: 'enabled', width: 80 },
  { title: '运行次数', dataIndex: 'run_count', key: 'run_count', width: 90 },
  { title: '上次运行', dataIndex: 'last_run_at', key: 'last_run_at', width: 170 },
  { title: '下次运行', dataIndex: 'next_run_at', key: 'next_run_at', width: 170 },
  { title: '操作', key: 'action', width: 140, fixed: 'right' as const },
]

const runColumns = [
  { title: '结果', key: 'success', width: 70 },
  { title: '开始时间', dataIndex: 'started_at', key: 'started_at', width: 170 },
  { title: '结束时间', dataIndex: 'finished_at', key: 'finished_at', width: 170 },
  { title: '结果摘要', dataIndex: 'result', key: 'result', ellipsis: true },
]

onMounted(() => {
  loadTasks()
})
</script>

<template>
  <div class="automation-view">
    <div class="automation-header">
      <div>
        <h1>定时任务</h1>
        <p>创建和管理自动化任务，支持一次性、间隔循环和 RRULE 规则调度</p>
      </div>
      <div class="header-actions">
        <Button :icon="h(ReloadOutlined)" @click="loadTasks">刷新</Button>
        <Button type="primary" :icon="h(PlusOutlined)" @click="openCreate">新建任务</Button>
      </div>
    </div>

    <Card class="automation-card">
      <Empty
        v-if="tasks.length === 0 && !loading"
        description="暂无定时任务"
      >
        <Button type="primary" :icon="h(PlusOutlined)" @click="openCreate">创建第一个任务</Button>
      </Empty>

      <Table
        v-else
        :columns="taskColumns"
        :data-source="tasks"
        :loading="loading"
        row-key="id"
        :pagination="false"
        size="middle"
        :scroll="{ x: 1000 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'schedule'">
            <Tag :color="scheduleTypeColor(record.schedule_type)">
              {{ scheduleTypeText(record.schedule_type) }}
            </Tag>
            <span class="schedule-desc">{{ describeSchedule(record) }}</span>
          </template>

          <template v-else-if="column.key === 'enabled'">
            <Switch
              :checked="record.enabled"
              :loading="toggleLoading === record.id"
              size="small"
              @change="toggleEnabled(record)"
            />
          </template>

          <template v-else-if="column.key === 'last_run_at'">
            {{ formatDate(record.last_run_at) }}
          </template>

          <template v-else-if="column.key === 'next_run_at'">
            {{ formatDate(record.next_run_at) }}
          </template>

          <template v-else-if="column.key === 'action'">
            <Tooltip title="运行历史">
              <Button type="text" size="small" :icon="h(HistoryOutlined)" @click="openRuns(record)" />
            </Tooltip>
            <Tooltip title="编辑">
              <Button type="text" size="small" :icon="h(EditOutlined)" @click="openEdit(record)" />
            </Tooltip>
            <Popconfirm
              title="确定删除此任务？"
              ok-text="删除"
              cancel-text="取消"
              @confirm="deleteTask(record.id)"
            >
              <Tooltip title="删除">
                <Button type="text" size="small" danger :icon="h(DeleteOutlined)" />
              </Tooltip>
            </Popconfirm>
          </template>
        </template>
      </Table>
    </Card>

    <!-- 创建/编辑弹窗 -->
    <Modal
      v-model:open="editorOpen"
      :title="editingId ? '编辑任务' : '新建任务'"
      :confirm-loading="editorLoading"
      ok-text="保存"
      cancel-text="取消"
      width="600px"
      @ok="saveTask"
    >
      <div class="editor-form">
        <div class="form-item">
          <label class="form-label">任务名称</label>
          <Input v-model:value="form.name" placeholder="如：每日新闻摘要" />
        </div>

        <div class="form-item">
          <label class="form-label">任务 Prompt</label>
          <Input.TextArea
            v-model:value="form.prompt"
            :rows="4"
            placeholder="用自然语言描述要执行的任务，如：搜索今天的科技新闻并总结成 3 条要点"
          />
        </div>

        <div class="form-item">
          <label class="form-label">调度类型</label>
          <Select v-model:value="form.schedule_type" style="width: 100%">
            <Select.Option value="once">
              <ClockCircleOutlined /> 一次性（指定时间执行一次）
            </Select.Option>
            <Select.Option value="interval">
              <ReloadOutlined /> 间隔循环（每隔 N 秒执行）
            </Select.Option>
            <Select.Option value="rrule">
              <ScheduleOutlined /> RRULE 规则（iCalendar 标准）
            </Select.Option>
          </Select>
        </div>

        <div v-if="form.schedule_type === 'once'" class="form-item">
          <label class="form-label">执行时间</label>
          <Input
            v-model:value="form.once_datetime"
            type="datetime-local"
            placeholder="2026-07-29T09:00"
          />
          <span class="form-hint">本地时区，格式：YYYY-MM-DDTHH:mm</span>
        </div>

        <div v-if="form.schedule_type === 'interval'" class="form-item">
          <label class="form-label">间隔秒数</label>
          <InputNumber
            v-model:value="form.interval_seconds"
            :min="1"
            :max="86400"
            style="width: 100%"
            addon-after="秒"
          />
          <span class="form-hint">3600 = 1 小时，86400 = 1 天</span>
        </div>

        <div v-if="form.schedule_type === 'rrule'" class="form-item">
          <label class="form-label">RRULE 规则</label>
          <Input v-model:value="form.rrule" placeholder="FREQ=DAILY;BYHOUR=9;BYMINUTE=0" />
          <span class="form-hint">
            示例：每天 9 点 = FREQ=DAILY;BYHOUR=9;BYMINUTE=0<br>
            每周一 = FREQ=WEEKLY;BYDAY=MO<br>
            每月 1 号 = FREQ=MONTHLY;BYMONTHDAY=1
          </span>
        </div>

        <div class="form-item">
          <label class="form-label">Webhook URL（可选）</label>
          <Input v-model:value="form.webhook_url" placeholder="https://example.com/webhook" />
          <span class="form-hint">任务执行后将结果 POST 到此地址</span>
        </div>

        <div class="form-row">
          <div class="form-item form-item-half">
            <label class="form-label">最大运行次数</label>
            <InputNumber
              v-model:value="form.max_runs"
              :min="1"
              style="width: 100%"
              placeholder="不限"
            />
          </div>
          <div class="form-item form-item-half">
            <label class="form-label">单次最大时长（分钟）</label>
            <InputNumber
              v-model:value="form.max_duration_minutes"
              :min="1"
              style="width: 100%"
              placeholder="不限"
            />
          </div>
        </div>

        <div class="form-item">
          <label class="form-label">启用状态</label>
          <Switch v-model:checked="form.enabled" />
        </div>
      </div>
    </Modal>

    <!-- 运行历史弹窗 -->
    <Modal
      v-model:open="runsOpen"
      :title="`运行历史 - ${runsTaskName}`"
      :footer="null"
      width="800px"
    >
      <Empty v-if="runs.length === 0 && !runsLoading" description="暂无运行记录" />
      <Table
        v-else
        :columns="runColumns"
        :data-source="runs"
        row-key="run_id"
        :pagination="{ pageSize: 10 }"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'success'">
            <CheckCircleOutlined v-if="record.success" style="color: #52c41a" />
            <CloseCircleOutlined v-else style="color: #ff4d4f" />
          </template>
          <template v-else-if="column.key === 'started_at'">
            {{ formatDate(record.started_at) }}
          </template>
          <template v-else-if="column.key === 'finished_at'">
            {{ formatDate(record.finished_at) }}
          </template>
          <template v-else-if="column.key === 'result'">
            <Tooltip :title="record.error || record.result">
              {{ record.success ? record.result : record.error }}
            </Tooltip>
          </template>
        </template>
      </Table>
    </Modal>
  </div>
</template>

<script lang="ts">
export default { name: 'AutomationView' }
</script>

<style scoped>
.automation-view {
  padding: 24px;
  height: 100%;
  overflow-y: auto;
}

.automation-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
}

.automation-header h1 {
  margin: 0 0 4px 0;
  font-size: 22px;
  font-weight: 600;
  color: var(--color-text);
}

.automation-header p {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-tertiary);
}

.header-actions {
  display: flex;
  gap: 8px;
}

.automation-card {
  border-radius: var(--radius-lg, 12px);
}

.schedule-desc {
  margin-left: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.editor-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-row {
  display: flex;
  gap: 16px;
}

.form-item-half {
  flex: 1;
}

.form-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
}

.form-hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
  line-height: 1.5;
}
</style>
