<script setup lang="ts">
import { ref, watch, computed, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { Input, Button, message, Tag, Tooltip, Modal, Progress } from 'ant-design-vue'
import {
  SendOutlined,
  PlusOutlined,
  StopOutlined,
  LoadingOutlined,
  UploadOutlined,
  EditOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { useRouter, useRoute } from 'vue-router'
import { sessionApi, type ContextUsage } from '@/api/session'
import { chatApi, type ChatAttachment } from '@/api/chat'
import { configApi } from '@/api/config'
import { uploadApi, type UploadResponse } from '@/api/upload'
import { renderMarkdown, formatTime } from '@/utils/markdown'
import { getToolConfig, formatToolArgs, formatToolResult } from '@/utils/toolDisplay'
import { skillsApi, type SkillInfo } from '@/api/skills'
import AttachmentChip from '@/components/AttachmentChip.vue'
import UserMessageContent from '@/components/UserMessageContent.vue'
import LobsterIcon from '@/assets/lobster.svg'

// 单文档大小硬上限（与后端 MULTIMODAL_DOC_MAX_BYTES 保持一致；超过即拒绝，不发起上传请求）
const DOC_MAX_BYTES = 10 * 1024 * 1024
// 单图片大小上限（与后端 MULTIMODAL_MAX_IMAGE_MB 保持一致的默认 10MB）
const IMAGE_MAX_BYTES = 10 * 1024 * 1024
const IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tiff', 'tif']
const DOC_EXTS = [
  'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
  'txt', 'md', 'markdown', 'csv', 'tsv',
  'json', 'xml', 'yaml', 'yml', 'toml',
  'html', 'htm', 'log', 'conf', 'ini', 'cfg',
]

function classifyKindByName(filename: string): 'image' | 'doc' | 'other' {
  const m = /\.([a-z0-9]+)$/i.exec(filename)
  const ext = (m?.[1] || '').toLowerCase()
  if (IMAGE_EXTS.includes(ext)) return 'image'
  if (DOC_EXTS.includes(ext)) return 'doc'
  return 'other'
}

interface PendingAttachment {
  /** 本地 id，用于 v-for key */
  id: string
  /** 后端返回的 stored_path（相对工作空间根） */
  storedPath: string
  filename: string
  size: number
  mimeType: string
  kind: 'image' | 'doc' | 'other'
  /** 本地 ObjectURL（仅 kind=image 时存在；用于预览） */
  previewUrl?: string
}

// localStorage key for saving current session
const SESSION_STORAGE_KEY = 'helloclaw.lastSessionId'

// 助手名字（从后端获取）
const assistantName = ref('HelloClaw')

// 消息段类型
interface TextSegment {
  type: 'text'
  id: number
  content: string
}

interface ToolSegment {
  type: 'tool'
  id: number
  tool: string
  args: Record<string, unknown>
  result?: string
  status: 'running' | 'done' | 'error'
}

type MessageSegment = TextSegment | ToolSegment

/** 单条用户消息携带的附件展示信息（来自历史或刚发送时的快照） */
interface MessageAttachment {
  filename: string
  kind: 'image' | 'doc' | 'other'
  storedPath?: string
  size?: number
  /** 仅 kind=image 时存在：可能是后端返回的 url，也可能是本地 ObjectURL */
  imageUrl?: string
}

interface Message {
  id: number
  role: 'user' | 'assistant'
  content: string  // 用于从历史加载的消息
  timestamp: Date
  segments?: MessageSegment[]  // 用于流式消息的分段
  /** 会话中第几条用户消息（0 起），用于编辑/重新生成 */
  userTurnIndex?: number
  /** 用户消息携带的附件（仅 role=user 时存在） */
  attachments?: MessageAttachment[]
}

interface MessageGroup {
  role: 'user' | 'assistant'
  messages: Message[]
}

const router = useRouter()
const route = useRoute()
const inputMessage = ref('')
const messages = ref<Message[]>([])
const loading = ref(false)
const currentSessionId = ref<string | null>(null)
const messagesContainer = ref<HTMLElement | null>(null)
const abortController = ref<AbortController | null>(null)
const initializing = ref(true)
const fileInputRef = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const editModalOpen = ref(false)
const editDraft = ref('')
const editingUserTurnIndex = ref<number | null>(null)
/** 编辑时剥离出的 `<file>...</file>` 原文，提交时原样拼回，保证文档内容不丢失 */
const editStrippedFileBlocks = ref('')

/** 与 UserMessageContent 一致的正则：用于剥离 user content 中的 file 块 */
const USER_FILE_BLOCK_RE = /<file\s+name="[^"]*"\s+kind="[^"]*">[\s\S]*?<\/file>/g

/** 把 user content 拆为 { userText, fileBlocks }；fileBlocks 已用 `\n\n` 连接好 */
function splitUserContent(content: string): { userText: string; fileBlocks: string } {
  if (!content) return { userText: '', fileBlocks: '' }
  const blocks: string[] = []
  const re = new RegExp(USER_FILE_BLOCK_RE.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(content)) !== null) {
    blocks.push(m[0])
  }
  const userText = content.replace(re, '').replace(/\n{3,}/g, '\n\n').trim()
  return { userText, fileBlocks: blocks.join('\n\n') }
}

// 技能触发相关状态
const skillSuggestions = ref<SkillInfo[]>([])
const skillDropdownVisible = ref(false)
const skillSelectedIndex = ref(-1)

const showSkillDropdown = () => {
  const text = inputMessage.value
  return text === '/' || /^\/[^\s]*$/.test(text)
}

const filterSkills = async () => {
  const text = inputMessage.value
  if (!text.startsWith('/')) {
    skillDropdownVisible.value = false
    return
  }
  const query = text.slice(1).toLowerCase()
  try {
    const res = await skillsApi.list()
    skillSuggestions.value = res.skills.filter(
      (s) => s.enabled && (
        !query || s.name.toLowerCase().includes(query) || s.description.toLowerCase().includes(query)
      )
    )
    skillDropdownVisible.value = skillSuggestions.value.length > 0
    skillSelectedIndex.value = -1
  } catch {
    skillDropdownVisible.value = false
  }
}

const selectSkill = (skill: SkillInfo) => {
  inputMessage.value = `/${skill.name} `
  skillDropdownVisible.value = false
}

const handleSkillKeydown = (e: KeyboardEvent) => {
  if (!skillDropdownVisible.value) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    skillSelectedIndex.value = Math.min(
      skillSelectedIndex.value + 1,
      skillSuggestions.value.length - 1
    )
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    skillSelectedIndex.value = Math.max(skillSelectedIndex.value - 1, -1)
  } else if (e.key === 'Enter' && skillSelectedIndex.value >= 0) {
    e.preventDefault()
    const selected = skillSuggestions.value[skillSelectedIndex.value]
    if (selected) selectSkill(selected)
  } else if (e.key === 'Escape') {
    skillDropdownVisible.value = false
  } else if (e.key === 'Enter') {
    // Enter without selection: submit as normal
    skillDropdownVisible.value = false
    sendMessage()
  }
}

const defaultContextUsage = (): ContextUsage => ({
  session_id: null,
  context_window: 128000,
  used_tokens: 0,
  system_tokens: 0,
  history_tokens: 0,
  used_percent: 0,
})

const contextUsage = ref<ContextUsage>(defaultContextUsage())

const contextUsagePercent = computed(() =>
  Math.min(100, Math.max(0, contextUsage.value.used_percent))
)

const contextUsageTooltip = computed(() => {
  const u = contextUsage.value
  const pct = u.used_percent.toFixed(2)
  return `${pct}% context used (${u.used_tokens.toLocaleString()} / ${u.context_window.toLocaleString()} tokens)`
})

