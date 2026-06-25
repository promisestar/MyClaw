<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, message, Input, Spin } from 'ant-design-vue'
import { ArrowLeftOutlined, SaveOutlined } from '@ant-design/icons-vue'
import { skillsApi } from '@/api/skills'

const route = useRoute()
const router = useRouter()
// 当前编辑的技能名（用 ref 以便改名后更新页面标题）
const skillName = ref(route.params.name as string)

const content = ref('')
const originalContent = ref('')
const loading = ref(false)
const saving = ref(false)
const hasChanges = ref(false)

/** 从后端结构化错误中提取用户可读消息 */
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

const loadContent = async () => {
  loading.value = true
  try {
    const res = await skillsApi.getContent(skillName.value)
    content.value = res.content
    originalContent.value = res.content
  } catch (error) {
    message.error(extractErrorMessage(error, '加载技能内容失败'))
    router.push({ name: 'skills' })
  } finally {
    loading.value = false
  }
}

const handleInput = () => {
  hasChanges.value = content.value !== originalContent.value
}

const handleSave = async () => {
  saving.value = true
  try {
    const res = await skillsApi.updateContent(skillName.value, content.value)
    originalContent.value = content.value
    hasChanges.value = false
    if (res.renamed && res.name !== skillName.value) {
      message.success(`技能已重命名为「${res.name}」并保存`)
      // 路由参数更新，避免刷新后 404
      const newName = res.name
      skillName.value = newName
      router.replace({ name: 'skill-editor', params: { name: newName } })
    } else {
      message.success('技能内容已保存')
    }
  } catch (error) {
    message.error(extractErrorMessage(error, '保存失败'))
  } finally {
    saving.value = false
  }
}

const handleBack = () => {
  if (hasChanges.value) {
    message.info('内容已保存')
  }
  router.push({ name: 'skills' })
}

onMounted(() => {
  loadContent()
})
</script>

<template>
  <div class="skill-editor">
    <!-- 顶部导航栏 -->
    <div class="editor-header">
      <Button type="text" @click="handleBack">
        <template #icon><ArrowLeftOutlined /></template>
        返回
      </Button>
      <h2 class="editor-title">编辑技能：{{ skillName }}</h2>
      <Button
        type="primary"
        :loading="saving"
        :disabled="!hasChanges"
        @click="handleSave"
      >
        <template #icon><SaveOutlined /></template>
        保存
      </Button>
    </div>

    <!-- 编辑器区域 -->
    <div class="editor-body">
      <Spin v-if="loading" size="large" class="editor-loading" />
      <template v-else>
        <div class="editor-hint">
          编辑 SKILL.md 内容。格式说明：使用 YAML frontmatter 定义 <code>name</code> 和 <code>description</code>，正文为 Markdown 格式的技能说明。
        </div>
        <Input.TextArea
          v-model:value="content"
          class="editor-textarea"
          :auto-size="false"
          @input="handleInput"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.skill-editor {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #fff;
}

.editor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 24px;
  border-bottom: 1px solid #f0f0f0;
  background: #fafafa;
  flex-shrink: 0;
}

.editor-title {
  font-size: 16px;
  font-weight: 600;
  color: #333;
  margin: 0;
}

.editor-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  overflow: hidden;
}

.editor-loading {
  margin: auto;
}

.editor-hint {
  font-size: 13px;
  color: #999;
  margin-bottom: 12px;
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 6px;
  line-height: 1.6;
  flex-shrink: 0;
}

.editor-hint code {
  background: #e8e8e8;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12px;
}

.editor-textarea {
  flex: 1;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 14px;
  line-height: 1.6;
}

.editor-textarea :deep(textarea) {
  height: 100% !important;
  resize: none;
  border: 1px solid #e0e0e0;
  border-radius: 8px;
  padding: 16px;
}

.editor-textarea :deep(textarea):focus {
  border-color: #ff5c5c;
  box-shadow: 0 0 0 2px rgba(255, 92, 92, 0.1);
}
</style>
