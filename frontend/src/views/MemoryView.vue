<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Card, Input, Tag, Empty, message, Spin, Tooltip, Button } from 'ant-design-vue'
import {
  SearchOutlined,
  ReloadOutlined,
  ClockCircleOutlined,
  TagOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons-vue'
import { memoryApi, type MemoryEntry, type MemoryCategory } from '@/api/memory'

// ── category config ──
const CATEGORIES: { key: MemoryCategory; label: string; color: string; icon: string }[] = [
  { key: 'preference',    label: '偏好',     color: '#fa8c16', icon: '❤️' },
  { key: 'decision',      label: '决策',     color: '#1890ff', icon: '🧭' },
  { key: 'entity',        label: '实体',     color: '#52c41a', icon: '👤' },
  { key: 'fact',          label: '事实',     color: '#722ed1', icon: '📌' },
  { key: 'plan',          label: '计划',     color: '#13c2c2', icon: '📋' },
  { key: 'relationship',  label: '关系',     color: '#eb2f96', icon: '🤝' },
  { key: 'reference',     label: '引用',     color: '#fa541c', icon: '🔗' },
  { key: 'rule',          label: '规则',     color: '#2f54eb', icon: '⚖️' },
]

const colorMap = Object.fromEntries(CATEGORIES.map(c => [c.key, c.color]))
const labelMap = Object.fromEntries(CATEGORIES.map(c => [c.key, c.label]))
const iconMap  = Object.fromEntries(CATEGORIES.map(c => [c.key, c.icon]))

// ── state ──
const loading = ref(false)
const memories = ref<MemoryEntry[]>([])
const total = ref(0)
const stats = ref<Record<string, number>>({})
const keyword = ref('')
const activeCategory = ref<string | null>(null)
const selectedMemory = ref<MemoryEntry | null>(null)

// ── filtered ──
const filteredMemories = computed(() => {
  let list = memories.value
  if (activeCategory.value) {
    list = list.filter(m => m.category === activeCategory.value)
  }
  if (keyword.value.trim()) {
    const kw = keyword.value.trim().toLowerCase()
    list = list.filter(m => m.content.toLowerCase().includes(kw))
  }
  return list
})

// ── API ──
const loadData = async () => {
  loading.value = true
  try {
    const [listRes, statsRes] = await Promise.all([
      memoryApi.list({ top_k: 200 }),
      memoryApi.stats(),
    ])
    memories.value = listRes.memories
    total.value = listRes.total
    stats.value = statsRes.categories
  } catch {
    message.error('加载记忆列表失败')
  } finally {
    loading.value = false
  }
}

const search = async () => {
  loading.value = true
  try {
    const kw = keyword.value.trim()
    const params: Record<string, unknown> = { top_k: 200 }
    if (kw) params.keyword = kw
    if (activeCategory.value) params.category = activeCategory.value

    const res = await memoryApi.list(params as any)
    memories.value = res.memories
    total.value = res.total
  } catch {
    message.error('搜索失败')
  } finally {
    loading.value = false
  }
}

const selectMemory = (m: MemoryEntry) => {
  selectedMemory.value = m
}

const selectCategory = (cat: string | null) => {
  activeCategory.value = activeCategory.value === cat ? null : cat
  search()
}

// ── format ──
const fmtTime = (ts: number) => {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const fmtRelative = (ts: number) => {
  if (!ts) return ''
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 604800) return `${Math.floor(diff / 86400)} 天前`
  return fmtTime(ts)
}

onMounted(loadData)
</script>

<template>
  <div class="memory-view">
    <!-- header -->
    <div class="memory-header">
      <div>
        <h1>🧠 工作记忆</h1>
        <p>
          共 {{ total }} 条记忆 ·
          {{ Object.values(stats).reduce((a, b) => a + (b || 0), 0) || total }} 条已分类
        </p>
      </div>
      <div class="header-actions">
        <Input
          v-model:value="keyword"
          placeholder="搜索记忆内容…"
          size="middle"
          allow-clear
          style="width: 260px"
          @press-enter="search"
          @clear="search"
        >
          <template #prefix><SearchOutlined style="color: #999" /></template>
        </Input>
        <Tooltip title="刷新">
          <Button size="middle" :loading="loading" @click="loadData">
            <template #icon><ReloadOutlined /></template>
          </Button>
        </Tooltip>
      </div>
    </div>

    <!-- body -->
    <div class="memory-body">
      <!-- left: category sidebar -->
      <div class="memory-sidebar">
        <Card size="small" class="sidebar-card">
          <template #title><TagOutlined /> 分类</template>
          <div class="cat-list">
            <div
              :class="['cat-item', { active: activeCategory === null }]"
              @click="selectCategory(null)"
            >
              <span class="cat-label">全部</span>
              <span class="cat-count">{{ total }}</span>
            </div>
            <div
              v-for="cat in CATEGORIES"
              :key="cat.key"
              :class="['cat-item', { active: activeCategory === cat.key }]"
              @click="selectCategory(cat.key)"
            >
              <span class="cat-label">
                <span class="cat-icon">{{ cat.icon }}</span>
                {{ cat.label }}
              </span>
              <Tag :color="cat.color" class="cat-count-tag">
                {{ stats[cat.key] || 0 }}
              </Tag>
            </div>
          </div>
        </Card>
      </div>

      <!-- center: memory list -->
      <div class="memory-list">
        <Card size="small" class="list-card" :body-style="{ padding: 0 }">
          <Spin :spinning="loading" tip="加载中…">
            <Empty v-if="!loading && filteredMemories.length === 0" description="暂无记忆" style="padding: 48px 0" />
            <div
              v-for="m in filteredMemories"
              :key="m.id"
              :class="['memory-item', { selected: selectedMemory?.id === m.id }]"
              @click="selectMemory(m)"
            >
              <div class="item-top">
                <Tag :color="colorMap[m.category] || '#999'">
                  {{ iconMap[m.category] || '📝' }}&nbsp;{{ labelMap[m.category] || m.category }}
                </Tag>
                <span class="item-time">
                  <ClockCircleOutlined style="margin-right: 4px" />
                  {{ fmtRelative(m.timestamp) }}
                </span>
              </div>
              <div class="item-content">{{ m.content.length > 120 ? m.content.slice(0, 120) + '…' : m.content }}</div>
            </div>
          </Spin>
        </Card>
      </div>

      <!-- right: detail -->
      <div class="memory-detail">
        <Card v-if="selectedMemory" size="small" class="detail-card">
          <template #title>
            <div class="detail-header">
              <Tag :color="colorMap[selectedMemory.category] || '#999'">
                {{ iconMap[selectedMemory.category] || '📝' }}&nbsp;{{ labelMap[selectedMemory.category] || selectedMemory.category }}
              </Tag>
              <span class="detail-time">
                <ClockCircleOutlined /> {{ fmtTime(selectedMemory.timestamp) }}
              </span>
            </div>
          </template>
          <div class="detail-body">
            <div v-if="selectedMemory.source" class="detail-source">
              <InfoCircleOutlined /> 来源：{{ selectedMemory.source }}
            </div>
            <div class="detail-text">{{ selectedMemory.content }}</div>
          </div>
        </Card>
        <Card v-else size="small" class="detail-card empty-detail">
          <Empty description="点击左侧记忆查看详情" :image-style="{ height: '64px' }" />
        </Card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.memory-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 20px 24px;
  box-sizing: border-box;
  background: var(--color-background, #f5f6f8);
}

.memory-header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.memory-header h1 {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.memory-header p {
  margin: 0;
  font-size: 13px;
  color: #999;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ── body ── */
.memory-body {
  flex: 1;
  display: flex;
  gap: 16px;
  min-height: 0;
  overflow: hidden;
}

/* ── sidebar ── */
.memory-sidebar {
  width: 180px;
  flex-shrink: 0;
}

.sidebar-card {
  height: 100%;
}

.sidebar-card :deep(.ant-card-body) {
  overflow-y: auto;
}

.cat-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cat-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: background .15s;
  font-size: 13px;
}

.cat-item:hover {
  background: #f0f0f0;
}

.cat-item.active {
  background: #e6f4ff;
  font-weight: 600;
}

.cat-label {
  color: #333;
  display: flex;
  align-items: center;
  gap: 4px;
}

.cat-icon {
  font-size: 14px;
}

.cat-count {
  font-size: 12px;
  color: #999;
  font-weight: 500;
}

.cat-count-tag {
  font-size: 11px !important;
  line-height: 16px !important;
  padding: 0 6px !important;
}

/* ── list ── */
.memory-list {
  flex: 1;
  min-width: 300px;
  max-width: 440px;
}

.list-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.list-card :deep(.ant-card-body) {
  flex: 1;
  overflow-y: auto;
  padding: 0;
}

.list-card :deep(.ant-spin-nested-loading),
.list-card :deep(.ant-spin-container) {
  height: 100%;
}

.memory-item {
  padding: 14px 16px;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
  transition: background .15s;
}

.memory-item:hover {
  background: #fafafa;
}

.memory-item.selected {
  background: #e6f4ff;
  border-left: 3px solid #1890ff;
}

.item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.item-time {
  font-size: 12px;
  color: #bbb;
  white-space: nowrap;
}

.item-content {
  font-size: 13px;
  color: #555;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ── detail ── */
.memory-detail {
  flex: 1;
  min-width: 300px;
}

.detail-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.detail-card :deep(.ant-card-body) {
  flex: 1;
  overflow-y: auto;
}

.empty-detail {
  display: flex;
  align-items: center;
  justify-content: center;
}

.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.detail-time {
  font-size: 13px;
  color: #999;
  font-weight: 400;
}

.detail-body {
  height: 100%;
}

.detail-source {
  font-size: 12px;
  color: #999;
  margin-bottom: 12px;
  padding: 6px 10px;
  background: #f5f5f5;
  border-radius: 4px;
}

.detail-text {
  font-size: 14px;
  line-height: 1.75;
  color: #333;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