const contextStrokeColor = computed(() => {
  const p = contextUsagePercent.value
  if (p >= 90) return '#ff4d4f'
  if (p >= 70) return '#faad14'
  return '#52c41a'
})

/** 从服务端拉取上下文用量（仅用于进入会话/切换会话时初始化，对话中由 SSE done 推送更新） */
const refreshContextUsage = async () => {
  const sid = currentSessionId.value
  if (!sid) {
    contextUsage.value = defaultContextUsage()
    return
  }
  try {
    contextUsage.value = await sessionApi.getContextUsage(sid)
  } catch {
    // 忽略失败，避免打断页面
  }
}

const applyContextUsage = (usage: ContextUsage | undefined) => {
  if (usage) {
    contextUsage.value = usage
  }
}

/** 多模态附件：聊天框旁上传图片/文档（单文档 ≤10MB），发送时随消息一并提交 */
const UPLOAD_TOOLTIP =
  '上传图片或文档（单文档 ≤10MB）。图片走视觉模型识别，文档自动抽取文本注入到本轮对话。'

/** 当前等待发送的附件列表 */
const pendingAttachments = ref<PendingAttachment[]>([])

const triggerFileSelect = () => {
  if (uploading.value || loading.value) return
  fileInputRef.value?.click()
}

/** 在调用上传 API 前的前端预校验：大小 + 类型；返回原因或 null（合法） */
function precheckFile(file: File): string | null {
  const kind = classifyKindByName(file.name)
  if (kind === 'doc' && file.size > DOC_MAX_BYTES) {
    return `文档大小 ${(file.size / 1024 / 1024).toFixed(1)}MB 超过 10MB 上限`
  }
  if (kind === 'image' && file.size > IMAGE_MAX_BYTES) {
    return `图片大小 ${(file.size / 1024 / 1024).toFixed(1)}MB 超过 10MB 上限`
  }
  return null
}

function uploadResponseToPending(res: UploadResponse, localFile: File): PendingAttachment {
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    storedPath: res.stored_path,
    filename: res.filename,
    size: res.size,
    mimeType: res.mime_type,
    kind: res.kind,
    previewUrl: res.kind === 'image' ? URL.createObjectURL(localFile) : undefined,
  }
}

function removePendingAttachment(id: string) {
  const idx = pendingAttachments.value.findIndex(a => a.id === id)
  if (idx >= 0) {
    const att = pendingAttachments.value[idx]
    if (att?.previewUrl) {
      try { URL.revokeObjectURL(att.previewUrl) } catch { /* ignore */ }
    }
    pendingAttachments.value.splice(idx, 1)
  }
}

function clearPendingAttachments() {
  for (const a of pendingAttachments.value) {
    if (a.previewUrl) {
      try { URL.revokeObjectURL(a.previewUrl) } catch { /* ignore */ }
    }
  }
  pendingAttachments.value = []
}

async function uploadFilesAsAttachments(files: File[]) {
  if (!files.length) return
  uploading.value = true
  try {
    for (const file of files) {
      const err = precheckFile(file)
      if (err) {
        message.warning(`${file.name}：${err}`)
        continue
      }
      try {
        const res = await uploadApi.uploadFile(file, currentSessionId.value)
        pendingAttachments.value.push(uploadResponseToPending(res, file))
      } catch (err2) {
        const text = err2 instanceof Error ? err2.message : String(err2)
        message.error(`${file.name} 上传失败：${text}`)
      }
    }
  } finally {
    uploading.value = false
  }
}

const onFileInputChange = async (e: Event) => {
  const el = e.target as HTMLInputElement
  const files = el.files
  if (!files?.length) return
  await uploadFilesAsAttachments(Array.from(files))
  el.value = ''
}

/** 拖拽落区：聊天输入区接收文件 */
const dragOver = ref(false)
const onDropFiles = async (e: DragEvent) => {
  e.preventDefault()
  dragOver.value = false
  if (uploading.value || loading.value) return
  const files = Array.from(e.dataTransfer?.files || [])
  if (files.length) {
    await uploadFilesAsAttachments(files)
  }
}
const onDragOverInput = (e: DragEvent) => {
  if (uploading.value || loading.value) return
  e.preventDefault()
  dragOver.value = true
}
const onDragLeaveInput = () => {
  dragOver.value = false
}
const collapsedTools = ref<Set<number>>(new Set())
// 默认所有工具都是展开的（用于新建的工具）
const expandedTools = ref<Set<number>>(new Set())

// 消息分组（Slack 风格）
const messageGroups = computed<MessageGroup[]>(() => {
  const groups: MessageGroup[] = []

  for (const msg of messages.value) {
    const lastGroup = groups[groups.length - 1]

    if (lastGroup && lastGroup.role === msg.role) {
      lastGroup.messages.push(msg)
    } else {
      groups.push({
        role: msg.role,
        messages: [msg]
      })
    }
  }

  return groups
})

const lastAssistantGroupIndex = computed(() => {
  for (let i = messageGroups.value.length - 1; i >= 0; i--) {
    if (messageGroups.value[i]?.role === 'assistant') {
      return i
    }
  }
  return -1
})

// 是否应该显示加载指示器（底部的独立指示器）
// 只有当助手消息组完全没有可见内容时才显示
const shouldShowLoadingIndicator = computed(() => {
  if (messages.value.length === 0) {
    return true
  }

  const lastMsg = messages.value[messages.value.length - 1]
  if (lastMsg?.role !== 'assistant') {
    return true
  }

  // 只有当完全没有可见内容时才显示底部指示器
  // 如果有工具卡片等可见内容，等待状态会在消息组内部显示
  return !hasVisibleContent(lastMsg)
})

// 检查消息组是否有可见内容
const hasGroupVisibleContent = (group: MessageGroup): boolean => {
  for (const msg of group.messages) {
    if (hasVisibleContent(msg)) {
      return true
    }
  }
  return false
}

// 检查消息组是否有文本内容
const hasGroupTextContent = (group: MessageGroup): boolean => {
  for (const msg of group.messages) {
    if (hasTextContent(msg)) {
      return true
    }
  }
  return false
}

// 检查消息组是否有正在执行或已完成的工具（但还没有文本回复）
const hasGroupToolWithoutText = (group: MessageGroup): boolean => {
  if (group.role !== 'assistant') return false
  let hasTool = false
  let hasText = false
  for (const msg of group.messages) {
    if (!msg.segments) continue
    for (const segment of msg.segments) {
      if (segment.type === 'tool' && !getToolConfig(segment.tool).hidden) {
        hasTool = true
      }
      if (segment.type === 'text' && segment.content && segment.content.trim()) {
        hasText = true
      }
    }
  }
  return hasTool && !hasText
}

// 保存当前会话 ID 到 localStorage
const saveCurrentSession = (sessionId: string) => {
  localStorage.setItem(SESSION_STORAGE_KEY, sessionId)
}

// 从 localStorage 读取上次会话 ID
const getLastSession = (): string | null => {
  return localStorage.getItem(SESSION_STORAGE_KEY)
}

