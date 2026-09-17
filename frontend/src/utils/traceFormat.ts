/** 轨迹页（TraceView）展示格式化工具 */

/** 时间瀑布条（TraceWaterfall）的一行数据 */
export interface WaterfallItem {
  key: string
  title: string
  kind: 'llm' | 'tool' | 'side'
  start_ts: string
  end_ts: string
  duration_ms: number
  status?: string
}

/** ISO 时间 → 本地时刻（HH:mm:ss） */
export function formatClockTime(isoStr: string | undefined | null): string {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  if (Number.isNaN(d.getTime())) return isoStr
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

/** ISO 时间 → 完整的本地时间（详情/悬浮用） */
export function formatDateTime(isoStr: string | undefined | null): string {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  if (Number.isNaN(d.getTime())) return isoStr
  return d.toLocaleString('zh-CN')
}

/** ISO 时间 → epoch 毫秒；非法返回 0 */
export function parseTs(isoStr: string | undefined | null): number {
  if (!isoStr) return 0
  const t = new Date(isoStr).getTime()
  return Number.isNaN(t) ? 0 : t
}

/** 耗时格式化：823ms / 3.0s / 4m26s */
export function formatDuration(ms: number | undefined | null): string {
  const value = Number(ms) || 0
  if (value < 1000) return `${Math.round(value)}ms`
  if (value < 60000) return `${(value / 1000).toFixed(1)}s`
  const minutes = Math.floor(value / 60000)
  const seconds = Math.round((value % 60000) / 1000)
  return `${minutes}m${seconds}s`
}

/** 千分位数字 */
export function formatTokens(n: number | undefined | null): string {
  return (Number(n) || 0).toLocaleString('zh-CN')
}

/** 未缓存输入 = 输入 - 缓存命中 */
export function uncachedTokens(prompt: number, cached: number): number {
  return Math.max((Number(prompt) || 0) - (Number(cached) || 0), 0)
}

/** 工具参数摘要（优先展示路径/命令/查询等可读字段） */
export function summarizeArgs(args: Record<string, unknown> | undefined | null): string {
  if (!args) return ''
  const preferred = ['query', 'path', 'command', 'url', 'pattern', 'file', 'name']
  for (const key of preferred) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) {
      return value.length > 90 ? `${value.slice(0, 90)}…` : value
    }
  }
  const first = Object.entries(args)[0]
  if (!first) return ''
  const text = typeof first[1] === 'string' ? first[1] : JSON.stringify(first[1])
  const line = `${first[0]}=${text}`
  return line.length > 90 ? `${line.slice(0, 90)}…` : line
}

