<script setup lang="ts">
/**
 * 工具结果渲染块。
 *
 * - JSON 结果：2 空格缩进漂亮打印 + 语法着色（键/字符串/数字/布尔/null 分色）
 * - JSON + 后续文本备注的混合结果：自动分段，「JSON 段 + 纯文本段」
 * - 非 JSON：保留原有换行的纯文本
 *
 * 内容来自本地日志（后端落盘时已截断到 2000 字符），此处仅做词法着色，
 * 全部用 <span> 渲染 token，不使用 v-html，无注入风险。
 */
import { computed } from 'vue'
import { parseToolResult, type ToolResultSegment } from '@/utils/traceFormat'

const props = defineProps<{ text?: string | null }>()

const segments = computed<ToolResultSegment[]>(() => parseToolResult(props.text))

/** 存在由「截断修复」得到的 JSON 段 → 提示用户展示的是部分内容 */
const hasRepaired = computed(() => segments.value.some((seg) => seg.repaired))
</script>

<template>
  <div class="result-block">
    <p v-if="hasRepaired" class="result-hint">
      结果被日志截断，以下为可解析的部分
    </p>
    <pre v-for="(seg, index) in segments" :key="index" class="result-seg" :class="`seg-${seg.kind}`"><span v-for="(tok, ti) in seg.tokens" :key="ti" :class="`tok-${tok.type}`">{{ tok.text }}</span></pre>
  </div>
</template>

<style scoped>
.result-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.result-seg {
  margin: 0;
  padding: 7px 8px;
  border-radius: 4px;
  background: var(--surface-1);
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  font-size: 11.5px;
  line-height: 1.5;
}

.result-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.4;
  color: #ad6800;
}

[data-theme='dark'] .result-hint {
  color: #faad14;
}

/* JSON：保留缩进与换行，长行不折行（横向滚动交给父级 body） */
.seg-json {
  white-space: pre;
}

/* 纯文本：自动换行 */
.seg-text {
  white-space: pre-wrap;
  word-break: break-word;
}

.tok-punct {
  color: var(--color-text-secondary);
}

.tok-key {
  color: #1677ff;
}

.tok-string {
  color: #389e0d;
}

.tok-number {
  color: #d46b08;
}

.tok-boolean,
.tok-null {
  color: #722ed1;
}

.tok-text {
  color: var(--color-text);
}

[data-theme='dark'] .tok-key {
  color: #69b1ff;
}

[data-theme='dark'] .tok-string {
  color: #95de64;
}

[data-theme='dark'] .tok-number {
  color: #ffc069;
}

[data-theme='dark'] .tok-boolean,
[data-theme='dark'] .tok-null {
  color: #b37feb;
}
</style>
