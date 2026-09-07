<script setup lang="ts">
import { ref, computed, onMounted, h } from 'vue'
import { Card, Table, Select, Progress, Empty, Spin, Tooltip, Popconfirm, message } from 'ant-design-vue'
import { ReloadOutlined, ThunderboltOutlined, DatabaseOutlined } from '@ant-design/icons-vue'
import { usageApi, type UsageSummary, type UsageBucket } from '@/api/usage'

const loading = ref(false)
const days = ref(7)
const summary = ref<UsageSummary | null>(null)

const loadSummary = async () => {
  loading.value = true
  try {
    summary.value = await usageApi.summary(days.value)
  } catch {
    message.error('加载用量统计失败')
  } finally {
    loading.value = false
  }
}

const s = computed<UsageSummary>(
  () =>
    summary.value ?? {
      calls: 0,
      calls_with_usage: 0,
      usage_coverage: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      cached_tokens: 0,
      reasoning_tokens: 0,
      cache_hit_rate: 0,
      avg_duration_ms: 0,
      by_model: [],
      by_call_site: [],
      by_day: [],
    }
)

const fmt = (n: number) => (n || 0).toLocaleString('zh-CN')
const pct = (v: number) => `${((v || 0) * 100).toFixed(1)}%`

const cacheHitPercent = computed(() => Math.round((s.value.cache_hit_rate || 0) * 1000) / 10)
const coveragePercent = computed(() => Math.round((s.value.usage_coverage || 0) * 1000) / 10)

/** 用量构成条：把四类 token 按 total 归一化成长度 */
const composition = computed(() => {
  const v = s.value
  const max = Math.max(v.prompt_tokens, v.completion_tokens, v.cached_tokens, v.reasoning_tokens, 1)
  const rows = [
    { key: 'prompt', label: '输入 Prompt', value: v.prompt_tokens, color: '#1677ff' },
    { key: 'completion', label: '输出 Completion', value: v.completion_tokens, color: '#52c41a' },
    { key: 'cached', label: '缓存命中 Cached', value: v.cached_tokens, color: '#faad14' },
    { key: 'reasoning', label: '推理 Reasoning', value: v.reasoning_tokens, color: '#722ed1' },
  ]
  return rows.map(r => ({ ...r, width: Math.round((r.value / max) * 100) }))
})

const maxDayTokens = computed(() =>
  Math.max(1, ...(s.value.by_day ?? []).map(d => d.total_tokens))
)

const bucketColumns = [
  { title: '名称', dataIndex: 'name', key: 'name', width: 220 },
  { title: '调用次数', dataIndex: 'calls', key: 'calls', width: 100, align: 'right' as const },
  { title: '总 tokens', dataIndex: 'total_tokens', key: 'total_tokens', align: 'right' as const },
  { title: '输入', dataIndex: 'prompt_tokens', key: 'prompt_tokens', align: 'right' as const },
  { title: '输出', dataIndex: 'completion_tokens', key: 'completion_tokens', align: 'right' as const },
  {
    title: '缓存命中率',
    key: 'cache_hit_rate',
    align: 'right' as const,
    customRender: ({ record }: { record: UsageBucket }) =>
      h('span', { class: record.cache_hit_rate > 0 ? 'hit-positive' : 'hit-zero' }, pct(record.cache_hit_rate)),
  },
  {
    title: '平均耗时',
    key: 'avg_duration_ms',
    align: 'right' as const,
    customRender: ({ record }: { record: UsageBucket }) => h('span', `${record.avg_duration_ms} ms`),
  },
]

