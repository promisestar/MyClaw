<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Card, Button, Switch, Empty, message, Popconfirm, Modal, Input, Tabs, Tag, Tooltip, Radio } from 'ant-design-vue'
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  ThunderboltOutlined,
  FolderOpenOutlined,
  GithubOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons-vue'
import { skillsApi, type SkillInfo } from '@/api/skills'

const router = useRouter()
const skills = ref<SkillInfo[]>([])
const loading = ref(false)
const curatorStatus = ref<Record<string, unknown> | null>(null)
const archivedNames = ref<string[]>([])
const curatorBusy = ref(false)

// Import modal state
const importModalOpen = ref(false)
const importLoading = ref(false)
const importSourceType = ref<'path' | 'git'>('path')
const importSource = ref('')
const importScope = ref<'global' | 'workspace'>('workspace')

const loadSkills = async () => {
  loading.value = true
  try {
    const res = await skillsApi.list()
    skills.value = res.skills
  } catch (error) {
    message.error('加载技能列表失败')
  } finally {
    loading.value = false
  }
}

const loadCuratorMeta = async () => {
  try {
    curatorStatus.value = await skillsApi.curatorStatus()
    const arch = await skillsApi.archived('workspace')
    archivedNames.value = arch.archived || []
  } catch {
    // Curator 未就绪时静默
  }
}

onMounted(async () => {
  await loadSkills()
  await loadCuratorMeta()
})

const toggleLoading = ref<string | null>(null)

/**
 * 从后端结构化错误中提取用户可读消息
 * 后端返回格式：{ detail: { code, message, detail } }
 * 兼容旧格式：{ detail: "字符串" }
 */
const extractErrorMessage = (error: unknown, fallback: string): string => {
  if (typeof error === 'object' && error !== null) {
    const e = error as { response?: { data?: { detail?: unknown } }; message?: string }
    const detail = e.response?.data?.detail
    if (typeof detail === 'object' && detail !== null) {
      const d = detail as { message?: string; code?: string }
      if (d.message) {
        return d.code ? `${d.message}（${d.code}）` : d.message
      }
    }
    if (typeof detail === 'string') return detail
    if (e.message) return e.message
  }
  return fallback
}

const handleToggle = async (skill: SkillInfo, checked: boolean) => {
  // 乐观更新：立即切换 UI
  const previousEnabled = skill.enabled
  skill.enabled = checked
  toggleLoading.value = skill.name
  try {
    const res = await skillsApi.toggle(skill.name)
    skill.enabled = res.enabled  // 以服务端返回值为准
    message.success(res.message)
  } catch (error) {
    skill.enabled = previousEnabled  // 失败时恢复
    message.error(extractErrorMessage(error, '切换状态失败'))
  } finally {
    toggleLoading.value = null
  }
}

const handleEdit = (skill: SkillInfo) => {
  router.push({ name: 'skill-editor', params: { name: skill.name } })
}

const handleDelete = async (skill: SkillInfo) => {
  try {
    await skillsApi.delete(skill.name)
    message.success(`技能 "${skill.name}" 已删除`)
    await loadSkills()
  } catch (error) {
    message.error(extractErrorMessage(error, '删除技能失败'))
  }
}

const installLoading = ref<string | null>(null)
const installLogModalOpen = ref(false)
const installLog = ref('')
const installLogTitle = ref('')

const handleInstallEnv = async (skill: SkillInfo) => {
  installLoading.value = skill.name
  message.loading({ content: `正在为「${skill.name}」安装依赖环境...`, key: 'install', duration: 0 })
  try {
    const res = await skillsApi.installEnv(skill.name)
    message.destroy('install')
    if (res.success) {
      message.success(`「${skill.name}」环境安装成功`)
    } else {
      message.error(`「${skill.name}」环境安装失败`)
    }
    installLogTitle.value = `「${skill.name}」环境安装日志`
    installLog.value = res.log || '（无输出）'
    installLogModalOpen.value = true
    await loadSkills()
  } catch (error) {
    message.destroy('install')
    message.error(extractErrorMessage(error, '环境安装请求失败'))
  } finally {
    installLoading.value = null
  }
}

