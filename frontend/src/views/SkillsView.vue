<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Card, Button, Switch, Empty, message, Popconfirm, Modal, Input, Tabs } from 'ant-design-vue'
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  ThunderboltOutlined,
  FolderOpenOutlined,
  GithubOutlined,
} from '@ant-design/icons-vue'
import { skillsApi, type SkillInfo } from '@/api/skills'

const router = useRouter()
const skills = ref<SkillInfo[]>([])
const loading = ref(false)

// Import modal state
const importModalOpen = ref(false)
const importLoading = ref(false)
const importSourceType = ref<'path' | 'git'>('path')
const importSource = ref('')

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

const toggleLoading = ref<string | null>(null)

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
    message.error('切换状态失败')
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
    message.error('删除技能失败')
  }
}

const handleImport = async () => {
  if (!importSource.value.trim()) {
    message.warning('请输入来源地址')
    return
  }
  importLoading.value = true
  try {
    const res = await skillsApi.import(importSourceType.value, importSource.value.trim())
    message.success(res.message || '导入成功')
    importModalOpen.value = false
    importSource.value = ''
    await loadSkills()
  } catch (error: unknown) {
    const msg = error instanceof Error ? error.message : '导入失败'
    message.error(msg)
  } finally {
    importLoading.value = false
  }
}

const openImportModal = () => {
  importSourceType.value = 'path'
  importSource.value = ''
  importModalOpen.value = true
}

onMounted(() => {
  loadSkills()
})
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
                @change="(checked: boolean) => handleToggle(skill, checked)"
              />
            </div>
            <p class="skill-desc">{{ skill.description }}</p>
            <div class="skill-dir">
              <FolderOpenOutlined />
              <span>{{ skill.dir }}</span>
            </div>
          </div>
          <div class="skill-card-actions">
            <Button size="small" type="text" @click.stop="handleEdit(skill)">
              <template #icon><EditOutlined /></template>
              编辑
            </Button>
            <Popconfirm
              title="确定要删除此技能吗？"
              :description="`将删除「${skill.name}」对应的整个目录`"
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
  margin-bottom: 24px;
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
</style>