// 加载会话历史（按照 OpenAI 标准格式解析）
const loadSessionHistory = async (sessionId: string) => {
  try {
    const res = await sessionApi.getHistory(sessionId)
    const rawMessages = res.messages

    // 用于存储工具调用结果（tool_call_id -> result）
    const toolResults: Map<string, string> = new Map()

    // 第一遍：收集所有 tool 消息的结果
    for (const msg of rawMessages) {
      if (msg.role === 'tool' && msg.tool_call_id && msg.content) {
        toolResults.set(msg.tool_call_id, msg.content)
      }
    }

    // 第二遍：构建显示消息
    const displayMessages: Message[] = []
    let pendingAssistant: Message | null = null
    let userTurnCounter = 0

    for (let i = 0; i < rawMessages.length; i++) {
      const msg = rawMessages[i]!

      if (msg.role === 'user') {
        // 如果有待处理的 assistant 消息，先添加
        if (pendingAssistant) {
          displayMessages.push(pendingAssistant)
          pendingAssistant = null
        }
        // 把后端返回的附件元数据（仅图片）转为 UI MessageAttachment
        const restoredAtts: MessageAttachment[] = (msg.attachments || [])
          .filter(a => a.kind === 'image' && a.url)
          .map(a => ({
            filename: '',  // 历史中未保存文件名，气泡渲染时 fallback 到"图片"
            kind: 'image',
            imageUrl: a.url,
          }))
        // 添加 user 消息
        displayMessages.push({
          id: Date.now() + i,
          role: 'user',
          content: msg.content || '',
          timestamp: new Date(),
          userTurnIndex: userTurnCounter,
          attachments: restoredAtts.length > 0 ? restoredAtts : undefined,
        })
        userTurnCounter += 1
      }
      else if (msg.role === 'assistant') {
        if (msg.tool_calls && msg.tool_calls.length > 0) {
          // 包含工具调用的 assistant 消息
          const segments: MessageSegment[] = []

          // 添加工具调用段
          msg.tool_calls.forEach((tc, tcIndex) => {
            const result = toolResults.get(tc.id)
            segments.push({
              type: 'tool',
              id: Date.now() + i * 1000 + tcIndex,
              tool: tc.function.name,
              args: JSON.parse(tc.function.arguments || '{}'),
              result: result,
              status: result?.startsWith('❌') ? 'error' : 'done'
            })
          })

          // 检查下一个消息是否是最终的 assistant 回答（没有 tool_calls）
          const nextMsg = rawMessages[i + 1]
          if (nextMsg && nextMsg.role === 'assistant' && !nextMsg.tool_calls && nextMsg.content) {
            // 有最终回答，添加文本段
            segments.push({
              type: 'text',
              id: Date.now() + i * 1000 + 100,
              content: nextMsg.content
            })
            i++ // 跳过下一个消息
          }

          pendingAssistant = {
            id: Date.now() + i,
            role: 'assistant',
            content: '',
            timestamp: new Date(),
            segments
          }
        } else if (msg.content) {
          // 普通的 assistant 文本消息
          if (pendingAssistant) {
            // 追加到待处理的 assistant 消息
            if (!pendingAssistant.segments) {
              pendingAssistant.segments = []
            }
            pendingAssistant.segments.push({
              type: 'text',
              id: Date.now() + i,
              content: msg.content
            })
          } else {
            // 新的 assistant 消息
            displayMessages.push({
              id: Date.now() + i,
              role: 'assistant',
              content: msg.content,
              timestamp: new Date()
            })
          }
        }
      }
      // tool 消息在第一遍已经处理，跳过
    }

    // 添加最后的待处理消息
    if (pendingAssistant) {
      displayMessages.push(pendingAssistant)
    }

    messages.value = displayMessages
    await scrollToBottom()
    await refreshContextUsage()
  } catch (error) {
    // 会话不存在或加载失败，清空消息
    messages.value = []
    await refreshContextUsage()
  }
}

// 初始化会话
const initSession = async () => {
  // 获取助手名字
  try {
    const agentInfo = await configApi.getAgentInfo()
    if (agentInfo.name) {
      assistantName.value = agentInfo.name
    }
  } catch (error) {
    // 获取失败时使用默认名字
    console.warn('获取助手名字失败:', error)
  }

  const urlSession = route.query.session as string | undefined

  if (urlSession) {
    // URL 中有 session 参数，使用它
    currentSessionId.value = urlSession
    saveCurrentSession(urlSession)
    await loadSessionHistory(urlSession)
    initializing.value = false
  } else {
    // URL 中没有 session 参数，尝试从 localStorage 读取
    const lastSession = getLastSession()
    if (lastSession) {
      // 有上次会话，设置 session 并加载历史，然后更新 URL
      currentSessionId.value = lastSession
      saveCurrentSession(lastSession)
      await loadSessionHistory(lastSession)
      // 使用 replace 更新 URL（不触发导航）
      window.history.replaceState({}, '', `/?session=${lastSession}`)
      initializing.value = false
    } else {
      // 没有上次会话，创建新会话
      try {
        const res = await sessionApi.create()
        saveCurrentSession(res.session_id)
        currentSessionId.value = res.session_id
        // 使用 replace 更新 URL（不触发导航）
        window.history.replaceState({}, '', `/?session=${res.session_id}`)
        initializing.value = false
      } catch (error) {
        message.error('创建会话失败')
        initializing.value = false
      }
    }
  }
}

// 监听 session 参数变化（处理从其他地方跳转过来的情况）
watch(
  () => route.query.session,
  async (newSession, oldSession) => {
    // 如果正在初始化，跳过
    if (initializing.value) return

    // 如果 session 没有实际变化，跳过
    if (newSession === oldSession) return

    const sessionId = (newSession as string) || null

    // 如果新 session 为空，不做处理（应该由 initSession 处理）
    if (!sessionId) return

    // 切换到新会话
    currentSessionId.value = sessionId
    saveCurrentSession(sessionId)
    inputMessage.value = ''
    await loadSessionHistory(sessionId)
  }
)

// 监听 refresh 参数变化（处理从配置页面初始化后跳转的情况）
watch(
  () => route.query.refresh,
  async (newRefresh) => {
    if (newRefresh) {
      // 重新获取助手名字
      try {
        const agentInfo = await configApi.getAgentInfo()
        if (agentInfo.name) {
          assistantName.value = agentInfo.name
        }
      } catch (error) {
        console.warn('获取助手名字失败:', error)
      }

      // 清除 URL 中的 refresh 参数
      const currentQuery = { ...route.query }
      delete currentQuery.refresh
      router.replace({ query: currentQuery })
    }
  }
)

// 监听 doc 参数（从知识库页面跳转，将文档路径插入输入框）
watch(
  () => route.query.doc,
  (newDoc) => {
    if (newDoc && typeof newDoc === 'string' && newDoc.trim()) {
      const docPath = newDoc.trim()
      const fileName = docPath.replace(/\\/g, '/').split('/').pop() || docPath
      const line = `[知识库文档: ${fileName}](${docPath})`
      if (inputMessage.value.trim()) {
        inputMessage.value = `${inputMessage.value.trim()}\n${line}`
      } else {
        inputMessage.value = line
      }
      // 清除 URL 中的 doc 参数
      const currentQuery = { ...route.query }
      delete currentQuery.doc
      router.replace({ query: currentQuery })
    }
  },
  { immediate: true }
)

// 监听输入框内容，触发技能下拉
watch(
  () => inputMessage.value,
  () => {
    if (showSkillDropdown()) {
      filterSkills()
    } else {
      skillDropdownVisible.value = false
    }
  }
)

// 组件挂载时初始化会话
onMounted(async () => {
  await initSession()
  await scrollToBottom()
  await refreshContextUsage()
})

// 组件卸载：清理未发送附件的 ObjectURL
onBeforeUnmount(() => {
  clearPendingAttachments()
})

watch(currentSessionId, () => {
  void refreshContextUsage()
})

// 滚动到底部
const scrollToBottom = async () => {
  await nextTick()
  if (messagesContainer.value) {
    messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
  }
}

