import { marked } from 'marked'
import DOMPurify from 'dompurify'

// 配置 marked：同步模式，确保在 Vue 模板 v-html 中正确渲染
marked.setOptions({
  breaks: true,     // 换行符转换为 <br>
  gfm: true,        // GitHub Flavored Markdown（表格、任务列表、删除线等）
  async: false,     // ⚠️ 同步模式（marked v5+ 默认为异步，v-html 中必须同步）
})

// 允许的标签（覆盖常用 Markdown 元素）
const allowedTags = [
  'a', 'b', 'blockquote', 'br', 'code', 'del', 'em',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img',
  'li', 'ol', 'p', 'pre', 'strong', 'sub', 'sup',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'ul', 'input', 'span',
]

// 允许的属性
const allowedAttrs = [
  'class', 'href', 'rel', 'target', 'title', 'src', 'alt',
  'type', 'checked', 'disabled',  // 任务列表 checkbox
]

/** DOMPurify 配置（复用，避免每次调用重建） */
const purifyConfig: DOMPurify.Config = {
  ALLOWED_TAGS: allowedTags,
  ALLOWED_ATTR: allowedAttrs,
}

/**
 * 渲染 Markdown 为安全的 HTML（同步版本，适用于 v-html）
 *
 * ⚠️ marked v5+ 默认为异步 parse()，需通过 setOptions({ async: false })
 *    确保此处返回的是字符串而非 Promise，否则 v-html 会渲染 [object Promise]。
 */
export function renderMarkdown(text: string): string {
  if (!text) return ''

  // 解析 Markdown → HTML
  const html = marked.parse(text, { async: false }) as string

  // 清理 HTML，防止 XSS
  const clean = DOMPurify.sanitize(html, purifyConfig)

  return clean
}

/**
 * 格式化时间戳
 */
export function formatTime(date: Date): string {
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
}
