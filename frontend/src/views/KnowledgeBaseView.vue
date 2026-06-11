<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Card, List, Button, Empty, message, Popconfirm } from 'ant-design-vue'
import { knowledgeBaseApi, type DocumentInfo } from '@/api/knowledge-base'
import { useRouter } from 'vue-router'
import { DeleteOutlined, FolderOpenOutlined } from '@ant-design/icons-vue'

const router = useRouter()
const documents = ref<DocumentInfo[]>([])
const loading = ref(false)

const loadDocuments = async () => {
  loading.value = true
  try {
    const res = await knowledgeBaseApi.list()
    documents.value = res.documents
  } catch (error) {
    message.error('加载知识库文档列表失败')
  } finally {
    loading.value = false
  }
}

const openDocument = (sourcePath: string) => {
  router.push({ name: 'chat', query: { doc: sourcePath } })
}

const deleteDocument = async (sourcePath: string) => {
  try {
    await knowledgeBaseApi.delete(sourcePath)
    message.success('文档已删除')
    await loadDocuments()
  } catch (error) {
    message.error('删除文档失败')
  }
}

const getFileName = (sourcePath: string) => {
  const parts = sourcePath.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || sourcePath
}

onMounted(() => {
  loadDocuments()
})
</script>

<template>
  <div class="knowledge-base-view">
    <div class="knowledge-base-header">
      <div>
        <h1>知识库</h1>
        <p>查看和管理知识库中的文档</p>
      </div>
    </div>

    <div class="knowledge-base-content">
      <Card v-if="documents.length > 0" class="knowledge-base-card">
        <List :data-source="documents" :loading="loading">
          <template #renderItem="{ item }">
            <List.Item class="doc-item">
              <List.Item.Meta>
                <template #title>
                  <span class="doc-title">{{ getFileName(item.source_path) }}</span>
                </template>
                <template #description>
                  <span class="doc-meta">
                    {{ item.source_path }} · {{ item.chunk_count }} 个分段
                    <span v-if="item.first_content" class="doc-preview">
                      · {{ item.first_content }}
                    </span>
                  </span>
                </template>
              </List.Item.Meta>
              <template #actions>
                <button
                  class="open-btn"
                  @click="openDocument(item.source_path)"
                >
                  打开
                </button>
                <Popconfirm
                  title="确定删除此文档？"
                  description="将删除该文档的所有分段数据"
                  ok-text="删除"
                  cancel-text="取消"
                  ok-type="danger"
                  @confirm="deleteDocument(item.source_path)"
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
        <Empty description="暂无文档">
          <p class="empty-hint">与Agent交互上传文档到知识库</p>
        </Empty>
      </Card>
    </div>
  </div>
</template>

<style scoped>
.knowledge-base-view {
  min-height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 24px;
  box-sizing: border-box;
}

.knowledge-base-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 24px;
}

.knowledge-base-header h1 {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 500;
}

.knowledge-base-header p {
  margin: 0;
  color: #999;
}

.knowledge-base-content {
  flex: 1;
  overflow-y: auto;
}

.knowledge-base-card {
  max-width: 800px;
}

.doc-item {
  padding: 16px 0;
}

.doc-title {
  font-weight: 500;
  font-family: monospace;
}

.doc-meta {
  color: #999;
  font-size: 13px;
}

.doc-preview {
  color: #bbb;
  font-style: italic;
}

/* 打开按钮 - 黑色字体，hover 红色 */
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

/* 删除按钮 - 黑色图标 */
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
</style>
