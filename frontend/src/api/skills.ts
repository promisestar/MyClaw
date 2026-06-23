import api from './index'

export interface SkillInfo {
  name: string
  description: string
  enabled: boolean
  dir: string
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
    return api.put(`/skills/${encodeURIComponent(name)}/content`, { content })
  },

  toggle: async (name: string) => {
    return api.post<{ message: string; enabled: boolean }>(`/skills/${encodeURIComponent(name)}/toggle`)
  },

  delete: async (name: string) => {
    return api.delete(`/skills/${encodeURIComponent(name)}`)
  },

  import: async (sourceType: 'path' | 'git', source: string) => {
    return api.post<{ message: string; skill?: SkillInfo }>('/skills/import', {
      source_type: sourceType,
      source,
    })
  },
}
