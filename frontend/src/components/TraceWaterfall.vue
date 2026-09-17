<script setup lang="ts">
/**
 * 轨迹时间瀑布条（Gantt）：按 span 起止相对位置横向铺开，
 * 直观看出每个模型调用内部各工具调用的耗时占比与是否并行。
 */
import { computed } from 'vue'
import { formatDuration, parseTs, type WaterfallItem } from '@/utils/traceFormat'

const props = defineProps<{ items: WaterfallItem[] }>()

const origin = computed(() => {
  const starts = props.items.map((item) => parseTs(item.start_ts)).filter((v) => v > 0)
  return starts.length ? Math.min(...starts) : 0
})

const span = computed(() => {
  const starts = props.items.map((item) => parseTs(item.start_ts)).filter((v) => v > 0)
  const ends = props.items.map((item) => parseTs(item.end_ts)).filter((v) => v > 0)
  if (!starts.length || !ends.length) return 0
  return Math.max(...ends) - Math.min(...starts)
})

interface Row extends WaterfallItem {
  left: number
  width: number
}

const rows = computed<Row[]>(() => {
  const total = Math.max(span.value, 1)
  const base = origin.value
  return props.items.map((item) => {
    const start = parseTs(item.start_ts) || base
    const end = parseTs(item.end_ts) || start
    const left = Math.min(((start - base) / total) * 100, 99)
    const width = Math.max(((end - start) / total) * 100, 1)
    return { ...item, left, width: Math.min(width, 100 - left) }
  })
})
</script>

<template>
  <div v-if="items.length > 1" class="waterfall">
    <div v-for="row in rows" :key="row.key" class="waterfall-row">
      <span class="waterfall-label" :title="row.title">{{ row.title }}</span>
      <div class="waterfall-track">
        <div
          class="waterfall-bar"
          :class="[`bar-${row.kind}`, row.status === 'error' ? 'bar-error' : '']"
          :style="{ left: `${row.left}%`, width: `${row.width}%` }"
          :title="`${row.title} · ${formatDuration(row.duration_ms)}`"
        />
      </div>
      <span class="waterfall-duration">{{ formatDuration(row.duration_ms) }}</span>
    </div>
  </div>
</template>

<style scoped>
.waterfall {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 0 8px;
  border-bottom: 1px dashed var(--color-border-light);
  margin-bottom: 8px;
}

.waterfall-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.waterfall-label {
  width: 132px;
  flex: 0 0 132px;
  font-size: 11px;
  color: var(--color-text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.waterfall-track {
  position: relative;
  flex: 1;
  height: 8px;
  border-radius: 4px;
  background: var(--surface-1);
  overflow: hidden;
}

.waterfall-bar {
  position: absolute;
  top: 0;
  height: 100%;
  border-radius: 4px;
  min-width: 2px;
}

.bar-llm {
  background: linear-gradient(90deg, #1677ff, #69b1ff);
}

.bar-tool {
  background: linear-gradient(90deg, #52c41a, #95de64);
}

.bar-side {
  background: linear-gradient(90deg, #8c8c8c, #bfbfbf);
}

.bar-error {
  background: linear-gradient(90deg, #ff4d4f, #ff7875);
}

.waterfall-duration {
  width: 56px;
  flex: 0 0 56px;
  text-align: right;
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  font-size: 11px;
  color: var(--color-text-tertiary);
}
</style>
