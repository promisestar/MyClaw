import api from './index'

/** 记忆分类 */
export type MemoryCategory =
  | 'preference'   // 偏好
  | 'decision'     // 决策
  | 'entity'       // 实体
  | 'fact'         // 事实
  | 'plan'         // 计划
  | 'relationship' // 关系
  | 'reference'    // 引用
  | 'rule'         // 规则

export interface MemoryEntry {
  id: string
  content: string
  category: MemoryCategory | string
  /** Unix 秒级时间戳 */
  timestamp: number
  source: string
}

export interface MemoryListResponse {
  memories: MemoryEntry[]
  total: number
}

export interface MemoryStatsResponse {
  total_count: number
  categories: Record<string, number>
}

export interface MemoryCaptureResponse {
  status: string
  message: string
  category: string
}

export interface MemoryCleanupResponse {
  status: string
  deleted: number
  message: string
}

export interface MemoryListParams {
  keyword?: string
  category?: string
  top_k?: number
}

export const memoryApi = {
  list: async (params: MemoryListParams = {}) => {
    return api.get<MemoryListResponse>('/memory/list', { params })
  },

  stats: async () => {
    return api.get<MemoryStatsResponse>('/memory/stats')
  },

  capture: async (content: string, category: MemoryCategory) => {
    return api.post<MemoryCaptureResponse>('/memory/capture', { content, category })
  },

  cleanup: async () => {
    return api.post<MemoryCleanupResponse>('/memory/cleanup')
  },
}
