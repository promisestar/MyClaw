import api from './index'

export interface SkillInfo {
  name: string
  description: string
  enabled: boolean
  dir: string
  /** 来源：global（跨工作区共享）/ workspace（项目专属） */
  source?: string
  has_venv?: boolean
  has_dependencies?: boolean
  python_path?: string | null
  use_count?: number
  patch_count?: number
  pinned?: boolean
  curator_managed?: boolean
  lifecycle_state?: string
}

export interface InstallEnvResponse {
  success: boolean
  message: string
  python_path?: string | null
  log: string
}

export interface SkillListResponse {
  skills: SkillInfo[]
  total: number
  enabled_count: number
}

export interface SkillDetail {
  name: string
  description: string
  body: string
  enabled: boolean
  dir: string
}

export interface SkillContent {
  name: string
  content: string
}

export interface SkillContentUpdateResponse {
  message: string
  name: string
  renamed: boolean
}

export const skillsApi = {
  list: async () => {
    return api.get<SkillListResponse>('/skills')
  },

  get: async (name: string) => {
    return api.get<SkillDetail>(`/skills/${encodeURIComponent(name)}`)
  },

  getContent: async (name: string) => {
    return api.get<SkillContent>(`/skills/${encodeURIComponent(name)}/content`)
  },

  updateContent: async (name: string, content: string) => {
    return api.put<SkillContentUpdateResponse>(
      `/skills/${encodeURIComponent(name)}/content`,
      { content },
    )
  },

  toggle: async (name: string) => {
    return api.post<{ message: string; enabled: boolean }>(`/skills/${encodeURIComponent(name)}/toggle`)
  },

  delete: async (name: string) => {
    return api.delete(`/skills/${encodeURIComponent(name)}`)
  },

  import: async (sourceType: 'path' | 'git', source: string, scope: 'global' | 'workspace' = 'workspace') => {
    return api.post<{ message: string; skill?: SkillInfo }>('/skills/import', {
      source_type: sourceType,
      source,
      scope,
    })
  },

  installEnv: async (name: string) => {
    return api.post<InstallEnvResponse>(`/skills/${encodeURIComponent(name)}/install-env`)
  },

  usage: async () => {
    return api.get<{ usage: Array<Record<string, unknown>> }>('/skills/usage')
  },

  archived: async (scope: 'global' | 'workspace' = 'workspace') => {
    return api.get<{ archived: string[]; scope: string }>(`/skills/archived?scope=${scope}`)
  },

  pin: async (name: string) => {
    return api.post<{ message: string; pinned: boolean }>(`/skills/${encodeURIComponent(name)}/pin`)
  },

  unpin: async (name: string) => {
    return api.post<{ message: string; pinned: boolean }>(`/skills/${encodeURIComponent(name)}/unpin`)
  },

  adopt: async (name: string) => {
    return api.post<{ message: string; curator_managed: boolean }>(
      `/skills/${encodeURIComponent(name)}/adopt`,
    )
  },

  archive: async (name: string) => {
    return api.post<{ message: string; archived_to: string }>(
      `/skills/${encodeURIComponent(name)}/archive`,
    )
  },

  restore: async (name: string, scope: 'global' | 'workspace' = 'workspace') => {
    return api.post<{ message: string; restored_to: string }>(
      `/skills/${encodeURIComponent(name)}/restore`,
      { scope },
    )
  },

  curatorStatus: async () => {
    return api.get<Record<string, unknown>>('/skills/curator/status')
  },

  curatorRun: async (force = true, dry_run = false) => {
    return api.post<Record<string, unknown>>('/skills/curator/run', { force, dry_run })
  },

  curatorPause: async () => {
    return api.post<{ paused: boolean }>('/skills/curator/pause')
  },

  curatorResume: async () => {
    return api.post<{ paused: boolean }>('/skills/curator/resume')
  },
}
