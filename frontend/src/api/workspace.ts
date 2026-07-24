import api from './index'

export interface WorkspaceListResponse {
  workspaces: string[]
  current: string
}

export interface WorkspaceSwitchResponse {
  status: string
  workspace: string
}

export const workspaceApi = {
  /** 列出所有已授权工作区及当前工作区 */
  list: async () => {
    return api.get<WorkspaceListResponse>('/workspace/list')
  },

  /** 切换到指定工作区（须已授权） */
  switch: async (path: string) => {
    return api.post<WorkspaceSwitchResponse>('/workspace/switch', { path })
  },

  /** 授权一个新工作区 */
  authorize: async (path: string) => {
    return api.post<{ status: string; workspace: string }>('/workspace/authorize', { path })
  },

  /** 撤销一个工作区授权 */
  revoke: async (path: string) => {
    return api.delete<{ status: string }>('/workspace/revoke', { data: { path } })
  },

  /** 弹出系统原生文件夹选择对话框（需后端运行在本地机器上） */
  pickFolder: async () => {
    return api.post<{ path: string | null; cancelled: boolean }>('/workspace/pick_folder', {})
  },
}