// 切换工具折叠状态
const toggleToolCollapse = (toolId: number) => {
  if (expandedTools.value.has(toolId)) {
    expandedTools.value.delete(toolId)
  } else {
    expandedTools.value.add(toolId)
  }
}

// 检查工具是否展开（默认折叠，只有点击后才展开）
const isToolExpanded = (toolId: number): boolean => {
  return expandedTools.value.has(toolId)
}

// 检查消息是否有可见内容
const hasVisibleContent = (msg: Message): boolean => {
  if (!msg.segments || msg.segments.length === 0) {
    // 没有分段，检查普通内容
    return !!msg.content
  }

  // 有分段，检查是否有可见的段
  for (const segment of msg.segments) {
    if (segment.type === 'text' && segment.content) {
      return true
    }
    if (segment.type === 'tool' && !getToolConfig(segment.tool).hidden) {
      return true
    }
  }
  return false
}

// 检查消息是否有文本内容（用于决定是否显示加载指示器）
const hasTextContent = (msg: Message): boolean => {
  if (!msg.segments || msg.segments.length === 0) {
    return !!msg.content
  }
  // 只检查文本段
  for (const segment of msg.segments) {
    if (segment.type === 'text' && segment.content) {
      return true
    }
  }
  return false
}

// 检查消息是否有可见的工具调用（用于决定是否显示工具卡片而非加载指示器）
const hasVisibleTools = (msg: Message): boolean => {
  if (!msg.segments) return false
  for (const segment of msg.segments) {
    if (segment.type === 'tool' && !getToolConfig(segment.tool).hidden) {
      return true
    }
  }
  return false
}

// 检查消息组是否正在等待响应（用于隐藏 group-footer）
const isGroupWaiting = (group: MessageGroup): boolean => {
  if (group.role !== 'assistant' || !loading.value) return false
  // 检查组内所有消息是否都没有文本内容
  return group.messages.every(msg => !hasTextContent(msg))
}

// 停止生成
const stopGeneration = () => {
  if (abortController.value) {
    abortController.value.abort()
    abortController.value = null
    loading.value = false
  }
}

// 更新消息段（触发 Vue 响应性）
const updateMessageSegments = (msgIndex: number, segments: MessageSegment[]) => {
  if (msgIndex >= 0 && msgIndex < messages.value.length) {
    const existingMsg = messages.value[msgIndex]!
    messages.value[msgIndex] = {
      id: existingMsg.id,
      role: existingMsg.role,
      content: existingMsg.content,
      timestamp: existingMsg.timestamp,
      segments: [...segments]
    }
  }
}

const countUserMessages = () =>
  messages.value.filter(m => m.role === 'user').length

const findFirstMessageIndexByUserTurn = (turn: number) =>
  messages.value.findIndex(m => m.role === 'user' && m.userTurnIndex === turn)

/** 定位该用户轮次：保留此前消息 + 之后其他轮对话，仅替换中间旧回复 */
const splitMessagesAtUserTurn = (turn: number) => {
  const userIdx = findFirstMessageIndexByUserTurn(turn)
  if (userIdx < 0) return null

  let nextUserIdx = messages.value.length
  for (let i = userIdx + 1; i < messages.value.length; i++) {
    if (messages.value[i]?.role === 'user') {
      nextUserIdx = i
      break
    }
  }

  return {
    userIdx,
    assistantInsertAt: userIdx + 1,
    prefix: messages.value.slice(0, userIdx),
    suffix: messages.value.slice(nextUserIdx),
  }
}

const replaceUserTurnInUi = (
  turn: number,
  newContent: string,
  attachments?: MessageAttachment[],
) => {
  const split = splitMessagesAtUserTurn(turn)
  if (!split) return null

  const userMsg: Message = {
    id: Date.now(),
    role: 'user',
    content: newContent,
    timestamp: new Date(),
    userTurnIndex: turn,
    attachments: attachments && attachments.length > 0 ? attachments : undefined,
  }

  messages.value = [...split.prefix, userMsg, ...split.suffix]
  return split.assistantInsertAt
}

const getLastUserMessage = (): Message | undefined => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const m = messages.value[i]
    if (m?.role === 'user') return m
  }
  return undefined
}

const openEditUserMessage = (msg: Message) => {
  if (loading.value || msg.userTurnIndex === undefined) return
  editingUserTurnIndex.value = msg.userTurnIndex
  // 编辑框只显示用户原文，剥离出的文档块（<file>...</file>）保留下来，
  // 提交时原样拼回，确保文档内容在重发时不丢失。
  const { userText, fileBlocks } = splitUserContent(msg.content || '')
  editDraft.value = userText
  editStrippedFileBlocks.value = fileBlocks
  editModalOpen.value = true
}

/** 从历史 Message 的 attachments 还原回 PendingAttachment（仅保留可访问字段） */
function messageAttachmentsToPending(atts?: MessageAttachment[]): PendingAttachment[] {
  if (!atts || atts.length === 0) return []
  return atts
    .filter(a => !!a.storedPath)
    .map<PendingAttachment>(a => ({
      id: `restore-${a.storedPath}-${Math.random().toString(36).slice(2, 6)}`,
      storedPath: a.storedPath!,
      filename: a.filename,
      size: a.size ?? 0,
      mimeType: '',
      kind: a.kind,
      previewUrl: a.imageUrl,
    }))
}

const submitEditUserMessage = async () => {
  const text = editDraft.value.trim()
  if (!text || editingUserTurnIndex.value === null) return
  editModalOpen.value = false
  const turn = editingUserTurnIndex.value
  editingUserTurnIndex.value = null
  // 找到原用户消息以回填附件
  const original = messages.value.find(m => m.role === 'user' && m.userTurnIndex === turn)
  const restoredAtts = messageAttachmentsToPending(original?.attachments)
  // 把剥离的文档块原样拼回，避免重发时丢失文档内容
  const fileBlocks = editStrippedFileBlocks.value
  editStrippedFileBlocks.value = ''
  const finalText = fileBlocks ? `${text}\n\n${fileBlocks}` : text
  await runChatRequest(finalText, {
    userTurnIndex: turn,
    regenerate: false,
    attachments: restoredAtts,
    skipAttachmentsClear: true,
  })
}

const regenerateLastResponse = async () => {
  const lastUser = getLastUserMessage()
  if (!lastUser || lastUser.userTurnIndex === undefined || loading.value) return
  const restoredAtts = messageAttachmentsToPending(lastUser.attachments)
  // 重发时原样使用历史 content（已包含 <file> 块），后端不会重复注入
  // （因为 restoredAtts 中只有图片，build_user_content 仅会重新构造 image_url part，
  //  文档全文以历史 content 文本形式透传给 LLM）
  await runChatRequest(lastUser.content, {
    userTurnIndex: lastUser.userTurnIndex,
    regenerate: true,
    attachments: restoredAtts,
    skipAttachmentsClear: true,
  })
}

interface ChatRequestOptions {
  userTurnIndex?: number
  regenerate?: boolean
  skipInputClear?: boolean
  skill?: string
  /** 显式传入附件（编辑/重发时回填用）；为空时使用 pendingAttachments */
  attachments?: PendingAttachment[]
  /** 跳过自动清空 pendingAttachments（编辑/重发场景不要清掉当前输入框中的附件） */
  skipAttachmentsClear?: boolean
}

