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
}
