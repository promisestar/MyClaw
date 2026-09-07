import api from './index'

/** 一次（或累计多次）LLM 调用的 token 用量，与后端 normalize_usage 对齐 */
export interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  /** 命中前缀缓存的 prompt token 数（验证 system 提示词冻结效果的关键指标） */
  cached_tokens: number
  /** 思考型模型的推理 token 数 */
  reasoning_tokens: number
}

export interface UsageBucket {
  name: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_tokens: number
  reasoning_tokens: number
  cache_hit_rate: number
  avg_duration_ms: number
  duration_ms?: number
}

export interface UsageSummary {
  date_str?: string
  days?: number
  calls: number
  /** 真正拿到 usage 的调用次数 */
  calls_with_usage: number
  /** usage 覆盖率 = calls_with_usage / calls */
  usage_coverage: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_tokens: number
  reasoning_tokens: number
  /** 缓存命中率 = cached_tokens / prompt_tokens */
  cache_hit_rate: number
  avg_duration_ms: number
  by_model: UsageBucket[]
  by_call_site: UsageBucket[]
  by_day?: UsageSummary[]
}

export interface UsageEntry extends TokenUsage {
  timestamp: string
  trace_id: string
  session_id: string
  agent_name: string
  call_site: string
  model: string
  stream: boolean
  iteration: number | null
  duration_ms: number
  status: string
  error?: string
}

export interface UsageFilesResponse {
  files: {
    date_str: string
    file_name: string
    entry_count: number
    size_bytes: number
    modified_at: string
  }[]
  total: number
}

export interface UsageRecentResponse {
  date_str: string
  entries: UsageEntry[]
  total: number
}

export interface UsageLogFileContentResponse {
  date_str: string
  file_name: string
  log_type: 'llm_usage'
  entries: UsageEntry[]
  total: number
}

export const usageApi = {
  /** 最近 N 天汇总（含按天趋势） */
  summary: (days = 7) => api.get<UsageSummary>('/usage/summary', { params: { days } }),

  /** 单日汇总 */
  day: (dateStr: string) => api.get<UsageSummary>(`/usage/day/${dateStr}`),

  /** 读取指定日期的用量原始日志全文（llm-usage-YYYY-MM-DD.jsonl） */
  logs: (dateStr: string, limit?: number) =>
    api.get<UsageLogFileContentResponse>(`/usage/logs/${dateStr}`, {
      params: limit ? { limit } : undefined,
    }),

  /** 最近若干条原始记录（排障用） */
  recent: (date?: string, limit = 100) =>
    api.get<UsageRecentResponse>('/usage/recent', { params: date ? { date, limit } : { limit } }),

  /** 日志文件列表 */
  files: () => api.get<UsageFilesResponse>('/usage/files'),

  /** 删除指定日期的用量日志 */
  remove: (dateStr: string) => api.delete(`/usage/day/${dateStr}`),
}
