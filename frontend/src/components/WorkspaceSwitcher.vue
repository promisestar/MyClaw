<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Dropdown, Menu, Button, message } from 'ant-design-vue'
import {
  FolderOutlined,
  DownOutlined,
  CheckOutlined,
  FolderOpenOutlined,
} from '@ant-design/icons-vue'
import { workspaceApi } from '@/api/workspace'
import { useWorkspaceStore } from '@/stores/workspace'

const store = useWorkspaceStore()

onMounted(() => {
  store.init()
})

function basename(path: string) {
  return path.split(/[/\\]/).pop() || path
}

async function handleMenuClick({ key }: { key: string | number }) {
  if (key === '__pick__') {
    await pickAndSelect()
  } else {
    await doSelect(String(key))
  }
}

/** 弹出系统文件夹选择对话框，选择后授权并切换工作区 */
const pickLoading = ref(false)
async function pickAndSelect() {
  if (pickLoading.value || store.loading) return // 防并发
  pickLoading.value = true
  try {
    const res = await workspaceApi.pickFolder()
    if (res.cancelled || !res.path) {
      return // 用户取消，静默处理
    }
    await store.authorizeAndSelect(res.path)
    message.success(`已切换到工作区: ${basename(res.path)}`)
  } catch {
    message.error('工作区授权失败（请检查后端是否在本地运行且支持 tkinter）')
  } finally {
    pickLoading.value = false
  }
}

async function doSelect(path: string) {
  if (store.loading) return // 防止并发切换
  try {
    await store.select(path)
    message.success(`已切换到工作区: ${basename(path)}`)
  } catch {
    message.error('工作区切换失败')
  }
}
</script>

<template>
  <div class="workspace-switcher">
    <Dropdown :trigger="['click']" :placement="'bottomLeft'">
      <Button size="small" type="text" class="switcher-btn" :loading="store.loading || pickLoading">
        <FolderOutlined class="switcher-icon" />
        <span class="workspace-name">{{ store.currentName }}</span>
        <DownOutlined class="switcher-icon" />
      </Button>
      <template #overlay>
        <Menu @click="handleMenuClick" class="workspace-menu">
          <Menu.Item
            v-for="ws in store.history"
            :key="ws"
            :class="{ active: ws === store.current }"
          >
            <CheckOutlined v-if="ws === store.current" class="item-icon" />
            <FolderOutlined v-else class="item-icon" />
            <span class="ws-name">{{ basename(ws) }}</span>
            <span class="ws-path">{{ ws }}</span>
          </Menu.Item>
          <Menu.Divider />
          <Menu.Item key="__pick__" :disabled="pickLoading" class="pick-item">
            <FolderOpenOutlined class="item-icon pick-icon" />
            <span>打开本地文件夹...</span>
          </Menu.Item>
        </Menu>
      </template>
    </Dropdown>
  </div>
</template>

<style scoped>
.workspace-switcher {
  display: flex;
  align-items: center;
}

.switcher-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 200px;
  padding: 4px 10px;
}

.switcher-icon {
  font-size: 14px;
}

.workspace-name {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

/* 菜单整体内边距与菜单项间距 */
:deep(.workspace-menu .ant-menu-item) {
  padding: 8px 14px;
  line-height: 1.5;
  margin: 2px 0;
}

:deep(.workspace-menu .ant-menu-item-divider) {
  margin: 6px 0;
}

.item-icon {
  margin-right: 10px;
  font-size: 14px;
}

.pick-icon {
  color: #1677ff;
}

.ws-name {
  margin-right: 8px;
  font-weight: 500;
}

.ws-path {
  font-size: 12px;
  color: #999;
}

.active {
  color: #ff5c5c;
  background-color: #fff2f0;
}

.pick-item {
  color: #1677ff;
}
</style>