const handleImport = async () => {
  if (!importSource.value.trim()) {
    message.warning('请输入来源地址')
    return
  }
  importLoading.value = true
  try {
    const res = await skillsApi.import(importSourceType.value, importSource.value.trim(), importScope.value)
    message.success(res.message || '导入成功')
    importModalOpen.value = false
    importSource.value = ''
    await loadSkills()
  } catch (error) {
    message.error(extractErrorMessage(error, '导入失败'))
  } finally {
    importLoading.value = false
  }
}

const openImportModal = () => {
  importSourceType.value = 'path'
  importSource.value = ''
  importScope.value = 'workspace'
  importModalOpen.value = true
}

const handlePinToggle = async (skill: SkillInfo) => {
  try {
    if (skill.pinned) {
      await skillsApi.unpin(skill.name)
      skill.pinned = false
      message.success(`已取消 pin：${skill.name}`)
    } else {
      await skillsApi.pin(skill.name)
      skill.pinned = true
      message.success(`已 pin：${skill.name}`)
    }
  } catch (error) {
    message.error(extractErrorMessage(error, 'pin 操作失败'))
  }
}

const handleAdopt = async (skill: SkillInfo) => {
  try {
    await skillsApi.adopt(skill.name)
    skill.curator_managed = true
    message.success(`已 adopt：${skill.name}`)
  } catch (error) {
    message.error(extractErrorMessage(error, 'adopt 失败'))
  }
}

const handleArchive = async (skill: SkillInfo) => {
  try {
    await skillsApi.archive(skill.name)
    message.success(`已归档：${skill.name}`)
    await loadSkills()
    await loadCuratorMeta()
  } catch (error) {
    message.error(extractErrorMessage(error, '归档失败'))
  }
}

const handleRestore = async (name: string) => {
  try {
    await skillsApi.restore(name, 'workspace')
    message.success(`已恢复：${name}`)
    await loadSkills()
    await loadCuratorMeta()
  } catch (error) {
    message.error(extractErrorMessage(error, '恢复失败'))
  }
}

const handleCuratorRun = async () => {
  curatorBusy.value = true
  try {
    const res = await skillsApi.curatorRun(true, false)
    message.success(`Curator 已运行：${(res as { summary?: string }).summary || 'ok'}`)
    await loadSkills()
    await loadCuratorMeta()
  } catch (error) {
    message.error(extractErrorMessage(error, 'Curator 运行失败'))
  } finally {
    curatorBusy.value = false
  }
}

const handleCuratorPauseToggle = async () => {
  try {
    const scopes = (curatorStatus.value?.scopes || {}) as Record<string, { state?: { paused?: boolean } }>
    const paused = Object.values(scopes).some((s) => s?.state?.paused)
    if (paused) {
      await skillsApi.curatorResume()
      message.success('Curator 已恢复')
    } else {
      await skillsApi.curatorPause()
      message.success('Curator 已暂停')
    }
    await loadCuratorMeta()
  } catch (error) {
    message.error(extractErrorMessage(error, 'Curator 暂停/恢复失败'))
  }
}
</script>

