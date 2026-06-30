<script setup lang="ts">
/**
 * 用户消息内容渲染器
 *
 * 后端在拼装多模态 user content 时，会把上传的文档以
 *
 *     <file name="report.pdf" kind="pdf">
 *     ...文档抽取出的全文...
 *     </file>
 *
 * 形式注入到 user 消息的文本中。LLM 拿到的依旧是这段完整文本，但
 * 在 UI 上直接渲染会让用户气泡变得极长。
 *
 * 本组件做的事：
 *   1. 用正则把内容拆为 `prefix + file-block-x-N + suffix`
 *   2. 普通文本片段走 markdown 渲染
 *   3. 文档块折叠为「📄 文件名」卡片，点击展开显示原始文本（<pre> 形式，不再走 markdown，避免再次渲染崩坏）
 */
import { computed, ref } from 'vue'
import {
  FileOutlined,
  FilePdfOutlined,
  FileWordOutlined,
  FileExcelOutlined,
  FileTextOutlined,
  DownOutlined,
  RightOutlined,
} from '@ant-design/icons-vue'
import { renderMarkdown } from '@/utils/markdown'

interface Props {
  content: string
}

const props = defineProps<Props>()

type Segment =
  | { type: 'text'; text: string }
  | { type: 'file'; filename: string; kind: string; body: string }

/**
 * 把内容拆为段。
 *
 * 匹配规则：`<file name="..." kind="...">...</file>`
 * - name / kind 属性的值允许带空格与中文，使用非贪婪 `[^"]*`
 * - 内部 body 用非贪婪 `[\s\S]*?` 跨行匹配
 * - 标签必须严格成对，未闭合的不识别（避免误吞用户原文）
 */
const FILE_BLOCK_RE = /<file\s+name="([^"]*)"\s+kind="([^"]*)">([\s\S]*?)<\/file>/g

const segments = computed<Segment[]>(() => {
  const text = props.content || ''
  const out: Segment[] = []
  let lastIdx = 0
  // re 不能直接用 computed 中复用同一实例（lastIndex 状态），每次重建
  const re = new RegExp(FILE_BLOCK_RE.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIdx) {
      out.push({ type: 'text', text: text.slice(lastIdx, m.index) })
    }
    out.push({
      type: 'file',
      filename: m[1] || '未命名文档',
      kind: m[2] || 'doc',
      // 去除头尾换行（后端拼接时会带）
      body: (m[3] || '').replace(/^\n+|\n+$/g, ''),
    })
    lastIdx = m.index + m[0].length
  }
  if (lastIdx < text.length) {
    out.push({ type: 'text', text: text.slice(lastIdx) })
  }
  return out
})

/** 折叠展开状态：segIndex -> bool */
const expanded = ref<Record<number, boolean>>({})

function toggle(idx: number) {
  expanded.value[idx] = !expanded.value[idx]
}

function iconFor(kind: string) {
  switch ((kind || '').toLowerCase()) {
    case 'pdf':
      return FilePdfOutlined
    case 'doc':
    case 'docx':
    case 'word':
      return FileWordOutlined
    case 'xls':
    case 'xlsx':
    case 'csv':
    case 'excel':
      return FileExcelOutlined
    case 'txt':
    case 'md':
    case 'markdown':
    case 'text':
    case 'log':
      return FileTextOutlined
    default:
      return FileOutlined
  }
}

function charCount(body: string): string {
  const n = body.length
  if (n < 1000) return `${n} 字`
  if (n < 10000) return `${(n / 1000).toFixed(1)}k 字`
  return `${Math.round(n / 1000)}k 字`
}

/** 文本段是否为纯空白：避免在两个 file 块之间渲染空段 */
function isBlankText(s: string) {
  return !s || !s.trim()
}
</script>

<template>
  <div class="user-msg-content">
    <template v-for="(seg, idx) in segments" :key="idx">
      <!-- 文本段：走 markdown -->
      <div
        v-if="seg.type === 'text' && !isBlankText(seg.text)"
        class="message-text"
        v-html="renderMarkdown(seg.text)"
      ></div>

      <!-- 文档块：折叠卡片 -->
      <div
        v-else-if="seg.type === 'file'"
        class="user-file-card"
        :class="{ expanded: !!expanded[idx] }"
      >
        <button
          type="button"
          class="user-file-header"
          :title="seg.filename"
          @click.stop="toggle(idx)"
        >
          <component :is="iconFor(seg.kind)" class="user-file-icon" />
          <span class="user-file-name">{{ seg.filename }}</span>
          <span class="user-file-meta">{{ charCount(seg.body) }}</span>
          <component
            :is="expanded[idx] ? DownOutlined : RightOutlined"
            class="user-file-caret"
          />
        </button>
        <pre v-if="expanded[idx]" class="user-file-body">{{ seg.body }}</pre>
      </div>
    </template>
  </div>
</template>

<style scoped>
.user-msg-content {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* 复用全局 .message-text 的字体/排版，仅在这里覆盖间距 */
.user-msg-content :deep(.message-text) {
  margin: 0;
}

.user-file-card {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-surface);
  overflow: hidden;
  max-width: 100%;
  box-sizing: border-box;
}

.user-file-header {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  font: inherit;
  color: var(--color-text);
}

.user-file-header:hover {
  background: rgba(0, 0, 0, 0.03);
}

.user-file-icon {
  font-size: 18px;
  color: var(--color-primary);
  flex-shrink: 0;
}

.user-file-name {
  flex: 1;
  min-width: 0;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-file-meta {
  font-size: 11px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.user-file-caret {
  font-size: 11px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.user-file-body {
  margin: 0;
  padding: 10px 12px;
  border-top: 1px dashed var(--color-border);
  background: rgba(0, 0, 0, 0.02);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
</style>
