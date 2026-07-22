<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Card, Tag, Button, Spin, message } from 'ant-design-vue'
import { ReloadOutlined, CheckCircleOutlined, WarningOutlined, CloseCircleOutlined } from '@ant-design/icons-vue'
import { healthApi, type DetailedHealthResponse, type HealthCheckResult } from '@/api/health'

const loading = ref(false)
const healthData = ref<DetailedHealthResponse | null>(null)

const statusColorMap: Record<string, string> = {
  ok: 'green',
  warning: 'orange',
  error: 'red',
  skip: 'default',
}

const statusIconMap: Record<string, typeof CheckCircleOutlined> = {
  ok: CheckCircleOutlined,
  warning: WarningOutlined,
  error: CloseCircleOutlined,
  skip: WarningOutlined,
}

const overallColorMap: Record<string, string> = {
  healthy: 'green',
  degraded: 'orange',
  unhealthy: 'red',
}

const checkLabels: Record<string, string> = {
  llm: 'LLM 模型',
  qdrant: 'Qdrant 向量库',
  disk: '磁盘空间',
  memory: '内存使用',
}

async function fetchHealth() {
  loading.value = true
  try {
    healthData.value = await healthApi.getDetailedHealth()
  } catch (e: unknown) {
    const err = e as Error
    message.error(`健康检查失败: ${err.message}`)
  } finally {
    loading.value = false
  }
}

function formatCheckDetail(key: string, check: HealthCheckResult): string {
  if (check.status === 'skip') return check.reason || '跳过'
  if (check.status === 'error') return check.error || '未知错误'

  switch (key) {
    case 'llm':
      return `模型: ${check.model || '-'} | 延迟: ${check.latency_ms || '-'}ms`
    case 'qdrant':
      return `集合数: ${check.collections || 0} | 延迟: ${check.latency_ms || '-'}ms`
    case 'disk':
      return `可用: ${check.free_gb || 0}GB / 总计: ${check.total_gb || 0}GB | 已用: ${check.used_pct || 0}%`
    case 'memory':
      return `RSS: ${check.rss_mb || 0}MB | 系统可用: ${check.available_mb || 0}MB`
    default:
      return ''
  }
}

onMounted(() => {
  fetchHealth()
})
</script>

<template>
  <div class="health-view">
    <div class="health-header">
      <h2 class="page-title">健康检查</h2>
      <Button
        type="primary"
        :loading="loading"
        @click="fetchHealth"
      >
        <template #icon><ReloadOutlined /></template>
        刷新
      </Button>
    </div>

    <Spin :spinning="loading">
      <div v-if="healthData" class="health-content">
        <!-- 总状态卡片 -->
        <Card class="overall-card" :bordered="false">
          <div class="overall-status">
            <Tag :color="overallColorMap[healthData.status]" class="status-tag">
              {{ healthData.status === 'healthy' ? '健康' : healthData.status === 'degraded' ? '降级' : '不健康' }}
            </Tag>
            <span class="timestamp">检查时间: {{ healthData.timestamp }}</span>
          </div>
        </Card>

        <!-- 依赖项卡片网格 -->
        <div class="checks-grid">
          <Card
            v-for="(check, key) in healthData.checks"
            :key="key"
            class="check-card"
            :bordered="true"
          >
            <div class="check-header">
              <component
                :is="statusIconMap[check.status]"
                :class="['check-icon', `status-${check.status}`]"
              />
              <span class="check-title">{{ checkLabels[key] || key }}</span>
              <Tag :color="statusColorMap[check.status]" class="check-status">
                {{ check.status }}
              </Tag>
            </div>
            <div class="check-detail">
              {{ formatCheckDetail(key, check) }}
            </div>
          </Card>
        </div>
      </div>

      <div v-else-if="!loading" class="empty-state">
        <p>点击"刷新"按钮执行健康检查</p>
      </div>
    </Spin>
  </div>
</template>

<style scoped>
.health-view {
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}

.health-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
}

.page-title {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
}

.overall-card {
  margin-bottom: 24px;
  background: var(--color-card, #f5f6fa);
}

.overall-status {
  display: flex;
  align-items: center;
}

.status-tag {
  font-size: 16px;
  padding: 4px 16px;
  margin-right: 16px;
}

.timestamp {
  color: var(--color-lightgray, #7a8499);
  font-size: 14px;
}

.checks-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}

.check-card {
  transition: box-shadow 0.2s;
}

.check-card:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, .08);
}

.check-header {
  display: flex;
  align-items: center;
  margin-bottom: 12px;
}

.check-icon {
  font-size: 20px;
  margin-right: 8px;
}

.status-ok {
  color: #52c41a;
}

.status-warning {
  color: #faad14;
}

.status-error {
  color: #ff4d4f;
}

.status-skip {
  color: #d9d9d9;
}

.check-title {
  font-size: 16px;
  font-weight: 500;
  flex: 1;
}

.check-status {
  margin-left: auto;
}

.check-detail {
  color: var(--color-midgray, #475166);
  font-size: 13px;
  line-height: 1.6;
}

.empty-state {
  text-align: center;
  padding: 48px;
  color: var(--color-lightgray, #7a8499);
}
</style>
