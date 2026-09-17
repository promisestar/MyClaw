<script setup lang="ts">
/**
 * 轨迹链路中的单个 span 卡片（REQUEST / RESPONSE / 工具调用）。
 *
 * 负责外壳：标签 + 图标 + 标题 + 指标 chips + 默认插槽（正文）。
 * 正文区默认可滚动（不再裁切）；当内容超出折叠高度且 expandable 为真时，
 * 额外提供展开/收起按钮放大视口（展开后仍可滚动，避免超长内容撑爆页面）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    label: string
    tone?: 'request' | 'response' | 'tool' | 'plain'
    icon?: string
    title?: string
    chips?: string[]
    status?: 'ok' | 'error' | 'warn'
    /** 无插槽内容时的占位文案（用于旧日志缺字段的降级展示） */
    placeholder?: string
    /** 是否允许展开/收起（仅当内容溢出时才出现按钮） */
    expandable?: boolean
    /** 折叠态正文最大高度（px） */
    collapsedHeight?: number
    /** 展开态正文最大高度（px） */
    expandedHeight?: number
  }>(),
  {
    tone: 'plain',
    icon: '',
    title: '',
    chips: () => [],
    status: 'ok',
    placeholder: '',
    expandable: false,
    collapsedHeight: 96,
    expandedHeight: 460,
  },
)

const expanded = ref(false)
const bodyRef = ref<HTMLElement | null>(null)
/** 内容是否超出折叠高度（决定是否显示展开按钮） */
const overflowing = ref(false)

const measure = () => {
  const el = bodyRef.value
  if (!el) return
  overflowing.value = el.scrollHeight > props.collapsedHeight + 2
}

let observer: ResizeObserver | null = null

onMounted(() => {
  if (!props.expandable) return
  const el = bodyRef.value
  if (!el) return
  measure()
  if (typeof ResizeObserver !== 'undefined') {
    // 内容或卡片宽度变化时重新评估是否还需要展开按钮
    observer = new ResizeObserver(() => measure())
    observer.observe(el)
  }
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

watch(
  () => props.expandable,
  () => nextTick(measure),
)

const toggle = () => {
  expanded.value = !expanded.value
  nextTick(measure)
}

const bodyStyle = () => ({
  maxHeight: `${expanded.value ? props.expandedHeight : props.collapsedHeight}px`,
})
</script>

<template>
  <div class="span-card" :class="[`tone-${tone}`, `status-${status}`, { expanded }]">
    <div class="span-card-head">
      <span v-if="icon" class="span-card-icon">{{ icon }}</span>
      <span class="span-card-label">{{ label }}</span>
      <span v-if="title" class="span-card-title" :title="title">{{ title }}</span>
    </div>

    <div v-if="chips.length" class="span-card-chips">
      <span v-for="chip in chips" :key="chip" class="span-chip">{{ chip }}</span>
    </div>

    <div v-if="$slots.default" class="span-card-body-wrapper">
      <div ref="bodyRef" class="span-card-body" :style="bodyStyle()">
        <slot />
      </div>
      <button
        v-if="expandable && overflowing"
        type="button"
        class="span-card-toggle"
        @click="toggle"
      >
        {{ expanded ? '收起 ▲' : '展开 ▼' }}
      </button>
    </div>
    <div v-else-if="placeholder" class="span-card-body span-card-placeholder">
      {{ placeholder }}
    </div>
  </div>
</template>

<style scoped>
.span-card {
  min-width: 168px;
  max-width: 280px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-text-tertiary);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 0 0 auto;
}

.tone-request {
  border-left-color: #1677ff;
}

.tone-response {
  border-left-color: #722ed1;
}

.tone-tool {
  border-left-color: #52c41a;
  /* 工具结果可能较长：放宽卡片，长 JSON 行交给横向滚动 */
  max-width: 460px;
}

.tone-tool.expanded {
  width: min(680px, 100%);
  max-width: min(680px, 100%);
}

.status-error {
  background: rgba(255, 77, 79, 0.06);
  border-color: rgba(255, 77, 79, 0.35);
}

.status-warn {
  background: rgba(250, 173, 20, 0.08);
}

.span-card-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}

.span-card-icon {
  font-size: 13px;
  line-height: 1;
}

.span-card-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.4px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.span-card-title {
  font-size: 12px;
  color: var(--color-text);
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.span-card-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.span-chip {
  font-size: 11px;
  line-height: 16px;
  padding: 0 6px;
  border-radius: 3px;
  background: var(--surface-1);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.span-card-body-wrapper {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.span-card-body {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  word-break: break-word;
  max-height: 96px;
  /* 关键：内容超出时滚动查看，而不是被裁掉 */
  overflow: auto;
  overscroll-behavior: contain;
  min-width: 0;
}

.span-card-toggle {
  align-self: flex-start;
  padding: 0;
  border: none;
  background: transparent;
  font-size: 11px;
  line-height: 16px;
  color: var(--color-primary);
  cursor: pointer;
}

.span-card-toggle:hover {
  text-decoration: underline;
}

/* 旧日志缺字段时的占位说明，避免出现空白卡片 */
.span-card-placeholder {
  color: var(--color-text-tertiary);
  font-style: italic;
}
</style>
