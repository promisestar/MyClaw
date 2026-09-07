import api from './index'
import type { ContextUsage } from './session'
import type { TokenUsage } from './usage'

const API_BASE = import.meta.env.VITE_API_BASE || ''

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

/** 与后端 src/api/chat.py 的 Attachment 对齐 */
export interface ChatAttachment {
  stored_path: string
  filename: string
  mime_type: string
  kind: 'image' | 'doc' | 'other'
  size: number
}

export interface ChatResponse {
  content: string
  session_id: string | null
}

/** Agent 模式：ask（只读）| plan（规划→确认→执行）| craft（全自动） */
export type AgentMode = 'ask' | 'plan' | 'craft'

/** Plan 模式生成的结构化 TODO 项 */
export interface PlanTodoItem {
  id: string
  description: string
  dependencies: string[]
  tools_required: string[]
}

export interface StreamEvent {
  type: 'session' | 'step_start' | 'chunk' | 'tool_start' | 'tool_finish' | 'step_finish' | 'done' | 'cancelled' | 'error' | 'plan_generated'
  content?: string
  tool?: string
  args?: Record<string, unknown>
  result?: string
  error?: string
  session_id?: string | null
  step?: number
  max_steps?: number
  /** 对话完成后由服务端推送的上下文窗口用量 */
  context_usage?: ContextUsage
  /** 本轮对话累计的 LLM token 用量（含缓存命中） */
  usage?: TokenUsage
  /** 本轮对话发生的 LLM 调用次数 */
  llm_calls?: number
  /** plan_generated 事件：结构化 TODO 列表 */
  plan?: PlanTodoItem[]
}

export type StreamCallback = (event: StreamEvent) => void

export interface SendMessageOptions {
  sessionId?: string | null
  userTurnIndex?: number
  /** 重新生成时传 true（与 userTurnIndex 合用）；编辑后发送传 false */
  regenerate?: boolean
  /** 用户通过 /技能名 指定的技能名 */
  skill?: string
  /** 多模态附件列表（已通过 /upload/file 上传得到 stored_path） */
  attachments?: ChatAttachment[]
  /** 工作区路径（指定后后端切换到该工作区再处理消息） */
  workspacePath?: string
  /** Agent 模式：ask（只读）| plan（规划→确认→执行）| craft（全自动），默认 craft */
  mode?: AgentMode
  /** Plan 模式：用户确认计划后发起新请求时传 true，后端加载已生成的 TODO 进入执行阶段 */
  planConfirmed?: boolean
  signal?: AbortSignal
}

export interface CancelResponse {
  success: boolean
  message: string
}

export const chatApi = {
  // 流式发送消息 (SSE)
  sendMessage: async (message: string, sessionId?: string) => {
    return api.post('/chat/send', { message, session_id: sessionId })
  },

  // 取消当前正在执行的 Agent 生成
  cancelGeneration: async (): Promise<CancelResponse> => {
    return api.post('/chat/cancel')
  },

  // 同步发送消息（支持取消，超时时间 5 分钟）
  sendMessageSync: async (
    message: string,
    sessionId?: string,
    signal?: AbortSignal
  ): Promise<ChatResponse> => {
    return api.post('/chat/send/sync', { message, session_id: sessionId }, {
      signal,
      timeout: 300000, // 5 分钟超时
    })
  },

  // 流式发送消息 (SSE) - 返回完整响应
  sendMessageStream: async (
    message: string,
    onChunk: StreamCallback,
    options: SendMessageOptions = {}
  ): Promise<ChatResponse> => {
    const { sessionId, userTurnIndex, regenerate, skill, attachments, workspacePath, mode, planConfirmed, signal } = options
    const body: Record<string, unknown> = {
      message,
      session_id: sessionId,
    }
    if (userTurnIndex !== undefined) {
      body.user_turn_index = userTurnIndex
      body.regenerate = regenerate ?? false
    }
    if (skill) {
      body.skill = skill
    }
    if (attachments && attachments.length > 0) {
      body.attachments = attachments
    }
    if (workspacePath) {
      body.workspace_path = workspacePath
    }
    if (mode) {
      body.mode = mode
    }
    if (planConfirmed) {
      body.plan_confirmed = true
    }

    const response = await fetch(`${API_BASE}/api/chat/send/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
      signal,
    })

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      throw new Error('No response body')
    }

    const decoder = new TextDecoder()
    let buffer = ''
    let fullContent = ''
    let finalSessionId = sessionId

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // 解析 SSE 事件
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        let currentEvent = ''
        for (const line of lines) {
          // 跳过 ping 事件和空行
          if (line.startsWith('ping') || line.trim() === '') {
            continue
          }

          if (line.startsWith('event:')) {
            currentEvent = line.substring(6).trim()
          } else if (line.startsWith('data:')) {
            const data = line.substring(5).trim()
            if (data && currentEvent) {
              try {
                const parsed = JSON.parse(data)

                if (currentEvent === 'session') {
                  finalSessionId = parsed.session_id
                  onChunk({ type: 'session', session_id: parsed.session_id })
                } else if (currentEvent === 'step_start') {
                  onChunk({ type: 'step_start', step: parsed.step, max_steps: parsed.max_steps })
                } else if (currentEvent === 'chunk') {
                  fullContent += parsed.content || ''
                  onChunk({ type: 'chunk', content: parsed.content })
                } else if (currentEvent === 'tool_start') {
                  onChunk({ type: 'tool_start', tool: parsed.tool, args: parsed.args })
                } else if (currentEvent === 'tool_finish') {
                  onChunk({ type: 'tool_finish', tool: parsed.tool, result: parsed.result })
                } else if (currentEvent === 'step_finish') {
                  onChunk({ type: 'step_finish', step: parsed.step })
                } else if (currentEvent === 'done') {
                  finalSessionId = parsed.session_id
                  onChunk({
                    type: 'done',
                    content: parsed.content,
                    session_id: parsed.session_id,
                    context_usage: parsed.context_usage,
                    usage: parsed.usage,
                    llm_calls: parsed.llm_calls,
                  })
                } else if (currentEvent === 'cancelled') {
                  onChunk({ type: 'cancelled', error: parsed.reason || 'cancelled' })
                } else if (currentEvent === 'error') {
                  onChunk({ type: 'error', error: parsed.error })
                } else if (currentEvent === 'plan_generated') {
                  // Plan 模式：LLM 生成的结构化 TODO 计划
                  onChunk({
                    type: 'plan_generated',
                    plan: parsed.plan || [],
                    content: parsed.content || '',
                  })
                }
              } catch {
                // 忽略解析错误
              }
              currentEvent = ''
            }
          }
        }
      }
    } finally {
      reader.releaseLock()
    }

    return { content: fullContent, session_id: finalSessionId ?? null }
  },
}
