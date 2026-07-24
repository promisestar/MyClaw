<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { Menu, ConfigProvider, theme } from 'ant-design-vue'
import {
  MessageOutlined,
  SettingOutlined,
  HistoryOutlined,
  BookOutlined,
  FolderOutlined,
  FileTextOutlined,
  ThunderboltOutlined,
  HeartOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from '@ant-design/icons-vue'
import LobsterIcon from '@/assets/lobster.svg'
import WorkspaceSwitcher from '@/components/WorkspaceSwitcher.vue'
import { healthApi } from '@/api/health'

const route = useRoute()

// 龙虾红主题配置
const customTheme = {
  token: {
    colorPrimary: '#ff5c5c',
    colorPrimaryHover: '#ff7070',
    colorPrimaryActive: '#e64a4a',
    colorPrimaryBg: 'rgba(255, 92, 92, 0.1)',
    colorPrimaryBgHover: 'rgba(255, 92, 92, 0.2)',
  },
}

// 暗色模式（持久化到 localStorage，通过 data-theme 驱动 CSS 变量覆写）
const THEME_KEY = 'myclaw.theme'
const isDark = ref(localStorage.getItem(THEME_KEY) === 'dark')
watch(
  isDark,
  (dark) => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light')
  },
  { immediate: true },
)

// 侧边栏折叠
const sidebarCollapsed = ref(false)

// 后端健康状态（每 30s 轮询）
type HealthState = 'unknown' | 'healthy' | 'degraded' | 'down'
const healthState = ref<HealthState>('unknown')

const statusText = computed(() => {
  const map: Record<HealthState, string> = {
    unknown: '检测中…',
    healthy: '服务正常',
    degraded: '服务降级',
    down: '连接异常',
  }
  return map[healthState.value]
})

const checkHealth = async () => {
  try {
    const res = await healthApi.getDetailedHealth()
    healthState.value =
      res.status === 'healthy' ? 'healthy' : res.status === 'degraded' ? 'degraded' : 'down'
  } catch {
    healthState.value = 'down'
  }
}

let healthTimer: number | undefined

onMounted(() => {
  checkHealth()
  healthTimer = window.setInterval(checkHealth, 30000)
})

onBeforeUnmount(() => {
  window.clearInterval(healthTimer)
})
</script>

<template>
  <ConfigProvider
    :theme="{
      token: customTheme.token,
      algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    }"
  >
    <div class="app-container">
      <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
        <div class="logo">
          <div class="logo-tile">
            <div class="logo-tile-inner">
              <img :src="LobsterIcon" alt="MyClaw" class="logo-icon" />
            </div>
          </div>
          <div v-if="!sidebarCollapsed" class="logo-meta">
            <span class="logo-text">MyClaw</span>
            <span class="logo-sub">Personal AI Agent</span>
          </div>
        </div>
        <div v-if="!sidebarCollapsed" class="workspace-area">
          <WorkspaceSwitcher />
        </div>
        <Menu
          mode="inline"
          :selected-keys="[route.name as string]"
          :inline-collapsed="sidebarCollapsed"
          class="sidebar-menu"
        >
          <Menu.ItemGroup title="对话">
            <Menu.Item key="chat">
              <RouterLink to="/">
                <MessageOutlined />
                <span>聊天</span>
              </RouterLink>
            </Menu.Item>
            <Menu.Item key="sessions">
              <RouterLink to="/sessions">
                <HistoryOutlined />
                <span>会话</span>
              </RouterLink>
            </Menu.Item>
          </Menu.ItemGroup>
          <Menu.ItemGroup title="系统">
            <Menu.Item key="skills">
              <RouterLink to="/skills">
                <ThunderboltOutlined />
                <span>技能</span>
              </RouterLink>
            </Menu.Item>
            <Menu.Item key="knowledge-base">
              <RouterLink to="/knowledge-base">
                <FolderOutlined />
                <span>知识库</span>
              </RouterLink>
            </Menu.Item>
            <Menu.Item key="tool-logs">
              <RouterLink to="/tool-logs">
                <FileTextOutlined />
                <span>工具日志</span>
              </RouterLink>
            </Menu.Item>
            <Menu.Item key="memory">
              <RouterLink to="/memory">
                <BookOutlined />
                <span>记忆</span>
              </RouterLink>
            </Menu.Item>
            <Menu.Item key="config">
              <RouterLink to="/config">
                <SettingOutlined />
                <span>配置</span>
              </RouterLink>
            </Menu.Item>
            <Menu.Item key="health">
              <RouterLink to="/health">
                <HeartOutlined />
                <span>健康检查</span>
              </RouterLink>
            </Menu.Item>
          </Menu.ItemGroup>
        </Menu>

        <div class="sidebar-footer">
          <div v-if="!sidebarCollapsed" class="status-card" :title="`后端状态：${statusText}`">
            <span class="status-dot" :class="healthState"></span>
            <span class="status-text">{{ statusText }}</span>
          </div>
          <div class="footer-actions">
            <button
              type="button"
              class="footer-btn"
              :title="isDark ? '切换为浅色模式' : '切换为深色模式'"
              @click="isDark = !isDark"
            >
              <svg v-if="isDark" class="theme-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
              </svg>
              <svg v-else class="theme-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
              </svg>
            </button>
            <button
              type="button"
              class="footer-btn"
              :title="sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
              @click="sidebarCollapsed = !sidebarCollapsed"
            >
              <MenuUnfoldOutlined v-if="sidebarCollapsed" />
              <MenuFoldOutlined v-else />
            </button>
          </div>
        </div>
      </aside>

      <main class="main-content">
        <RouterView v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" />
          </Transition>
        </RouterView>
      </main>
    </div>
  </ConfigProvider>
