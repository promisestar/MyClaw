import api from './index'

/** 单个任务项 */
export interface TaskItem {
  id: string
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'cancelled'
  blocked_by: string[]
  blocks: string[]
  created_at: number
  started_at: number | null
  completed_at: number | null
  notes: string
}

/** 任务进度响应 */
export interface TaskProgress {
  session_id: string | null
  total: number
  completed: number
  in_progress: number
  pending: number
  failed: number
  cancelled: number
  blocked: number
  summary: string
  tasks: TaskItem[]
}

export const agentApi = {
  /** 获取当前会话（或指定会话）的任务进度 */
  getTaskProgress: async (sessionId?: string) => {
    const params = sessionId ? { session_id: sessionId } : {}
    return api.get<TaskProgress>('/agent/task-progress', { params })
  },
}