const runChatRequest = async (userMessage: string, options: ChatRequestOptions = {}) => {
  // 允许「仅附件」无文字提交：当 options.attachments 或 pendingAttachments 非空时也放行
  const hasAtt = (options.attachments?.length || pendingAttachments.value.length) > 0
  if ((!userMessage.trim() && !hasAtt) || loading.value) return

  const isResend = options.userTurnIndex !== undefined
  let assistantInsertAt: number | undefined

  // 决定本轮使用的附件：显式传入优先，否则用输入框旁的 pending 列表
  const turnAttachments: PendingAttachment[] =
    options.attachments ?? pendingAttachments.value.slice()
  const attachmentsSnapshot: MessageAttachment[] = turnAttachments.map(a => ({
    filename: a.filename,
    kind: a.kind,
    storedPath: a.storedPath,
    size: a.size,
    imageUrl: a.previewUrl,
  }))

  if (isResend) {
    assistantInsertAt = replaceUserTurnInUi(options.userTurnIndex!, userMessage, attachmentsSnapshot) ?? undefined
  } else {
    const userTurnIndex = countUserMessages()
    messages.value.push({
      id: Date.now(),
      role: 'user',
      content: options.skill ? `/${options.skill} ${userMessage}` : userMessage,
      timestamp: new Date(),
      userTurnIndex,
      attachments: attachmentsSnapshot.length > 0 ? attachmentsSnapshot : undefined,
    })
  }

  if (!options.skipInputClear) {
    inputMessage.value = ''
  }
  if (!options.skipAttachmentsClear && options.attachments === undefined) {
    // 普通发送：本轮 pending 已转入消息快照，清空待发送区
    // 注意：不 revoke ObjectURL，因为消息气泡仍在引用；走单独的 onBeforeUnmount 兜底
    pendingAttachments.value = []
  }
  loading.value = true

  abortController.value = new AbortController()

  let assistantMsgIndex = -1
  let currentSegments: MessageSegment[] = []
  let currentTextSegmentId = -1

  const ensureAssistantMessage = () => {
    if (assistantMsgIndex !== -1) {
      updateMessageSegments(assistantMsgIndex, currentSegments)
      return
    }
    const newAssistant: Message = {
      id: Date.now(),
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      segments: currentSegments,
    }
    if (assistantInsertAt !== undefined) {
      assistantMsgIndex = assistantInsertAt
      messages.value.splice(assistantInsertAt, 0, newAssistant)
    } else {
      assistantMsgIndex = messages.value.length
      messages.value.push(newAssistant)
    }
  }

  await scrollToBottom()

  try {
    await chatApi.sendMessageStream(
      userMessage,
      (event) => {
        if (event.type === 'session') {
          // 收到会话 ID
          if (event.session_id) {
            currentSessionId.value = event.session_id
            saveCurrentSession(event.session_id)
          }
        } else if (event.type === 'step_start') {
          // 新步骤开始 - 创建新的文本段
          currentTextSegmentId = Date.now()
          currentSegments.push({
            type: 'text',
            id: currentTextSegmentId,
            content: ''
          })

          ensureAssistantMessage()
          scrollToBottom()
        } else if (event.type === 'chunk' && event.content) {
          // 更新当前文本段
          const textSegment = currentSegments.find(s => s.type === 'text' && s.id === currentTextSegmentId) as TextSegment | undefined
          if (textSegment) {
            textSegment.content += event.content
            updateMessageSegments(assistantMsgIndex, currentSegments)
          }
          scrollToBottom()
        } else if (event.type === 'tool_start') {
          // 工具调用开始 - 创建工具段
          currentSegments.push({
            type: 'tool',
            id: Date.now(),
            tool: event.tool || '',
            args: event.args || {},
            status: 'running'
          })

          ensureAssistantMessage()
          scrollToBottom()
        } else if (event.type === 'tool_finish') {
          // 工具调用结束 - 查找或创建工具段
          const lastToolSegment = [...currentSegments].reverse().find(s => s.type === 'tool' && s.status === 'running') as ToolSegment | undefined
          if (lastToolSegment) {
            // 更新现有的运行中工具
            lastToolSegment.result = event.result
            lastToolSegment.status = 'done'
          } else {
            // 没有对应的 tool_start，直接添加为完成的工具
            currentSegments.push({
              type: 'tool',
              id: Date.now(),
              tool: event.tool || '',
              args: {},
              result: event.result,
              status: 'done'
            })
          }
          updateMessageSegments(assistantMsgIndex, currentSegments)
          scrollToBottom()
        } else if (event.type === 'done') {
          // 完成
          if (event.session_id) {
            currentSessionId.value = event.session_id
          }
          applyContextUsage(event.context_usage)
          // 对话结束后重新获取助手名字（可能在对话中更新了 IDENTITY.md）
          configApi.getAgentInfo().then(agentInfo => {
            if (agentInfo.name) {
              assistantName.value = agentInfo.name
            }
          }).catch(() => {
            // 忽略错误，保持当前名字
          })
        } else if (event.type === 'error') {
          message.error(event.error || '发送消息失败')
        }
      },
      {
        sessionId: currentSessionId.value,
        userTurnIndex: options.userTurnIndex,
        regenerate: options.regenerate,
        skill: options.skill,
        attachments: turnAttachments.length > 0
          ? turnAttachments.map<ChatAttachment>(a => ({
              stored_path: a.storedPath,
              filename: a.filename,
              mime_type: a.mimeType,
              kind: a.kind,
              size: a.size,
            }))
          : undefined,
        signal: abortController.value.signal,
      }
    )

    await scrollToBottom()
  } catch (error: unknown) {
    if (error instanceof Error && error.name === 'AbortError') {
      console.log('用户取消了请求')
    } else {
      console.error('发送消息失败:', error)
      message.error('发送消息失败')
      if (currentSessionId.value) {
        await loadSessionHistory(currentSessionId.value)
      } else {
        const last = messages.value[messages.value.length - 1]
        if (last?.role === 'user') {
          messages.value.pop()
        }
      }
    }
  } finally {
    loading.value = false
    abortController.value = null
  }
}

const sendMessage = async () => {
  const text = inputMessage.value.trim()
  // 允许「无文字、仅附件」发送
  if (!text && pendingAttachments.value.length === 0) return
  const effectiveText = text || '请基于附件回答。'

  // 解析 /技能名 前缀
  const skillMatch = effectiveText.match(/^\/(\S+)\s+([\s\S]*)/)
  if (skillMatch) {
    const skillName = skillMatch[1]
    const userContent = skillMatch[2].trim()
    if (!userContent) {
      message.warning('请在技能后输入具体内容')
      return
    }
    await runChatRequest(userContent, { skill: skillName })
  } else {
    await runChatRequest(effectiveText)
  }
}

const createNewSession = async () => {
  try {
    const res = await sessionApi.create()
    saveCurrentSession(res.session_id)
    contextUsage.value = defaultContextUsage()
    router.push({ name: 'chat', query: { session: res.session_id } })
    await refreshContextUsage()
  } catch (error) {
    message.error('新建会话失败')
  }
}
</script>

