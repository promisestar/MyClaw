// 工具显示配置
export interface ToolDisplayConfig {
  name: string         // 友好名称
  icon: string         // emoji 图标
  hidden?: boolean     // 是否隐藏
}

export const TOOL_DISPLAY_CONFIG: Record<string, ToolDisplayConfig> = {
  // 内置工具 - 隐藏
  Thought: { name: '思考', icon: '💭', hidden: true },
  Finish: { name: '完成', icon: '✅', hidden: true },

  // 文件操作工具（HelloAgents 内置）
  Read: { name: '读取文件', icon: '📄' },
  Write: { name: '写入文件', icon: '✏️' },
  Edit: { name: '编辑文件', icon: '📝' },
  MultiEdit: { name: '批量编辑', icon: '📝' },

  // 计算工具
  python_calculator: { name: '计算器', icon: '🔢' },

  // 记忆工具（HelloClaw 自定义）
  memory: { name: '记忆操作', icon: '🧠' },
  memory_search: { name: '搜索记忆', icon: '🔍' },
  memory_get: { name: '读取记忆', icon: '📖' },
  memory_add: { name: '添加记忆', icon: '📝' },
  memory_update_longterm: { name: '更新长期记忆', icon: '📚' },
  memory_list: { name: '列出记忆文件', icon: '📋' },
  memory_cleanup: { name: '清理过期记忆', icon: '🧹' },

  // 任务工具
  Task: { name: '子任务', icon: '📋' },

  // 命令执行工具
  execute_command: { name: '执行命令', icon: '💻' },

  // 网络工具
  web_search: { name: '网络搜索', icon: '🌐' },
  search_web: { name: '网络搜索', icon: '🌐' },
  web_fetch: { name: '获取网页', icon: '📡' },
  fetch_url: { name: '获取网页', icon: '📡' },
}

// 默认配置（未知工具）
export const DEFAULT_TOOL_CONFIG: ToolDisplayConfig = {
  name: '工具',
  icon: '🔧',
}

// 获取工具显示配置
export function getToolConfig(toolName: string): ToolDisplayConfig {
  return TOOL_DISPLAY_CONFIG[toolName] || DEFAULT_TOOL_CONFIG
}

// 格式化工具参数显示
export function formatToolArgs(args: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) {
    return ''
  }

  const parts: string[] = []
  for (const [key, value] of Object.entries(args)) {
    let displayValue: string
    if (typeof value === 'string') {
      // 截断长字符串
      displayValue = value.length > 100 ? value.slice(0, 100) + '...' : value
    } else if (typeof value === 'object') {
      displayValue = JSON.stringify(value)
      if (displayValue.length > 100) {
        displayValue = displayValue.slice(0, 100) + '...'
      }
    } else {
      displayValue = String(value)
    }
    parts.push(`${key}: ${displayValue}`)
  }
  return parts.join('\n')
}

// 格式化工具结果显示
export function formatToolResult(result: string | undefined): string {
  if (!result) return ''
  // 截断长结果
  return result.length > 500 ? result.slice(0, 500) + '...' : result
}
