import api from './index'

export interface Session {
  id: string
  created_at: number
  updated_at: number
}

// OpenAI 标准消息格式
export interface ToolCallFunction {
  name: string
  arguments: string  // JSON 字符串
}

export interface ToolCall {
  id: string
  type: 'function'
  function: ToolCallFunction
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'tool'
  content?: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
}

export interface SessionHistory {
  session_id: string
  messages: ChatMessage[]
}

export interface ContextUsage {
  session_id: string | null
  context_window: number
  used_tokens: number
  system_tokens: number
  history_tokens: number
  used_percent: number
}

export const sessionApi = {
  list: async () => {
    return api.get<{ sessions: Session[] }>('/session/list')
  },
  create: async () => {
    return api.post<{ session_id: string }>('/session/create')
  },
  get: async (id: string) => {
    return api.get<Session>(`/session/${id}`)
  },
  delete: async (id: string) => {
    return api.delete(`/session/${id}`)
  },
  getHistory: async (id: string) => {
    return api.get<SessionHistory>(`/session/${id}/history`)
  },
  getContextUsage: async (id: string) => {
    return api.get<ContextUsage>(`/session/${id}/context-usage`)
  },
}