const dayColumns = [
  { title: '日期', dataIndex: 'date_str', key: 'date_str', width: 120 },
  { title: '调用次数', dataIndex: 'calls', key: 'calls', width: 100, align: 'right' as const },
  { title: '总 tokens', dataIndex: 'total_tokens', key: 'total_tokens', align: 'right' as const },
  {
    title: '缓存命中率',
    key: 'cache_hit_rate',
    align: 'right' as const,
    customRender: ({ record }: { record: UsageSummary }) => h('span', pct(record.cache_hit_rate)),
  },
  {
    title: '占比',
    key: 'bar',
    customRender: ({ record }: { record: UsageSummary }) =>
      h('div', { class: 'day-bar' }, [
        h('div', {
          class: 'day-bar-fill',
          style: { width: `${(record.total_tokens / maxDayTokens.value) * 100}%` },
        }),
      ]),
  },
]

const removeToday = async () => {
  const today = new Date().toISOString().slice(0, 10)
  try {
    await usageApi.remove(today)
    message.success('已清空今日用量日志')
    await loadSummary()
  } catch {
    message.error('清空失败（文件可能不存在）')
  }
}

onMounted(loadSummary)
</script>

<template>
  <div class="usage-view">
    <div class="usage-header">
      <div>
        <h1>用量统计</h1>
        <p>LLM token 消耗与前缀缓存命中情况（数据来自 llm-usage-*.jsonl）</p>
      </div>
      <div class="header-actions">
        <Select v-model:value="days" style="width: 120px" @change="loadSummary">
          <Select.Option :value="1">今天</Select.Option>
          <Select.Option :value="7">最近 7 天</Select.Option>
          <Select.Option :value="14">最近 14 天</Select.Option>
          <Select.Option :value="30">最近 30 天</Select.Option>
        </Select>
        <button class="icon-btn" title="刷新" @click="loadSummary">
          <ReloadOutlined />
        </button>
      </div>
    </div>

    <Spin :spinning="loading" tip="加载中...">
      <div class="usage-content">
        <!-- 顶部指标卡 -->
        <div class="stat-grid">
          <Card class="stat-card">
            <div class="stat-label">总 tokens</div>
            <div class="stat-value">{{ fmt(s.total_tokens) }}</div>
            <div class="stat-sub">{{ s.calls }} 次 LLM 调用</div>
          </Card>
          <Card class="stat-card">
            <div class="stat-label">
              <DatabaseOutlined /> 缓存命中率
            </div>
            <div class="stat-value">{{ pct(s.cache_hit_rate) }}</div>
            <Progress
              :percent="cacheHitPercent"
              :show-info="false"
              stroke-color="#faad14"
              size="small"
            />
            <div class="stat-sub">{{ fmt(s.cached_tokens) }} / {{ fmt(s.prompt_tokens) }} 输入 token</div>
          </Card>
          <Card class="stat-card">
            <div class="stat-label">
              <ThunderboltOutlined /> 平均延迟
            </div>
            <div class="stat-value">{{ s.avg_duration_ms }}<span class="unit">ms</span></div>
            <div class="stat-sub">单次 LLM 调用</div>
          </Card>
          <Card class="stat-card">
            <Tooltip
              title="成功拿到 usage 的调用占比。低于 100% 说明仍有调用点未采集到 token（多为厂商流式未返回 usage）"
            >
              <div class="stat-label">用量覆盖率</div>
            </Tooltip>
            <div class="stat-value" :class="{ 'value-warn': coveragePercent < 100 }">
              {{ pct(s.usage_coverage) }}
            </div>
            <div class="stat-sub">{{ s.calls_with_usage }} / {{ s.calls }} 次有数据</div>
          </Card>
        </div>

        <!-- 用量构成 -->
        <Card title="用量构成" class="section-card">
          <div v-for="row in composition" :key="row.key" class="comp-row">
            <span class="comp-label">{{ row.label }}</span>
            <div class="comp-track">
              <div class="comp-fill" :style="{ width: row.width + '%', background: row.color }" />
            </div>
            <span class="comp-value">{{ fmt(row.value) }}</span>
          </div>
          <p v-if="s.total_tokens === 0" class="empty-hint">
            暂无数据。发起一次对话后即可看到 token 消耗与缓存命中情况。
          </p>
        </Card>

        <!-- 按模型 -->
        <Card title="按模型" class="section-card">
          <Empty v-if="!s.by_model.length" description="暂无数据" />
          <Table
            v-else
            :columns="bucketColumns"
            :data-source="s.by_model"
            :pagination="false"
            row-key="name"
            size="small"
          />
        </Card>

        <!-- 按调用点 -->
        <Card title="按调用点" class="section-card">
          <Empty v-if="!s.by_call_site.length" description="暂无数据" />
          <Table
            v-else
            :columns="bucketColumns"
            :data-source="s.by_call_site"
            :pagination="false"
            row-key="name"
            size="small"
          />
        </Card>

        <!-- 按天趋势 -->
        <Card title="按天趋势" class="section-card">
          <Empty v-if="!s.by_day || !s.by_day.length" description="暂无数据" />
          <Table
            v-else
            :columns="dayColumns"
            :data-source="s.by_day"
            :pagination="false"
            row-key="date_str"
            size="small"
          />
        </Card>

        <div class="footer-actions">
          <Popconfirm
            title="清空今日用量日志？"
            description="仅删除今天的记录，不可恢复"
            ok-text="清空"
            cancel-text="取消"
            ok-type="danger"
            @confirm="removeToday"
          >
            <button class="danger-btn">清空今日记录</button>
          </Popconfirm>
        </div>
      </div>
    </Spin>
  </div>
