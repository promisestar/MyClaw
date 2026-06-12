import api from './index'

export interface LogFileInfo {
  date_str: string
  file_name: string
  entry_count: number
  size_bytes: number
  modified_at: string
}

export interface LogFileListResponse {
  files: LogFileInfo[]
  total: number
}

export interface LogEntry {
  timestamp: string
  trace_id: string
  session_id: string
  tool_name: string
  tool_call_id: string
  args: Record<string, unknown>
  result: string
  result_len: number
  status: string
  duration_ms: number
}

export interface LogFileContentResponse {
  date_str: string
  entries: LogEntry[]
  total: number
}

export const toolLogsApi = {
  /** 获取日志文件列表 */
  list: () => api.get<LogFileListResponse>('/tool-logs/list'),

  /** 获取指定日期日志内容 */
  get: (dateStr: string, limit?: number) => {
    const params = limit ? { limit } : {}
    return api.get<LogFileContentResponse>(`/tool-logs/${dateStr}`, { params })
  },

  /** 删除指定日期的日志文件 */
  delete: (dateStr: string) => api.delete(`/tool-logs/${dateStr}`),
}