<template>
  <div class="chat-view">
    <!-- 消息区域 -->
    <div class="chat-messages" ref="messagesContainer">
      <!-- 初始化加载状态 -->
      <div v-if="initializing" class="empty-state">
        <img :src="LobsterIcon" alt="HelloClaw" class="empty-icon loading" />
        <p class="empty-hint">加载中...</p>
      </div>
      <template v-else-if="messages.length > 0">
        <div
          v-for="(group, groupIndex) in messageGroups"
          :key="groupIndex"
          v-show="group.role !== 'assistant' || hasGroupVisibleContent(group)"
          :class="['message-group', group.role]"
        >
          <!-- 头像 -->
          <div class="group-avatar">
            <img v-if="group.role === 'assistant'" :src="LobsterIcon" alt="HelloClaw" />
            <div v-else class="user-avatar">你</div>
          </div>

          <!-- 消息内容 -->
          <div class="group-content">
            <!-- 遍历每条消息 -->
            <template v-for="(msg, msgIndex) in group.messages" :key="msg.id">
              <!-- 用户附件展示（在文本气泡之前） -->
              <div
                v-if="group.role === 'user' && msg.attachments && msg.attachments.length > 0"
                class="message-attachments"
              >
                <AttachmentChip
                  v-for="(att, attIdx) in msg.attachments"
                  :key="`${msg.id}-att-${attIdx}`"
                  :filename="att.filename || (att.kind === 'image' ? '图片' : '文件')"
                  :kind="att.kind"
                  :size="att.size"
                  :image-url="att.imageUrl"
                />
              </div>
              <!-- 如果有分段，按分段显示 -->
              <template v-if="msg.segments && msg.segments.length > 0">
                <template v-for="segment in msg.segments" :key="segment.id">
                  <!-- 文本段 -->
                  <div v-if="segment.type === 'text' && segment.content" class="message-bubble">
                    <div
                      class="message-text"
                      v-html="renderMarkdown(segment.content)"
                    ></div>
                  </div>
                  <!-- 工具调用段 - 只显示非隐藏的工具 -->
                  <div
                    v-if="segment.type === 'tool' && !getToolConfig(segment.tool).hidden"
                    :class="['tool-card', segment.status]"
                  >
                    <div
                      class="tool-header"
                      @click="segment.status !== 'running' && toggleToolCollapse(segment.id)"
                    >
                      <span class="tool-icon">{{ getToolConfig(segment.tool).icon }}</span>
                      <span class="tool-name">
                        <template v-if="!isToolExpanded(segment.id)">使用了</template>
                        {{ getToolConfig(segment.tool).name }}
                      </span>
                      <Tag v-if="segment.status === 'running'" color="processing" class="tool-tag">
                        <LoadingOutlined /> 执行中
                      </Tag>
                      <Tag v-else-if="segment.status === 'done'" color="success" class="tool-tag">完成</Tag>
                      <Tag v-else-if="segment.status === 'error'" color="error" class="tool-tag">失败</Tag>
                      <span
                        v-if="segment.status !== 'running'"
                        class="collapse-indicator"
                      >
                        {{ isToolExpanded(segment.id) ? '▼' : '▶' }}
                      </span>
                    </div>
                    <!-- 展开后显示入参和结果 -->
                    <div v-if="isToolExpanded(segment.id)" class="tool-details">
                      <!-- 入参 -->
                      <div v-if="segment.args && Object.keys(segment.args).length > 0" class="tool-args">
                        <div class="tool-detail-label">入参</div>
                        <pre class="tool-detail-content">{{ formatToolArgs(segment.args) }}</pre>
                      </div>
                      <!-- 结果 -->
                      <div v-if="segment.result" class="tool-result-wrapper">
                        <div class="tool-detail-label">结果</div>
                        <pre class="tool-detail-content">{{ formatToolResult(segment.result) }}</pre>
                      </div>
                    </div>
                  </div>
                </template>
              </template>
              <!-- 如果没有分段，显示普通内容（历史消息） -->
              <div
                v-else-if="msg.content"
                class="message-bubble"
                :class="{ 'user-editable': group.role === 'user' && !loading }"
                @click="group.role === 'user' && !loading && openEditUserMessage(msg)"
              >
                <!-- 用户消息：可能包含 <file>...</file> 文档块，需要折叠展示 -->
                <UserMessageContent
                  v-if="group.role === 'user'"
                  :content="msg.content"
                />
                <!-- 助手消息：直接渲染 markdown -->
                <div
                  v-else
                  class="message-text"
                  v-html="renderMarkdown(msg.content)"
                ></div>
                <div
                  v-if="group.role === 'user' && !loading"
                  class="message-edit-hint"
                  title="点击编辑并重新发送"
                >
                  <EditOutlined /> 编辑
                </div>
              </div>
            </template>

            <!-- 消息组内部的等待状态（有工具调用但没有文本回复时） -->
            <div v-if="loading && hasGroupToolWithoutText(group)" class="message-bubble">
              <div class="loading-dots">
                <span></span>
                <span></span>
                <span></span>
              </div>
            </div>

            <!-- 组底部：名称和时间（加载等待时隐藏） -->
            <div v-if="!isGroupWaiting(group)" class="group-footer">
              <span class="group-name">{{ group.role === 'user' ? '你' : assistantName }}</span>
              <span class="group-time">{{ formatTime(group.messages[group.messages.length - 1]?.timestamp || new Date()) }}</span>
              <button
                v-if="group.role === 'assistant' && !loading && groupIndex === lastAssistantGroupIndex"
                type="button"
                class="regenerate-btn"
                title="重新生成回答"
                @click.stop="regenerateLastResponse"
              >
                <ReloadOutlined /> 重新生成
              </button>
            </div>
          </div>
        </div>
      </template>

      <!-- 空状态 -->
      <div v-else class="empty-state">
        <img :src="LobsterIcon" alt="HelloClaw" class="empty-icon" />
        <p class="empty-hint">发送消息开始对话</p>
      </div>

      <!-- 加载指示器（助手消息组样式）- 等待响应时显示 -->
      <div v-if="loading && shouldShowLoadingIndicator" class="message-group assistant loading-group">
        <div class="group-avatar">
          <img :src="LobsterIcon" alt="HelloClaw" />
        </div>
        <div class="group-content">
          <div class="message-bubble">
            <div class="loading-dots">
              <span></span>
              <span></span>
              <span></span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 输入区域 -->
    <div class="chat-input-wrapper">
      <!-- 技能下拉选择器 -->
      <Transition name="skill-dropdown-fade">
        <div v-if="skillDropdownVisible" class="skill-dropdown">
          <div class="skill-dropdown-header">
            <span>选择技能</span>
            <span class="skill-dropdown-hint">↑↓ 选择 · Enter 确认 · Esc 取消</span>
          </div>
          <div class="skill-dropdown-list">
            <div
              v-for="(skill, index) in skillSuggestions"
              :key="skill.name"
              :class="['skill-dropdown-item', { active: index === skillSelectedIndex }]"
              @click="selectSkill(skill)"
              @mouseenter="skillSelectedIndex = index"
            >
              <div class="skill-dropdown-item-icon">⚡</div>
              <div class="skill-dropdown-item-content">
                <div class="skill-dropdown-item-name">/{{ skill.name }}</div>
                <div class="skill-dropdown-item-desc">{{ skill.description }}</div>
              </div>
            </div>
          </div>
        </div>
      </Transition>
      <!-- 附件预览条（待发送） -->
      <div
        v-if="pendingAttachments.length > 0"
        class="chat-attachments-preview"
      >
        <AttachmentChip
          v-for="att in pendingAttachments"
          :key="att.id"
          :filename="att.filename"
          :kind="att.kind"
          :size="att.size"
          :image-url="att.previewUrl"
          removable
          @remove="removePendingAttachment(att.id)"
        />
      </div>
      <div
        class="chat-input"
        :class="{ 'is-drag-over': dragOver }"
        @dragover="onDragOverInput"
        @dragleave="onDragLeaveInput"
        @drop="onDropFiles"
      >
        <input
          ref="fileInputRef"
          type="file"
          class="chat-file-input"
          multiple
          @change="onFileInputChange"
        />
        <Tooltip :title="UPLOAD_TOOLTIP" placement="top">
          <Button
            type="text"
            class="chat-input-attach"
            :disabled="loading || uploading"
            aria-label="上传文件"
            @click="triggerFileSelect"
          >
            <template #icon>
              <LoadingOutlined v-if="uploading" spin />
              <UploadOutlined v-else />
            </template>
          </Button>
        </Tooltip>
        <!-- 输入框 -->
        <Input.TextArea
          v-model:value="inputMessage"
          placeholder="输入 / 使用技能 (Enter 发送, Shift+Enter 换行)"
          :auto-size="{ minRows: 1, maxRows: 4 }"
          @press-enter="(e: KeyboardEvent) => { if (skillDropdownVisible.value) { handleSkillKeydown(e); return; } if (!e.shiftKey) { e.preventDefault(); sendMessage() } }"
          @keydown="handleSkillKeydown"
        />
        <!-- 按钮区域（固定宽度） -->
        <div class="input-actions">
          <Tooltip :title="contextUsageTooltip">
            <div class="context-usage-ring" aria-label="上下文使用情况">
              <Progress
                type="circle"
                :percent="contextUsagePercent"
                :size="34"
                :stroke-width="10"
                :show-info="false"
                :stroke-color="contextStrokeColor"
              />
            </div>
          </Tooltip>
          <!-- 新建会话按钮 -->
          <Button
            class="icon-btn"
            @click="createNewSession"
            title="新建会话"
          >
            <template #icon>
              <PlusOutlined />
            </template>
          </Button>
          <!-- 停止按钮（loading 时显示） -->
          <button
            v-if="loading"
            class="stop-btn"
            @click="stopGeneration"
            title="停止生成"
          >
            <div class="stop-icon"></div>
          </button>
          <!-- 发送按钮（有文字时显示） -->
          <button
            v-else-if="inputMessage.trim()"
            class="send-btn active"
            @click="sendMessage"
            title="发送消息"
          >
            <SendOutlined />
          </button>
        </div>
      </div>
    </div>

    <Modal
      v-model:open="editModalOpen"
      title="编辑消息"
      ok-text="发送"
      cancel-text="取消"
      :confirm-loading="loading"
      @ok="submitEditUserMessage"
      @cancel="() => { editingUserTurnIndex = null }"
    >
      <Input.TextArea
        v-model:value="editDraft"
        :auto-size="{ minRows: 3, maxRows: 12 }"
        placeholder="修改后重新发送，将仅替换该条消息对应的助手回复，后续对话会保留"
      />
    </Modal>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  box-sizing: border-box;
  background-color: var(--color-background);
  position: relative;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 消息组样式 */