</template>

<style scoped>
.usage-view {
  min-height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 24px;
  box-sizing: border-box;
}

.usage-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
  gap: 16px;
}

.usage-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
}

.usage-header p {
  margin: 0;
  color: #999;
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
  border: 1px solid #d9d9d9;
  border-radius: 6px;
  background: #fff;
  color: #333;
  cursor: pointer;
  transition: all 0.2s ease;
}

.icon-btn:hover {
  color: #1677ff;
  border-color: #1677ff;
}

.usage-content {
  flex: 1;
  overflow-y: auto;
  max-width: 900px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}

.stat-card {
  border-radius: 8px;
}

.stat-label {
  color: #8c8c8c;
  font-size: 13px;
  margin-bottom: 6px;
}

.stat-value {
  font-size: 26px;
  font-weight: 600;
  color: #262626;
  line-height: 1.2;
}

.stat-value .unit {
  font-size: 13px;
  font-weight: 400;
  color: #999;
  margin-left: 2px;
}

.value-warn {
  color: #faad14;
}

.stat-sub {
  margin-top: 6px;
  color: #bfbfbf;
  font-size: 12px;
}

.section-card {
  margin-bottom: 20px;
  border-radius: 8px;
}

.comp-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.comp-label {
  width: 150px;
  flex-shrink: 0;
  font-size: 13px;
  color: #595959;
}

.comp-track {
  flex: 1;
  height: 10px;
  background: #f5f5f5;
  border-radius: 5px;
  overflow: hidden;
}

.comp-fill {
  height: 100%;
  border-radius: 5px;
  transition: width 0.3s ease;
}

.comp-value {
  width: 100px;
  text-align: right;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  color: #262626;
}

.empty-hint {
  color: #bbb;
  font-size: 13px;
  margin: 8px 0 0;
}

.day-bar {
  width: 100%;
  height: 8px;
  background: #f5f5f5;
  border-radius: 4px;
  overflow: hidden;
}

.day-bar-fill {
  height: 100%;
  background: #1677ff;
  border-radius: 4px;
}

.hit-positive {
  color: #faad14;
  font-weight: 500;
}

.hit-zero {
  color: #d9d9d9;
}

.footer-actions {
  margin: 8px 0 32px;
}

.danger-btn {
  padding: 5px 14px;
  border: 1px solid #ffccc7;
  border-radius: 6px;
  background: #fff;
  color: #ff4d4f;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s ease;
}

.danger-btn:hover {
  background: #ff4d4f;
  color: #fff;
  border-color: #ff4d4f;
}
</style>
