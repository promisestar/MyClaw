<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Card, List, Button, Empty, message, Popconfirm, Modal, Spin } from 'ant-design-vue'
import { toolLogsApi, type LogFileInfo, type LogEntry } from '@/api/tool-logs'
import { DeleteOutlined, FileTextOutlined, ClockCircleOutlined } from '@ant-design/icons-vue'

const logFiles = ref<LogFileInfo[]>([])
const loading = ref(false)

// Modal 相关状态
const modalOpen = ref(false)
const modalLoading = ref(false)
const modalTitle = ref('')
const modalEntries = ref<LogEntry[]>([])

const loadLogFiles = async () => {
  loading.value = true
  try {
    const res = await toolLogsApi.list()
    logFiles.value = res.files
  } catch (error) {
    message.error('加载日志文件列表失败')
  } finally {
    loading.value = false
  }
}

const openLogFile = async (file: LogFileInfo) => {
  modalOpen.value = true
  modalTitle.value = file.file_name
  modalLoading.value = true
  modalEntries.value = []
  try {
    const res = await toolLogsApi.get(file.date_str)
    modalEntries.value = res.entries
  } catch (error) {
    message.error('加载日志内容失败')
    modalOpen.value = false
  } finally {
    modalLoading.value = false
  }
}

const deleteLogFile = async (dateStr: string) => {
  try {
    await toolLogsApi.delete(dateStr)
    message.success('日志文件已删除')
    await loadLogFiles()
  } catch (error) {
    message.error('删除日志文件失败')
  }
}

const formatSize = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const formatDate = (isoStr: string) => {
  return new Date(isoStr).toLocaleString('zh-CN')
}

const getStatusTag = (status: string) => {
  if (status === 'error') return '❌'
  if (status === 'timeout') return '⏱️'
  return '✅'
}

onMounted(() => {
  loadLogFiles()
})
</script>

<template>
  <div class="tool-logs-view">
    <div class="tool-logs-header">
      <div>
        <h1>工具日志</h1>
        <p>查看和管理工具调用日志</p>
      </div>
    </div>

    <div class="tool-logs-content">
      <Card v-if="logFiles.length > 0" class="tool-logs-card">
        <List :data-source="logFiles" :loading="loading">
          <template #renderItem="{ item }">
            <List.Item class="log-item">
              <List.Item.Meta>
                <template #title>
                  <span class="log-title">{{ item.file_name }}</span>
                </template>
                <template #description>
                  <span class="log-meta">
                    {{ item.entry_count }} 条记录 · {{ formatSize(item.size_bytes) }}
                    <span class="log-time">
                      <ClockCircleOutlined /> {{ formatDate(item.modified_at) }}
                    </span>
                  </span>
                </template>
              </List.Item.Meta>
              <template #actions>
                <button
                  class="open-btn"
                  @click="openLogFile(item)"
                >
                  打开
                </button>
                <Popconfirm
                  title="确定删除此日志文件？"
                  description="删除后不可恢复"
                  ok-text="删除"
                  cancel-text="取消"
                  ok-type="danger"
                  @confirm="deleteLogFile(item.date_str)"
                >
                  <button class="delete-btn" title="删除">
                    <DeleteOutlined />
                  </button>
                </Popconfirm>
              </template>
            </List.Item>
          </template>
        </List>
      </Card>

      <Card v-else class="empty-card">
        <Empty description="暂无工具调用日志">
          <p class="empty-hint">与 Agent 对话将自动记录工具调用日志</p>
        </Empty>
      </Card>
    </div>

    <!-- 日志内容查看弹窗 -->
    <Modal
      v-model:open="modalOpen"
      :title="modalTitle"
      width="820px"
      :footer="null"
      destroy-on-close
    >
      <Spin :spinning="modalLoading" tip="加载中...">
        <div v-if="modalEntries.length > 0" class="log-entries">
          <div
            v-for="(entry, index) in modalEntries"
            :key="index"
            class="log-entry"
          >
            <div class="log-entry-header">
              <span class="log-entry-index">#{{ index + 1 }}</span>
              <span class="log-entry-status">{{ getStatusTag(entry.status) }}</span>
              <span class="log-entry-tool">{{ entry.tool_name }}</span>
              <span class="log-entry-duration">{{ entry.duration_ms }}ms</span>
            </div>
            <pre class="log-entry-json">{{ JSON.stringify(entry, null, 2) }}</pre>
          </div>
        </div>
        <Empty v-else-if="!modalLoading" description="该日志文件无有效记录" />
      </Spin>
    </Modal>
  </div>
</template>

<style scoped>
.tool-logs-view {
  min-height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 24px;
  box-sizing: border-box;
}

.tool-logs-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
}

.tool-logs-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
}

.tool-logs-header p {
  margin: 0;
  color: #999;
}

.tool-logs-content {
  flex: 1;
  overflow-y: auto;
}

.tool-logs-card {
  max-width: 800px;
}

.log-item {
  padding: 16px 0;
}

.log-title {
  font-weight: 500;
  font-family: monospace;
}

.log-meta {
  color: #999;
  font-size: 13px;
}

.log-time {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 12px;
  color: #bbb;
}

/* 打开按钮 */
.open-btn {
  padding: 0 8px;
  height: 22px;
  font-size: 12px;
  line-height: 20px;
  border: none;
  background: transparent;
  color: #333;
  cursor: pointer;
  transition: color 0.2s ease;
}

.open-btn:hover {
  color: #ff4d4f;
}

/* 删除按钮 */
.delete-btn {
  padding: 4px 8px;
  border: none;
  background: transparent;
  color: #333;
  cursor: pointer;
  transition: color 0.2s ease;
}

.delete-btn:hover {
  color: #ff4d4f;
}

.empty-card {
  max-width: 400px;
  margin: 60px auto;
}

.empty-hint {
  color: #bbb;
  font-size: 13px;
  margin-top: 8px;
}

/* ===== Modal 内日志条目样式 ===== */
.log-entries {
  max-height: 70vh;
  overflow-y: auto;
  padding-right: 8px;
}

.log-entry {
  margin-bottom: 16px;
  border: 1px solid #f0f0f0;
  border-radius: 6px;
  overflow: hidden;
}

.log-entry-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #fafafa;
  border-bottom: 1px solid #f0f0f0;
  font-size: 13px;
}

.log-entry-index {
  font-weight: 600;
  color: #999;
  font-family: monospace;
}

.log-entry-status {
  font-size: 14px;
}

.log-entry-tool {
  flex: 1;
  font-weight: 500;
  font-family: monospace;
  color: #333;
}

.log-entry-duration {
  color: #999;
  font-family: monospace;
  font-size: 12px;
}

.log-entry-json {
  margin: 0;
  padding: 12px;
  font-size: 12px;
  line-height: 1.6;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  color: #333;
  background: #fff;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 360px;
  overflow-y: auto;
}
</style>
