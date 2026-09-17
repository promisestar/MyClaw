import api from './index'

/** 一次（或累计多次）LLM 调用的 token 用量，与后端 normalize_usage 对齐 */
export interface TraceTokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_tokens: number
  reasoning_tokens: number
}

/** 请求侧元数据（RESPONSE 卡片的对侧，供 REQUEST 卡片展示） */
export interface TraceRequestMeta {
  system_count?: number
  message_count?: number
  tool_count?: number
}

/** 响应侧元数据 */
export interface TraceResponseMeta {
  content_preview?: string
  content_len?: number
  tool_call_count?: number
  finish_reason?: string
  reasoning_tokens?: number
}

/** 同一次工具调用的历史重试尝试 */
export interface TraceToolAttempt {
  status: string
  duration_ms: number
  retry_attempt?: number | null
  error_type?: string | null
  timestamp: string
}

/** 工具调用 span（同一 tool_call_id 的重试已合并） */
export interface TraceToolSpan {
  tool_call_id: string
  tool_name: string
  args: Record<string, unknown>
  result: string
  result_len: number
  status: string
  error_type?: string | null
  agent_name: string
  duration_ms: number
  start_ts: string
  end_ts: string
  retry_count: number
  attempts: TraceToolAttempt[]
}

/** 模型调用 span（一次 LLM 调用，含其触发的一批工具调用） */
export interface TraceModelSpan {
  seq: number | null
  iteration: number | null
  call_site: string
  model: string
  agent_name: string
  stream: boolean
  status: string
  error?: string | null
  duration_ms: number
  start_ts: string
  end_ts: string
  usage: TraceTokenUsage
  request: TraceRequestMeta | null
  response: TraceResponseMeta | null
  tools: TraceToolSpan[]
}

/** 轮次摘要（左侧列表用） */
export interface TraceTurnSummary {
  trace_id: string
  session_id: string
  index: number
  prompt_preview: string
  start_ts: string
  end_ts: string
  duration_ms: number
  model_calls: number
  tool_calls: number
  usage: TraceTokenUsage
  status: string
  agents: string[]
  /** exact=显式轮次精确归属；time_window=时间窗推断；unassigned/mixed=存在未归属工具 */
  attribution: string
}

/** 整轮 span 树 */
export interface TraceTurnTree extends TraceTurnSummary {
  model_spans: TraceModelSpan[]
  side_spans: TraceModelSpan[]
  unassigned_tools: TraceToolSpan[]
}

export interface TraceDateInfo {
  date_str: string
  tool_entries: number
  usage_entries: number
  modified_at: string
}

export interface TraceTurnsResponse {
  date_str: string
  sessions: {
    session_id: string
    turn_count: number
    turns: TraceTurnSummary[]
  }[]
  total_turns: number
}

export const traceApi = {
  /** 可用日期列表（合并工具日志与用量日志） */
  dates: () => api.get<{ dates: TraceDateInfo[]; total: number }>('/trace/dates'),

  /** 某日全部轮次摘要 */
  turns: (date: string, sessionId?: string) =>
    api.get<TraceTurnsResponse>('/trace/turns', {
      params: sessionId ? { date, session_id: sessionId } : { date },
    }),

  /** 单个轮次的完整 span 树 */
  turn: (traceId: string, date: string) =>
    api.get<TraceTurnTree>(`/trace/turns/${encodeURIComponent(traceId)}`, {
      params: { date },
    }),
}