</template>

<style scoped>
.app-container {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.sidebar {
  width: 220px;
  background-color: var(--surface-2);
  border-right: 1px solid var(--color-border);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  transition: width 0.2s ease, background-color 0.2s ease;
}

.sidebar.collapsed {
  width: 68px;
}

.logo {
  padding: 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--color-border);
}

.sidebar.collapsed .logo {
  padding: 16px 0;
  justify-content: center;
}

/* 渐变描边 logo 底座 */
.logo-tile {
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: var(--gradient-brand);
  padding: 2px;
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(255, 92, 92, 0.3);
}

.logo-tile-inner {
  width: 100%;
  height: 100%;
  border-radius: 10px;
  background: var(--surface-2);
  display: flex;
  align-items: center;
  justify-content: center;
}

.logo-icon {
  width: 24px;
  height: 24px;
}

.logo-meta {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  min-width: 0;
}

.logo-text {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
}

.logo-sub {
  font-size: 11px;
  color: var(--color-text-tertiary);
}

.workspace-area {
  padding: 8px 16px 12px;
  border-bottom: 1px solid var(--color-border);
}

.sidebar-menu {
  flex: 1;
  border-right: none;
  padding-top: 4px;
  background: transparent;
  overflow-y: auto;
  overflow-x: hidden;
}

.sidebar-menu.ant-menu-inline-collapsed {
  width: 100%;
}

/* 菜单分组标题 */
.sidebar-menu :deep(.ant-menu-item-group-title) {
  font-size: 11px;
  color: var(--color-text-tertiary);
  padding: 14px 18px 4px;
  letter-spacing: 1px;
}

/* 菜单项圆角与间距 */
.sidebar-menu :deep(.ant-menu-item) {
  border-radius: var(--radius-md);
  transition: background-color 0.15s ease, color 0.15s ease;
}

/* 选中态：左侧主色指示条，隐藏 antd 默认右侧条 */
.sidebar-menu :deep(.ant-menu-item-selected)::after {
  display: none;
}

.sidebar-menu :deep(.ant-menu-item-selected)::before {
  content: '';
  position: absolute;
  left: 2px;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 2px;
  background: var(--color-primary);
}

/* 底部区域 */
.sidebar-footer {
  border-top: 1px solid var(--color-border);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.status-card {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--surface-1);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  padding: 8px 10px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--color-text-tertiary);
}

.status-dot.healthy {
  background: #52c41a;
  animation: status-pulse 2s ease-out infinite;
}

.status-dot.degraded {
  background: #faad14;
  animation: status-pulse 2s ease-out infinite;
}

.status-dot.down {
  background: #ff4d4f;
}

@keyframes status-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(82, 196, 26, 0.4);
  }
  70% {
    box-shadow: 0 0 0 6px rgba(82, 196, 26, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(82, 196, 26, 0);
  }
}

.footer-actions {
  display: flex;
  gap: 8px;
}

.footer-btn {
  flex: 1;
  height: 32px;
  border: 1px solid var(--color-border);
  background: var(--surface-2);
  border-radius: var(--radius-md);
  cursor: pointer;
  color: var(--color-text-secondary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  transition: color 0.15s ease, border-color 0.15s ease, background-color 0.15s ease;
}

.footer-btn:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.theme-icon {
  width: 15px;
  height: 15px;
}

.sidebar.collapsed .sidebar-footer {
  align-items: center;
}

.sidebar.collapsed .footer-actions {
  flex-direction: column;
  align-items: center;
}

.sidebar.collapsed .footer-btn {
  width: 36px;
  flex: none;
}

.main-content {
  flex: 1;
  background-color: var(--surface-1);
  overflow: auto;
  height: 100vh;
  transition: background-color 0.2s ease;
}
</style>