<template>
  <div class="skills-view">
    <!-- 顶部工具栏 -->
    <div class="skills-header">
      <div>
        <h1><ThunderboltOutlined /> 技能</h1>
        <p>管理和配置 Agent 可用的专业技能</p>
      </div>
      <Button type="primary" @click="openImportModal">
        <template #icon><PlusOutlined /></template>
        导入
      </Button>
    </div>

    <!-- Curator 状态条 -->
    <div class="curator-bar">
      <div class="curator-bar-text">
        <span>Curator</span>
        <Tag v-if="curatorStatus" color="blue">已接入</Tag>
        <Tag v-else color="default">未就绪</Tag>
        <span v-if="archivedNames.length" class="curator-archived">
          归档 {{ archivedNames.length }}：
          <a
            v-for="n in archivedNames.slice(0, 5)"
            :key="n"
            class="restore-link"
            @click="handleRestore(n)"
          >{{ n }}</a>
        </span>
      </div>
      <div class="curator-bar-actions">
        <Button size="small" :loading="curatorBusy" @click="handleCuratorRun">立即运行</Button>
        <Button size="small" @click="handleCuratorPauseToggle">暂停/恢复</Button>
      </div>
    </div>

    <!-- 技能卡片区域 -->
    <div class="skills-content">
      <div v-if="skills.length > 0" class="skills-grid">
        <Card
          v-for="skill in skills"
          :key="skill.name"
          :class="['skill-card', { disabled: !skill.enabled }]"
          hoverable
        >
          <div class="skill-card-body">
            <div class="skill-card-header">
              <div class="skill-icon">
                <ThunderboltOutlined />
              </div>
              <h3 class="skill-name">{{ skill.name }}</h3>
              <Switch
                v-model:checked="skill.enabled"
                :loading="toggleLoading === skill.name"
                size="small"
                @change="(checked: string | number | boolean) => handleToggle(skill, !!checked)"
              />
            </div>
            <p class="skill-desc">{{ skill.description }}</p>
            <div class="skill-meta-tags">
              <Tag v-if="skill.pinned" color="purple">pin</Tag>
              <Tag v-if="skill.curator_managed" color="cyan">curator</Tag>
              <Tag v-if="skill.lifecycle_state && skill.lifecycle_state !== 'active'" color="orange">
                {{ skill.lifecycle_state }}
              </Tag>
              <Tag>use {{ skill.use_count ?? 0 }}</Tag>
              <Tag>patch {{ skill.patch_count ?? 0 }}</Tag>
            </div>
            <!-- 环境状态徽标 -->
            <div class="skill-env-status">
              <Tooltip v-if="skill.has_venv" :title="skill.python_path || ''">
                <Tag color="success">
                  <template #icon><CheckCircleOutlined /></template>
                  专属环境就绪
                </Tag>
              </Tooltip>
              <Tooltip v-else-if="skill.has_dependencies" title="该技能声明了依赖但未安装专属环境，运行可能失败">
                <Tag color="warning">
                  <template #icon><ExclamationCircleOutlined /></template>
                  依赖未安装
                </Tag>
              </Tooltip>
              <Tag v-else color="default">无依赖</Tag>
            </div>
            <div class="skill-dir">
              <FolderOpenOutlined />
              <span>{{ skill.dir }}</span>
            </div>
          </div>
          <div class="skill-card-actions">
            <Tooltip :title="skill.has_venv ? '重新安装依赖到专属环境' : '为该技能创建专属环境并安装依赖'">
              <Button
                v-if="skill.has_dependencies"
                size="small"
                type="text"
                :loading="installLoading === skill.name"
                @click.stop="handleInstallEnv(skill)"
              >
                <template #icon><ReloadOutlined /></template>
                {{ skill.has_venv ? '重装依赖' : '安装依赖' }}
              </Button>
            </Tooltip>
            <Button size="small" type="text" @click.stop="handleEdit(skill)">
              <template #icon><EditOutlined /></template>
              编辑
            </Button>
            <Button size="small" type="text" @click.stop="handlePinToggle(skill)">
              {{ skill.pinned ? '取消 pin' : 'pin' }}
            </Button>
            <Button
              v-if="!skill.curator_managed"
              size="small"
              type="text"
              @click.stop="handleAdopt(skill)"
            >
              adopt
            </Button>
            <Popconfirm
              title="归档此技能？（可恢复，非硬删）"
              ok-text="归档"
              cancel-text="取消"
              @confirm="handleArchive(skill)"
            >
              <Button size="small" type="text" @click.stop>归档</Button>
            </Popconfirm>
            <Popconfirm
              title="确定要删除此技能吗？"
              :description="`将删除「${skill.name}」对应的整个目录（含专属环境）`"
              ok-text="删除"
              cancel-text="取消"
              ok-type="danger"
              @confirm="handleDelete(skill)"
            >
              <Button size="small" type="text" danger @click.stop>
                <template #icon><DeleteOutlined /></template>
                删除
              </Button>
            </Popconfirm>
          </div>
        </Card>
      </div>

      <!-- 空状态 -->
      <Empty v-else-if="!loading" description="暂无技能，点击右上角「导入」按钮添加" />
    </div>

    <!-- 导入弹窗 -->
    <Modal
      v-model:open="importModalOpen"
      title="导入技能"
      :confirm-loading="importLoading"
      ok-text="导入"
      cancel-text="取消"
      @ok="handleImport"
    >
      <Tabs v-model:activeKey="importSourceType">
        <Tabs.TabPane key="path" tab="本地目录">
          <div class="import-form">
            <p class="import-hint">输入本地技能目录的完整路径，系统将复制该目录到技能存储位置。</p>
            <Input
              v-model:value="importSource"
              placeholder="例：/Users/me/skills/my-skill"
              size="large"
            />
          </div>
        </Tabs.TabPane>
        <Tabs.TabPane key="git" tab="Git 仓库">
          <div class="import-form">
            <p class="import-hint">输入 Git 仓库地址，系统将自动克隆仓库中的技能文件。</p>
            <Input
              v-model:value="importSource"
              placeholder="例：https://github.com/user/my-skill.git"
              size="large"
            >
              <template #prefix>
                <GithubOutlined style="color: #999" />
              </template>
            </Input>
          </div>
        </Tabs.TabPane>
      </Tabs>
      <div class="import-scope">
        <p class="import-hint">导入层级</p>
        <Radio.Group v-model:value="importScope" class="scope-radio">
          <Radio value="workspace">项目级（仅当前工作区）</Radio>
          <Radio value="global">用户级（跨工作区共享）</Radio>
        </Radio.Group>
      </div>
      <div class="import-deps-hint">
        💡 如果技能包含 <code>requirements.txt</code> 或在 SKILL.md frontmatter 中声明了
        <code>dependencies</code>，系统将自动为该技能创建专属 venv 并安装依赖（可能需要 1-5 分钟，依赖量大时更久）。
      </div>
    </Modal>

    <!-- 安装日志 Modal -->
    <Modal
      v-model:open="installLogModalOpen"
      :title="installLogTitle"
      :footer="null"
      width="720px"
    >
      <pre class="install-log">{{ installLog }}</pre>
    </Modal>
  </div>
