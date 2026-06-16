<script setup lang="ts">
import { computed } from 'vue'
import { Tag } from 'ant-design-vue'
import { LoadingOutlined } from '@ant-design/icons-vue'
import { renderMarkdown, formatTime } from '@/utils/markdown'
import { getToolConfig, formatToolArgs, formatToolResult } from '@/utils/toolDisplay'
import LobsterIcon from '@/assets/lobster.svg'

// 消息段类型
interface TextSegment {
  type: 'text'
  id: number
  content: string
}

interface ToolSegment {
  type: 'tool'
  id: number
  tool: string
  args: Record<string, unknown>
  result?: string
  status: 'running' | 'done' | 'error'
}

type MessageSegment = TextSegment | ToolSegment

interface Message {
  id: number
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  segments?: MessageSegment[]
}

interface Props {
  message: Message
  assistantName?: string
  expandedTools?: Set<number>
}

interface Emits {
  (e: 'toggle-tool', toolId: number): void
}

const props = withDefaults(defineProps<Props>(), {
  assistantName: 'HelloClaw',
  expandedTools: () => new Set()
})

const emit = defineEmits<Emits>()

// 切换工具折叠状态
const toggleToolCollapse = (toolId: number) => {
  emit('toggle-tool', toolId)
}

// 检查工具是否展开
const isToolExpanded = (toolId: number): boolean => {
  return props.expandedTools.has(toolId)
}

// 检查是否有可见内容
const hasVisibleContent = computed(() => {
  if (!props.message.segments || props.message.segments.length === 0) {
    return !!props.message.content
  }

  for (const segment of props.message.segments) {
    if (segment.type === 'text' && segment.content) {
      return true
    }
    if (segment.type === 'tool' && !getToolConfig(segment.tool).hidden) {
      return true
    }
  }
  return false
})
</script>