.message-group {
  display: flex;
  gap: 12px;
  max-width: 85%;
}

.message-group.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.message-group.assistant {
  align-self: flex-start;
}

/* 头像 */
.group-avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
}

.group-avatar img {
  width: 36px;
  height: 36px;
  border-radius: 8px;
}

.user-avatar {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background-color: var(--color-primary);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* 消息组内容 */
.group-content {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

/* 消息气泡 */
.message-bubble {
  display: inline-block;
  max-width: 100%;
}

.message-text {
  padding: 10px 14px;
  border-radius: 12px;
  background-color: var(--color-surface);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
  line-height: 1.6;
  word-wrap: break-word;
}

.message-group.user .message-text {
  background-color: var(--color-primary-light);
  border: 1px solid rgba(255, 92, 92, 0.2);
}

.message-bubble.user-editable {
  cursor: pointer;
}

.message-bubble.user-editable:hover .message-text {
  border-color: var(--color-primary);
}

.message-edit-hint {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--color-text-secondary);
  padding: 2px 14px 6px;
  opacity: 0;
  transition: opacity 0.15s;
}

.message-bubble.user-editable:hover .message-edit-hint {
  opacity: 1;
}

.regenerate-btn {
  margin-left: 4px;
  padding: 0 6px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 11px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.regenerate-btn:hover {
  color: var(--color-primary);
}

/* ── Markdown 渲染样式（完整覆盖所有元素） ── */

/* 段落 */
.message-text :deep(p) {
  margin: 0;
}
.message-text :deep(p + p) {
  margin-top: 8px;
}

/* 标题 */
.message-text :deep(h1) { font-size: 1.4em; font-weight: 700; margin: 16px 0 8px; border-bottom: 1px solid var(--color-border); padding-bottom: 4px; }
.message-text :deep(h2) { font-size: 1.25em; font-weight: 700; margin: 14px 0 6px; }
.message-text :deep(h3) { font-size: 1.12em; font-weight: 700; margin: 12px 0 6px; }
.message-text :deep(h4) { font-size: 1.05em; font-weight: 600; margin: 10px 0 4px; }
.message-text :deep(h5) { font-size: 1em; font-weight: 600; margin: 8px 0 4px; }
.message-text :deep(h6) { font-size: 0.95em; font-weight: 600; margin: 6px 0 4px; color: var(--color-text-secondary); }

/* 强调 */
.message-text :deep(strong) { font-weight: 700; }
.message-text :deep(em) { font-style: italic; }
.message-text :deep(del) { text-decoration: line-through; color: var(--color-text-secondary); }

/* 行内代码 */
.message-text :deep(code) {
  background-color: rgba(0, 0, 0, 0.06);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.9em;
  font-family: ui-monospace, 'SF Mono', 'Cascadia Code', 'Fira Code', Monaco, monospace;
  color: #d63384;
}

/* 代码块 */
.message-text :deep(pre) {
  background-color: #1e1e1e;
  color: #d4d4d4;
  padding: 14px 16px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 10px 0;
  line-height: 1.5;
  font-size: 0.88em;
}
.message-text :deep(pre code) {
  background-color: transparent;
  color: inherit;
  padding: 0;
  border-radius: 0;
  font-size: inherit;
}

/* 列表 */
.message-text :deep(ul),
.message-text :deep(ol) {
  margin: 6px 0;
  padding-left: 22px;
}
.message-text :deep(li) {
  margin: 2px 0;
  line-height: 1.55;
}
.message-text :deep(li > ul),
.message-text :deep(li > ol) {
  margin: 2px 0;
}
.message-text :deep(ol ol) { list-style-type: lower-alpha; }
.message-text :deep(ol ol ol) { list-style-type: lower-roman; }

/* 分割线 */
.message-text :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 14px 0;
}

/* 引用块 */
.message-text :deep(blockquote) {
  border-left: 3px solid var(--color-primary);
  padding: 6px 12px;
  margin: 8px 0;
  color: var(--color-text-secondary);
  background: rgba(0, 0, 0, 0.02);
  border-radius: 0 6px 6px 0;
}
.message-text :deep(blockquote p:last-child) {
  margin-bottom: 0;
}

/* 链接 */
.message-text :deep(a) {
  color: var(--color-primary);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.message-text :deep(a:hover) {
  color: var(--color-primary-dark, #d64545);
}

/* ── 表格 ── */
.message-text :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 0.94em;
  overflow: hidden;
  border-radius: 6px;
}
.message-text :deep(thead) {
  background-color: var(--color-primary);
  color: #fff;
}
.message-text :deep(th) {
  padding: 8px 12px;
  text-align: left;
  font-weight: 600;
  font-size: 0.92em;
}
.message-text :deep(td) {
  padding: 7px 12px;
  border-bottom: 1px solid var(--color-border);
}
.message-text :deep(tbody tr:nth-child(even)) {
  background-color: rgba(0, 0, 0, 0.02);
}
.message-text :deep(tbody tr:hover) {
  background-color: rgba(0, 0, 0, 0.04);
}

/* ── 任务列表 ── */
.message-text :deep(input[type="checkbox"]) {
  margin-right: 6px;
  accent-color: var(--color-primary);
  vertical-align: middle;
}

/* ── 图片 ── */
.message-text :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
  margin: 8px 0;
}

/* ── 键盘标签 ── */
.message-text :deep(kbd) {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 0.85em;
  font-family: ui-monospace, monospace;
  box-shadow: 0 1px 0 var(--color-border);
}

/* 上下标 */
.message-text :deep(sub),
.message-text :deep(sup) {
  font-size: 0.8em;
}

/* 行内链接卡片补偿 */
.message-text :deep(a:has(img)) {
  text-decoration: none;
}

/* 组底部 */
.group-footer {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 4px;
  padding-left: 4px;
}

.group-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}

