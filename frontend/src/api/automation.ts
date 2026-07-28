import api from './index'

export interface AutomationTask {
  id: string
  name: string
  prompt: string
  schedule_type: 'once' | 'interval' | 'rrule'
  schedule_config: Record<string, unknown>
  webhook_url: string | null
  enabled: boolean
  workspace_path: string
  created_at: string
  last_run_at: string | null
  next_run_at: string | null
  run_count: number
  max_runs: number | null
  max_duration_minutes: number | null
}

export interface AutomationRun {
  run_id: string
  task_id: string
  prompt: string
  result: string
  success: boolean
  error: string | null
  started_at: string
  finished_at: string
}

export interface CreateAutomationPayload {
  name: string
  prompt: string
  schedule_type: 'once' | 'interval' | 'rrule'
  schedule_config: Record<string, unknown>
  webhook_url?: string | null
  workspace_path?: string
  enabled?: boolean
  max_runs?: number | null
  max_duration_minutes?: number | null
}

export interface UpdateAutomationPayload {
  name?: string
  prompt?: string
  schedule_type?: 'once' | 'interval' | 'rrule'
  schedule_config?: Record<string, unknown>
  webhook_url?: string | null
  workspace_path?: string
  enabled?: boolean
  max_runs?: number | null
  max_duration_minutes?: number | null
}

export const automationApi = {
  list: async (enabledOnly = false) => {
    return api.get<AutomationTask[]>('/automation', {
      params: enabledOnly ? { enabled_only: true } : {},
    })
  },

  get: async (taskId: string) => {
    return api.get<AutomationTask>(`/automation/${taskId}`)
  },

  create: async (payload: CreateAutomationPayload) => {
    return api.post<AutomationTask>('/automation', payload)
  },

  update: async (taskId: string, payload: UpdateAutomationPayload) => {
    return api.put<AutomationTask>(`/automation/${taskId}`, payload)
  },

  delete: async (taskId: string) => {
    return api.delete<{ deleted: string }>(`/automation/${taskId}`)
  },

  enable: async (taskId: string) => {
    return api.post<AutomationTask>(`/automation/${taskId}/enable`)
  },

  disable: async (taskId: string) => {
    return api.post<AutomationTask>(`/automation/${taskId}/disable`)
  },

  runs: async (taskId: string, limit = 20) => {
    return api.get<AutomationRun[]>(`/automation/${taskId}/runs`, {
      params: { limit },
    })
  },
}