<template>
  <div v-if="hasVisibleContent" :class="['chat-message', message.role]">
    <!-- 头像 -->
    <div class="message-avatar">
      <img v-if="message.role === 'assistant'" :src="LobsterIcon" alt="HelloClaw" />
      <div v-else class="user-avatar">你</div>
    </div>

    <!-- 消息内容 -->
    <div class="message-content">
      <!-- 如果有分段，按分段显示 -->
      <template v-if="message.segments && message.segments.length > 0">
        <template v-for="segment in message.segments" :key="segment.id">
          <!-- 文本段 -->
          <div v-if="segment.type === 'text' && segment.content" class="message-bubble">
            <div
              class="message-text"
              v-html="renderMarkdown(segment.content)"
            ></div>
          </div>
          <!-- 工具调用段 - 只显示非隐藏的工具 -->
          <div
            v-if="segment.type === 'tool' && !getToolConfig(segment.tool).hidden"
            :class="['tool-card', segment.status]"
          >
            <div
              class="tool-header"
              @click="segment.status !== 'running' && toggleToolCollapse(segment.id)"
            >
              <span class="tool-icon">{{ getToolConfig(segment.tool).icon }}</span>
              <span class="tool-name">
                <template v-if="!isToolExpanded(segment.id)">使用了</template>
                {{ getToolConfig(segment.tool).name }}
              </span>
              <Tag v-if="segment.status === 'running'" color="processing" class="tool-tag">
                <LoadingOutlined /> 执行中
              </Tag>
              <Tag v-else-if="segment.status === 'done'" color="success" class="tool-tag">完成</Tag>
              <Tag v-else-if="segment.status === 'error'" color="error" class="tool-tag">失败</Tag>
              <span
                v-if="segment.status !== 'running'"
                class="collapse-indicator"
              >
                {{ isToolExpanded(segment.id) ? '▼' : '▶' }}
              </span>
            </div>
            <!-- 展开后显示入参和结果 -->
            <div v-if="isToolExpanded(segment.id)" class="tool-details">
              <!-- 入参 -->
              <div v-if="segment.args && Object.keys(segment.args).length > 0" class="tool-args">
                <div class="tool-detail-label">入参</div>
                <pre class="tool-detail-content">{{ formatToolArgs(segment.args) }}</pre>
              </div>
              <!-- 结果 -->
              <div v-if="segment.result" class="tool-result-wrapper">
                <div class="tool-detail-label">结果</div>
                <pre class="tool-detail-content">{{ formatToolResult(segment.result) }}</pre>
              </div>
            </div>
          </div>
        </template>
      </template>
      <!-- 如果没有分段，显示普通内容（历史消息） -->
      <div v-else-if="message.content" class="message-bubble">
        <div
          class="message-text"
          v-html="renderMarkdown(message.content)"
        ></div>
      </div>

      <!-- 消息元信息 -->
      <div class="message-meta">
        <span class="message-sender">
          {{ message.role === 'user' ? '你' : assistantName }}
        </span>
        <span class="message-time">{{ formatTime(message.timestamp) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-message {
  display: flex;
  gap: 12px;
  max-width: 85%;
}

.chat-message.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.chat-message.assistant {
  align-self: flex-start;
}

/* 头像 */
.message-avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
}

.message-avatar img {
  width: 36px;
  height: 36px;
  border-radius: 8px;
}

.user-avatar {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background-color: var(--color-primary);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* 消息内容 */
.message-content {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

/* 消息气泡 */
.message-bubble {
  display: inline-block;
  max-width: 100%;
}

.message-text {
  padding: 10px 14px;
  border-radius: 12px;
  background-color: var(--color-surface);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
  line-height: 1.6;
  word-wrap: break-word;
}

.chat-message.user .message-text {
  background-color: var(--color-primary-light);
  border: 1px solid rgba(255, 92, 92, 0.2);
}

/* ── Markdown 渲染样式（完整覆盖所有元素） ── */

/* 段落 */
.message-text :deep(p) {
  margin: 0;
}
.message-text :deep(p + p) {
  margin-top: 8px;
}

/* 标题 */
.message-text :deep(h1) { font-size: 1.4em; font-weight: 700; margin: 16px 0 8px; border-bottom: 1px solid var(--color-border); padding-bottom: 4px; }
.message-text :deep(h2) { font-size: 1.25em; font-weight: 700; margin: 14px 0 6px; }
.message-text :deep(h3) { font-size: 1.12em; font-weight: 700; margin: 12px 0 6px; }
.message-text :deep(h4) { font-size: 1.05em; font-weight: 600; margin: 10px 0 4px; }
.message-text :deep(h5) { font-size: 1em; font-weight: 600; margin: 8px 0 4px; }
.message-text :deep(h6) { font-size: 0.95em; font-weight: 600; margin: 6px 0 4px; color: var(--color-text-secondary); }

/* 强调 */
.message-text :deep(strong) { font-weight: 700; }
.message-text :deep(em) { font-style: italic; }
.message-text :deep(del) { text-decoration: line-through; color: var(--color-text-secondary); }

/* 行内代码 */
.message-text :deep(code) {
  background-color: rgba(0, 0, 0, 0.06);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.9em;
  font-family: ui-monospace, 'SF Mono', 'Cascadia Code', 'Fira Code', Monaco, monospace;
  color: #d63384;
}

/* 代码块 */
.message-text :deep(pre) {
  background-color: #1e1e1e;
  color: #d4d4d4;
  padding: 14px 16px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 10px 0;
  line-height: 1.5;
  font-size: 0.88em;
}
.message-text :deep(pre code) {
  background-color: transparent;
  color: inherit;
  padding: 0;
  border-radius: 0;
  font-size: inherit;
}

/* 列表 */
.message-text :deep(ul),
.message-text :deep(ol) {
  margin: 6px 0;
  padding-left: 22px;
}
.message-text :deep(li) {
  margin: 2px 0;
  line-height: 1.55;
}
.message-text :deep(li > ul),
.message-text :deep(li > ol) {
  margin: 2px 0;
}
.message-text :deep(ol ol) { list-style-type: lower-alpha; }
.message-text :deep(ol ol ol) { list-style-type: lower-roman; }

/* 分割线 */
.message-text :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 14px 0;
}

/* 引用块 */
.message-text :deep(blockquote) {
  border-left: 3px solid var(--color-primary);
  padding: 6px 12px;
  margin: 8px 0;
  color: var(--color-text-secondary);
  background: rgba(0, 0, 0, 0.02);
  border-radius: 0 6px 6px 0;
}
.message-text :deep(blockquote p:last-child) {
  margin-bottom: 0;
}

/* 链接 */
.message-text :deep(a) {
  color: var(--color-primary);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.message-text :deep(a:hover) {
  color: var(--color-primary-dark, #d64545);
}

/* ── 表格 ── */
.message-text :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 0.94em;
  overflow: hidden;
  border-radius: 6px;
}
.message-text :deep(thead) {
  background-color: var(--color-primary);
  color: #fff;
}
.message-text :deep(th) {
  padding: 8px 12px;
  text-align: left;
  font-weight: 600;
  font-size: 0.92em;
}
.message-text :deep(td) {
  padding: 7px 12px;
  border-bottom: 1px solid var(--color-border);
}
.message-text :deep(tbody tr:nth-child(even)) {
  background-color: rgba(0, 0, 0, 0.02);
}
.message-text :deep(tbody tr:hover) {
  background-color: rgba(0, 0, 0, 0.04);
}

/* ── 任务列表 ── */
.message-text :deep(input[type="checkbox"]) {
  margin-right: 6px;
  accent-color: var(--color-primary);
  vertical-align: middle;
}

/* ── 图片 ── */
.message-text :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
  margin: 8px 0;
}

/* ── 键盘标签 ── */
.message-text :deep(kbd) {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 0.85em;
  font-family: ui-monospace, monospace;
  box-shadow: 0 1px 0 var(--color-border);
}

/* 上下标 */
.message-text :deep(sub),
.message-text :deep(sup) {
  font-size: 0.8em;
}

/* 行内链接卡片补偿 */
.message-text :deep(a:has(img)) {
  text-decoration: none;
}

/* 消息元信息 */
.message-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 4px;
  padding-left: 4px;
}

.message-sender {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}

.message-time {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* 工具调用卡片 */
.tool-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  transition: all 0.2s ease;
  margin-top: 8px;
}

/* 执行中状态 - 龙虾红主题 */
.tool-card.running {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.tool-card.running .tool-icon,
.tool-card.running .tool-name {
  color: var(--color-primary);
}

/* 完成状态 - 灰色调 */
.tool-card.done {
  border-color: var(--color-border);
  background: var(--color-surface);
}

/* 失败状态 - 红色调 */
.tool-card.error {
  border-color: var(--color-primary);
  background: #fff1f0;
}

.tool-card.error .tool-icon,
.tool-card.error .tool-name {
  color: var(--color-primary);
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.tool-header:hover {
  opacity: 0.8;
}

.tool-icon {
  font-size: 14px;
  line-height: 1;
}

.tool-name {
  font-weight: 500;
  color: var(--color-text);
  flex: 1;
}

.tool-tag {
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

/* 工具详情区域 */
.tool-details {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border);
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
  color: var(--color-text-secondary);
  margin-bottom: 4px;
  font-weight: 500;
}

.tool-detail-content {
  margin: 0;
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