.group-time {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* 空状态 */
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
}

.empty-icon {
  width: 100px;
  height: 100px;
  opacity: 0.5;
}

.empty-icon.loading {
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% {
    opacity: 0.3;
    transform: scale(0.95);
  }
  50% {
    opacity: 0.6;
    transform: scale(1);
  }
}

.empty-hint {
  color: var(--color-text-secondary);
  font-size: 14px;
}

/* 加载指示器 */
.loading-group .message-bubble,
.message-bubble:has(.loading-dots) {
  padding: 14px 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.loading-dots {
  display: flex;
  gap: 4px;
  align-items: center;
}

.loading-dots span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: var(--color-primary);
  animation: loading-pulse 1.4s ease-in-out infinite;
}

.loading-dots span:nth-child(2) {
  animation-delay: 0.2s;
}

.loading-dots span:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes loading-pulse {
  0%, 100% {
    opacity: 0.4;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1);
  }
}

/* 技能下拉选择器 */
.skill-dropdown {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin: 0 0 6px;
  background: #1e1e2e;
  border: 1px solid #333;
  border-radius: 10px;
  overflow: hidden;
  z-index: 100;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  max-height: 320px;
  display: flex;
  flex-direction: column;
}

.skill-dropdown-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  font-size: 12px;
  color: #999;
  border-bottom: 1px solid #333;
  flex-shrink: 0;
}

.skill-dropdown-hint {
  font-size: 11px;
  color: #666;
}

.skill-dropdown-list {
  overflow-y: auto;
  flex: 1;
}

.skill-dropdown-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 16px;
  cursor: pointer;
  transition: background-color 0.15s;
}

.skill-dropdown-item:hover,
.skill-dropdown-item.active {
  background: rgba(255, 92, 92, 0.15);
}

.skill-dropdown-item-icon {
  font-size: 16px;
  flex-shrink: 0;
  margin-top: 2px;
}

.skill-dropdown-item-content {
  min-width: 0;
}

.skill-dropdown-item-name {
  font-size: 14px;
  font-weight: 600;
  color: #e0e0e0;
  margin-bottom: 2px;
}

.skill-dropdown-item-desc {
  font-size: 12px;
  color: #888;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 技能下拉淡入动画 */
.skill-dropdown-fade-enter-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}

.skill-dropdown-fade-leave-active {
  transition: opacity 0.1s ease, transform 0.1s ease;
}

.skill-dropdown-fade-enter-from {
  opacity: 0;
  transform: translateY(8px);
}

.skill-dropdown-fade-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

/* 输入区域 */
.chat-input-wrapper {
  padding: 16px 24px 32px;
  background-color: var(--color-surface);
  border-top: 1px solid var(--color-border);
  position: relative;
}

.chat-input {
  display: flex;
  gap: 12px;
  align-items: center;
  max-width: 800px;
  margin: 0 auto;
}

.chat-file-input {
  display: none;
}

/* 附件预览条（输入框上方） */
.chat-attachments-preview {
  max-width: 800px;
  margin: 0 auto 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 6px 0;
}

/* 拖拽落区高亮 */
.chat-input.is-drag-over {
  outline: 2px dashed var(--color-primary);
  outline-offset: 4px;
  border-radius: 12px;
  background: var(--color-primary-light, rgba(255, 92, 92, 0.04));
}

/* 消息气泡内的附件展示 */
.message-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 6px;
  max-width: 100%;
}

.message-group.user .message-attachments {
  justify-content: flex-end;
}

.chat-input-attach {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  padding: 0;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
}

.chat-input-attach:hover:not(:disabled) {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.chat-input-attach:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.chat-input :deep(.ant-input) {
  flex: 1;
  border-radius: 16px;
  padding: 10px 16px;
  resize: none;
}

/* 按钮区域（固定宽度，防止输入框抖动） */
.input-actions {
  flex-shrink: 0;
  display: flex;
  gap: 8px;
  align-items: center;
  width: auto;
  min-width: 92px;
}

.context-usage-ring {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: default;
  flex-shrink: 0;
}

.context-usage-ring :deep(.ant-progress-inner) {
  width: 34px !important;
  height: 34px !important;
}

/* 新建会话按钮 */
.input-actions .icon-btn {
  width: 40px;
  height: 40px;
  padding: 0;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
}

.input-actions .icon-btn:hover {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

/* 发送按钮 - 白底 + 黑色图标，输入后红底 + 白色图标 */
.input-actions .send-btn {
  width: 40px;
  height: 40px;
  padding: 0;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.2s ease;
  color: #333;
}

.input-actions .send-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

/* 输入文字后：红底 + 白色图标 */
.input-actions .send-btn.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}

.input-actions .send-btn.active:hover {
  background: var(--color-primary-hover);
  border-color: var(--color-primary-hover);
}

/* 停止按钮 - 红底 + 白色圆角方块图标 */
.input-actions .stop-btn {
  width: 40px;
  height: 40px;
  padding: 0;
  border: none;
  border-radius: 8px;
  background: var(--color-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.2s ease;
}

.input-actions .stop-btn:hover {
  background: var(--color-primary-hover);
}

.stop-icon {
  width: 14px;
  height: 14px;
  background: #fff;
  border-radius: 3px;
}

/* 工具调用卡片 */
.tool-calls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

.tool-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  transition: all 0.2s ease;
}

/* 执行中状态 - 龙虾红主题 */
.tool-card.running {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.tool-card.running .tool-icon,
.tool-card.running .tool-name {
  color: var(--color-primary);
}

/* 完成状态 - 灰色调 */
.tool-card.done {
  border-color: var(--color-border);
  background: var(--color-surface);
}

/* 失败状态 - 红色调 */
.tool-card.error {
  border-color: var(--color-primary);
  background: #fff1f0;
}

.tool-card.error .tool-icon,
.tool-card.error .tool-name {
  color: var(--color-primary);
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.tool-header:hover {
  opacity: 0.8;
}

.tool-icon {
  font-size: 14px;
  line-height: 1;
}

.tool-name {
  font-weight: 500;
  color: var(--color-text);
  flex: 1;
}

.tool-tag {
  font-size: 11px;
  padding: 0 6px;
  line-height: 18px;
  border-radius: 4px;
}

.collapse-indicator {
  font-size: 10px;
  color: var(--color-text-secondary);
  margin-left: auto;
  transition: transform 0.2s ease;
}

/* 工具详情区域 */
.tool-details {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border);
}

.tool-args,
.tool-result-wrapper {
  margin-bottom: 8px;
}

.tool-result-wrapper:last-child {
  margin-bottom: 0;
}

.tool-detail-label {
  font-size: 11px;
  color: var(--color-text-secondary);
  margin-bottom: 4px;
  font-weight: 500;
}

.tool-detail-content {
  margin: 0;
  padding: 8px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 4px;
  font-size: 12px;
  color: var(--color-text);
  max-height: 150px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, 'SF Mono', Monaco, 'Andale Mono', monospace;
}

.step-info {
  color: var(--color-text-secondary);
  font-size: 11px;
}
</style>