</template>

<style scoped>
.skills-view {
  padding: 24px 32px;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.skills-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}

.curator-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  margin-bottom: 16px;
  border: 1px solid #eee;
  border-radius: 8px;
  background: #fafafa;
}

.curator-bar-text {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #666;
}

.curator-bar-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.curator-archived .restore-link {
  margin-right: 8px;
  color: #1677ff;
  cursor: pointer;
}

.skill-meta-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}

.skills-header h1 {
  font-size: 20px;
  font-weight: 600;
  color: #333;
  margin: 0 0 4px 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.skills-header p {
  font-size: 13px;
  color: #999;
  margin: 0;
}

.skills-content {
  flex: 1;
  overflow-y: auto;
}

.skills-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}

.skill-card {
  display: flex;
  flex-direction: column;
  border-radius: 10px;
  transition: transform 0.2s, box-shadow 0.2s;
}

.skill-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
}

.skill-card.disabled {
  opacity: 0.6;
}

.skill-card-body {
  flex: 1;
}

.skill-card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.skill-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: linear-gradient(135deg, #ff5c5c, #ff8a80);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 16px;
  flex-shrink: 0;
}

.skill-name {
  flex: 1;
  font-size: 15px;
  font-weight: 600;
  color: #333;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-desc {
  font-size: 13px;
  color: #666;
  margin: 0 0 10px 0;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.skill-env-status {
  display: flex;
  gap: 6px;
  margin: 6px 0 8px;
}

.skill-dir {
  font-size: 12px;
  color: #bbb;
  display: flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-card-actions {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #f0f0f0;
}

.skill-card-actions :deep(.ant-btn-text) {
  font-size: 12px;
}

.import-form {
  padding: 8px 0;
}

.import-hint {
  font-size: 13px;
  color: #999;
  margin: 0 0 12px 0;
  line-height: 1.5;
}

.import-scope {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #f0f0f0;
}

.import-scope .import-hint {
  margin-bottom: 8px;
  font-weight: 500;
  color: #666;
}

.import-deps-hint {
  margin-top: 12px;
  padding: 10px 12px;
  background: #f6ffed;
  border: 1px solid #b7eb8f;
  border-radius: 6px;
  font-size: 12px;
  color: #389e0d;
  line-height: 1.6;
}

.import-deps-hint code {
  background: rgba(0, 0, 0, 0.06);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11.5px;
}

.install-log {
  max-height: 480px;
  overflow: auto;
  background: #1e1e2e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: 8px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