/** 结果预览（截断，避免超长内容撑破卡片） */
export function summarizeResult(result: string | undefined | null, limit = 220): string {
  if (!result) return ''
  const flat = result.replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

/** 会话 ID 缩短展示 */
export function shortId(id: string | undefined | null, keep = 10): string {
  if (!id) return '未关联会话'
  return id.length > keep ? `${id.slice(0, keep)}…` : id
}

/** attribution 文案 */
export function attributionLabel(attribution: string | undefined): string {
  switch (attribution) {
    case 'exact':
      return '精确关联'
    case 'time_window':
      return '时间窗推断'
    case 'unassigned':
      return '存在未归属工具'
    case 'mixed':
      return '部分为推断'
    default:
      return ''
  }
}

/**
 * 工具结果体量文案（后端 ToolCallLogger.log 落盘时为 `result[:2000]`，超出即截断）：
 * - 日志被截断 → 「日志截断 2,000/5,675」（避免误以为工具只输出了 2000 字符）
 * - 未截断 → 「7 字符」
 */
export function resultSizeLabel(
  resultLen: number | undefined | null,
  result: string | undefined | null,
): string {
  const total = Number(resultLen) || 0
  const logged = (result || '').length
  if (logged > 0 && total > logged) {
    return `日志截断 ${formatTokens(logged)}/${formatTokens(total)}`
  }
  return `${formatTokens(total || logged)} 字符`
}

// ══════════════════════════════════════════════════════════
// 工具结果渲染：JSON 漂亮打印 + 语法着色
// ══════════════════════════════════════════════════════════

/** 着色 token 类型；`text` 表示非 JSON 的纯文本段整体 */
export type ResultTokenType =
  | 'key'
  | 'string'
  | 'number'
  | 'boolean'
  | 'null'
  | 'punct'
  | 'text'

export interface ResultToken {
  type: ResultTokenType
  text: string
}

/** 工具结果被拆出的一个片段（JSON 段或纯文本段），tokens 直接供模板渲染 */
export interface ToolResultSegment {
  kind: 'json' | 'text'
  text: string
  tokens: ResultToken[]
  /** JSON 段由「截断内容修复」得到（日志只落了前 2000 字符），需向用户说明 */
  repaired?: boolean
}

const JSON_OPENERS = new Set(['{', '['])

/**
 * 从 `start` 起扫描配对的 JSON 块，返回结束下标（不含）；失败返回 -1。
 *
 * 需要自己扫描而不能用正则：字符串里可能出现 `{`、`}`、`"`（含 `\"` 转义），
 * 正则会误判边界。
 */
function matchJsonBlock(text: string, start: number): number {
  const opener = text[start]
  if (opener !== '{' && opener !== '[') return -1

  let depth = 0
  let inString = false
  let i = start

  while (i < text.length) {
    const ch = text[i] as string
    if (inString) {
      if (ch === '\\') {
        i += 2
        continue
      }
      if (ch === '"') inString = false
      i += 1
      continue
    }
    if (ch === '"') {
      inString = true
      i += 1
      continue
    }
    if (ch === '{' || ch === '[') {
      depth += 1
    } else if (ch === '}' || ch === ']') {
      depth -= 1
      if (depth === 0) return i + 1
    }
    i += 1
  }
  return -1
}

/** index 处是否位于字符串字面量内部（正确处理 `\"` 转义） */
function isInsideString(text: string, index: number): boolean {
  let inString = false
  let i = 0
  while (i < index) {
    const ch = text[i] as string
    if (inString) {
      if (ch === '\\') {
        i += 2
        continue
      }
      if (ch === '"') inString = false
    } else if (ch === '"') {
      inString = true
    }
    i += 1
  }
  return inString
}

/**
 * 补齐未闭合的字符串与括号（不做合法性校验）；括号多余时返回 null。
 *
 * 用于「日志把 JSON 截断」的场景：末尾常停在字符串中间或半个元素处。
 */
function closeJson(text: string): string | null {
  const stack: string[] = []
  let inString = false
  let i = 0

  while (i < text.length) {
    const ch = text[i] as string
    if (inString) {
      if (ch === '\\') {
        i += 2
        continue
      }
      if (ch === '"') inString = false
      i += 1
      continue
    }
    if (ch === '"') {
      inString = true
    } else if (ch === '{') {
      stack.push('}')
    } else if (ch === '[') {
      stack.push(']')
    } else if (ch === '}' || ch === ']') {
      if (!stack.length) return null
      stack.pop()
    }
    i += 1
  }

  let out = text
  if (inString) {
    // 末尾若停在转义符上，先丢掉它再闭合字符串
    if (out.endsWith('\\')) out = out.slice(0, -1)
    out += '"'
  }
  while (stack.length) out += stack.pop() as string
  return out
}

/**
 * 解析被截断的 JSON。
 *
 * 先尝试直接补齐末尾；失败则回退「丢弃末尾不完整元素」——从后往前找字符串外的逗号，
 * 截断到该逗号之前再补齐（保证不会退化成 `{}` 而丢掉全部内容）。
 *
 * Returns: 解析结果；无法修复返回 null
 */
function parseRepairedJson(text: string): { value: unknown } | null {
  const direct = closeJson(text)
  if (direct !== null) {
    try {
      return { value: JSON.parse(direct) }
    } catch {
      // 落到回退分支
    }
  }

  let attempts = 0
  for (let i = text.length - 1; i >= 0 && attempts < 50; i -= 1) {
    if (text[i] !== ',') continue
    attempts += 1
    if (isInsideString(text, i)) continue
    const repaired = closeJson(text.slice(0, i))
    if (repaired === null) continue
    try {
      return { value: JSON.parse(repaired) }
    } catch {
      continue
    }
  }
  return null
}

/**
 * 对漂亮打印后的 JSON 文本做词法扫描，输出分色 token。
 *
 * 采用状态机而非正则：字符串内的 `:` / `,` / 关键字不能被误判；
 * 字符串 token 通过「向后跳过空白是否为 `:`」判定为键名。
 * 相邻同类 token 会被合并，以减少 DOM 节点数（token 内可能含换行，靠 white-space: pre 保留）。
 */
export function tokenizeJson(pretty: string): ResultToken[] {
  const tokens: ResultToken[] = []
  const push = (type: ResultTokenType, text: string) => {
    if (!text) return
    const last = tokens[tokens.length - 1]
    if (last && last.type === type) last.text += text
    else tokens.push({ type, text })
  }

  const len = pretty.length
  let i = 0

  while (i < len) {
    const ch = pretty[i] as string

    // 字符串字面量（含转义）
    if (ch === '"') {
      let j = i + 1
      while (j < len) {
        const c = pretty[j] as string
        if (c === '\\') {
          j += 2
          continue
        }
        if (c === '"') {
          j += 1
          break
        }
        j += 1
      }
      let k = j
      while (k < len && /\s/.test(pretty[k] as string)) k += 1
      push(pretty[k] === ':' ? 'key' : 'string', pretty.slice(i, j))
      i = j
      continue
    }

    // 数字（含负号、小数、指数）
    if (ch === '-' || (ch >= '0' && ch <= '9')) {
      let j = i + 1
      while (j < len && /[0-9eE+\-.]/.test(pretty[j] as string)) j += 1
      push('number', pretty.slice(i, j))
      i = j
      continue
    }

    // 字面量
    if (pretty.startsWith('true', i) || pretty.startsWith('false', i)) {
      const word = pretty.startsWith('true', i) ? 'true' : 'false'
      push('boolean', word)
      i += word.length
      continue
    }
    if (pretty.startsWith('null', i)) {
      push('null', 'null')
      i += 4
      continue
    }

    // 结构符号与空白（连续合并，避免每字符一个节点）
    push('punct', ch)
    i += 1
  }

  return tokens
}

/**
 * 把工具结果拆成可渲染片段：
 *
 * - 整串是 JSON → 单个 JSON 段；
 * - `{…}\n\n备注文本` 这类混合结果 → JSON 段 + 文本段（如 session_search 的返回值）；
 * - 非 JSON → 单个文本段（保留原有换行）。
 *
 * JSON 段统一 2 空格缩进漂亮打印，并附带着色 token。
 */
export function parseToolResult(text: string | undefined | null): ToolResultSegment[] {
  const raw = (text ?? '').replace(/\r\n/g, '\n')
  if (!raw.trim()) return []

  const segments: ToolResultSegment[] = []
  let pending = ''
  let cursor = 0

  const flushPending = () => {
    const trimmed = pending.trim()
    if (trimmed) {
      segments.push({ kind: 'text', text: trimmed, tokens: [{ type: 'text', text: trimmed }] })
    }
    pending = ''
  }

  while (cursor < raw.length) {
    const ch = raw[cursor] as string

    if (!JSON_OPENERS.has(ch)) {
      pending += ch
      cursor += 1
      continue
    }

    const end = matchJsonBlock(raw, cursor)
    if (end === -1) {
      // 括号未闭合：多为「日志把结果截断到 2000 字符」导致，尝试修复后解析
      const repairedResult = parseRepairedJson(raw.slice(cursor))
      if (repairedResult) {
        flushPending()
        const pretty = JSON.stringify(repairedResult.value, null, 2) || raw.slice(cursor)
        segments.push({
          kind: 'json',
          text: pretty,
          tokens: tokenizeJson(pretty),
          repaired: true,
        })
        cursor = raw.length
        continue
      }
      pending += ch
      cursor += 1
      continue
    }

    let parsed: unknown
    try {
      parsed = JSON.parse(raw.slice(cursor, end))
    } catch {
      // 括号配对成功但不是合法 JSON（如正文里的大括号），按普通文本处理
      pending += ch
      cursor += 1
      continue
    }

    flushPending()
    const pretty = JSON.stringify(parsed, null, 2) || raw.slice(cursor, end)
    segments.push({ kind: 'json', text: pretty, tokens: tokenizeJson(pretty) })
    cursor = end
  }

  flushPending()
  return segments
}
