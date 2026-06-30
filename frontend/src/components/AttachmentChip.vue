<script setup lang="ts">
import { computed } from 'vue'
import { CloseOutlined, FileOutlined, FilePdfOutlined, FileWordOutlined, FileExcelOutlined, FileTextOutlined, PictureOutlined } from '@ant-design/icons-vue'

interface Props {
  /** 文件名 */
  filename: string
  /** 类别（与后端对齐） */
  kind: 'image' | 'doc' | 'other'
  /** 字节数（用于显示大小） */
  size?: number
  /** 图片缩略图 URL（可选，仅 kind=image 时使用） */
  imageUrl?: string
  /** 是否显示删除按钮 */
  removable?: boolean
  /** 是否紧凑显示（消息气泡内使用） */
  compact?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  removable: false,
  compact: false,
  size: 0,
  imageUrl: '',
})

const emit = defineEmits<{
  (e: 'remove'): void
  (e: 'preview'): void
}>()

const formattedSize = computed(() => {
  if (!props.size) return ''
  if (props.size < 1024) return `${props.size} B`
  if (props.size < 1024 * 1024) return `${(props.size / 1024).toFixed(1)} KB`
  return `${(props.size / 1024 / 1024).toFixed(1)} MB`
})

const docExt = computed(() => {
  const m = /\.([a-z0-9]+)$/i.exec(props.filename)
  return (m?.[1] || '').toLowerCase()
})

const DocIcon = computed(() => {
  if (props.kind === 'image') return PictureOutlined
  switch (docExt.value) {
    case 'pdf':
      return FilePdfOutlined
    case 'doc':
    case 'docx':
      return FileWordOutlined
    case 'xls':
    case 'xlsx':
    case 'csv':
      return FileExcelOutlined
    case 'txt':
    case 'md':
    case 'markdown':
    case 'log':
      return FileTextOutlined
    default:
      return FileOutlined
  }
})
</script>

<template>
  <div :class="['att-chip', kind, { compact, removable }]" @click="emit('preview')">
    <!-- 图片缩略图 -->
    <div v-if="kind === 'image' && imageUrl" class="att-thumb">
      <img :src="imageUrl" :alt="filename" loading="lazy" />
    </div>
    <!-- 文档 / 其他：图标 + 文件名 + 大小 -->
    <div v-else class="att-doc">
      <component :is="DocIcon" class="att-doc-icon" />
      <div class="att-doc-meta">
        <div class="att-doc-name" :title="filename">{{ filename }}</div>
        <div v-if="formattedSize && !compact" class="att-doc-size">{{ formattedSize }}</div>
      </div>
    </div>

    <button
      v-if="removable"
      type="button"
      class="att-remove-btn"
      title="移除"
      @click.stop="emit('remove')"
    >
      <CloseOutlined />
    </button>
  </div>
</template>

<style scoped>
.att-chip {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  font-size: 12px;
  color: var(--color-text);
  cursor: default;
  max-width: 240px;
  box-sizing: border-box;
}

.att-chip.image {
  padding: 0;
  border: none;
  background: transparent;
  max-width: 160px;
}

.att-chip.compact {
  padding: 4px 8px;
  font-size: 11px;
}

.att-thumb {
  width: 96px;
  height: 96px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--color-border);
  background: #f5f5f5;
}

.att-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.att-doc {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.att-doc-icon {
  font-size: 20px;
  color: var(--color-primary);
  flex-shrink: 0;
}

.att-doc-meta {
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.att-doc-name {
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 180px;
}

.att-doc-size {
  font-size: 11px;
  color: var(--color-text-secondary);
  margin-top: 2px;
}

.att-remove-btn {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: none;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
  line-height: 1;
  z-index: 2;
}

.att-remove-btn:hover {
  background: var(--color-primary);
}
</style>
