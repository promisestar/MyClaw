import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { workspaceApi } from '@/api/workspace'

/**
 * 工作区状态管理 store
 *
 * 管理当前工作区与已授权工作区列表。切换工作区时调用后端 /workspace/switch，
 * 后端 bind_workspace 重绑工具根目录后，后续对话与文件操作都落在新工作区。
 */
export const useWorkspaceStore = defineStore('workspace', () => {
  /** 当前工作区绝对路径 */
  const current = ref('')
  /** 已授权工作区列表 */
  const history = ref<string[]>([])
  /** 切换中加载态 */
  const loading = ref(false)

  /** 当前工作区目录名（用于 UI 展示） */
  const currentName = computed(() => {
    if (!current.value) return '未选择工作区'
    const parts = current.value.split(/[/\\]/)
    return parts[parts.length - 1] || current.value
  })

  /** 初始化：拉取已授权工作区列表与当前工作区 */
  async function init() {
    try {
      const res = await workspaceApi.list()
      history.value = res.workspaces || []
      current.value = res.current || ''
    } catch {
      // 静默失败（后端可能未就绪）
    }
  }

  /** 切换到已授权工作区 */
  async function select(path: string) {
    loading.value = true
    try {
      const res = await workspaceApi.switch(path)
      // 使用后端返回的规范化绝对路径，避免本地原始输入与白名单不一致
      current.value = res.workspace
      if (!history.value.includes(res.workspace)) {
        history.value.unshift(res.workspace)
      }
    } finally {
      loading.value = false
    }
  }

  /** 授权新工作区（不自动切换），返回后端规范化的绝对路径 */
  async function authorize(path: string): Promise<string> {
    const res = await workspaceApi.authorize(path)
    if (!history.value.includes(res.workspace)) {
      history.value.unshift(res.workspace)
    }
    return res.workspace
  }

  /** 授权并切换到新工作区 */
  async function authorizeAndSelect(path: string) {
    const normalized = await authorize(path)
    try {
      await select(normalized)
    } catch (e) {
      // 授权成功但切换失败，从 history 中移除刚添加的路径保持 UI 一致
      const idx = history.value.indexOf(normalized)
      if (idx >= 0) history.value.splice(idx, 1)
      throw e
    }
  }

  return {
    current,
    currentName,
    history,
    loading,
    init,
    select,
    authorize,
    authorizeAndSelect,
  }
})
