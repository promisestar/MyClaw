<script setup lang="ts">
import { computed } from 'vue'
import { Dropdown, Menu, MenuItem, MenuDivider, Tooltip } from 'ant-design-vue'
import {
  FileSearchOutlined,
  EditOutlined,
  ThunderboltOutlined,
  DownOutlined,
  CheckOutlined,
  InfoCircleOutlined,
  PlusOutlined,
} from '@ant-design/icons-vue'

export type AgentMode = 'ask' | 'plan' | 'craft'

const props = defineProps<{
  modelValue: AgentMode
}>()

const emit = defineEmits<{
  'update:modelValue': [value: AgentMode]
}>()

// 每种模式的配置：图标 + 标签 + 描述
const modeConfig: Record<AgentMode, { label: string; icon: any; desc: string }> = {
  craft: {
    label: 'Craft',
    icon: ThunderboltOutlined,
    desc: '全自动执行，自动调用所有工具完成复杂任务',
  },
  ask: {
    label: 'Ask',
    icon: FileSearchOutlined,
    desc: '只读模式，仅提问、搜索和分析，不修改文件',
  },
  plan: {
    label: 'Plan',
    icon: EditOutlined,
    desc: '先分析生成执行计划，确认后按计划执行',
  },
}

const modeOrder: AgentMode[] = ['craft', 'ask', 'plan']

const currentMode = computed(() => modeConfig[props.modelValue])

const handleSelect = (key: string | number) => {
  emit('update:modelValue', key as AgentMode)
}
</script>

<template>
  <Dropdown trigger="click" placement="bottomLeft">
    <!-- 触发按钮：当前模式图标 + 标签 + 下拉箭头 -->
    <div class="mode-trigger">
      <component :is="currentMode.icon" class="trigger-icon" />
      <span class="trigger-label">{{ currentMode.label }}</span>
      <DownOutlined class="trigger-arrow" />
    </div>

    <!-- 下拉菜单 -->
    <template #overlay>
      <Menu
        class="mode-menu"
        :selected-keys="[props.modelValue]"
        @click="({ key }) => handleSelect(key)"
      >
        <MenuItem
          v-for="mode in modeOrder"
          :key="mode"
          class="mode-menu-item"
        >
          <component :is="modeConfig[mode].icon" class="item-icon" />
          <span class="item-label">{{ modeConfig[mode].label }}</span>
          <CheckOutlined v-if="mode === props.modelValue" class="item-check" />
          <Tooltip
            v-else
            :title="modeConfig[mode].desc"
            placement="right"
            :mouse-enter-delay="0.3"
            overlay-class-name="mode-info-tooltip"
          >
            <InfoCircleOutlined class="item-info" />
          </Tooltip>
        </MenuItem>

        <MenuDivider />

        <MenuItem key="create" disabled class="mode-menu-item create-item">
          <PlusOutlined class="item-icon" />
          <span class="item-label">创建 Agent</span>
        </MenuItem>
      </Menu>
    </template>
  </Dropdown>
</template>

<style scoped>
/* ---- 触发按钮（在主文档 DOM 中，scoped 有效） ---- */
.mode-trigger {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-secondary, #475166);
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  user-select: none;
  flex-shrink: 0;
  transition: all .2s ease;
}

.mode-trigger:hover {
  background: var(--color-primary-light, rgba(48, 119, 236, .06));
  color: var(--color-primary, #3077ec);
}

.trigger-icon {
  font-size: 14px;
  color: inherit;
}

.trigger-label {
  letter-spacing: .2px;
}

.trigger-arrow {
  font-size: 10px;
  color: var(--color-text-tertiary, #7a8499);
  transition: transform .2s ease;
}

.mode-trigger:hover .trigger-arrow {
  color: var(--color-primary, #3077ec);
}
</style>

<!--
  Dropdown overlay（Menu）和 Tooltip 都渲染到 body portal，
  scoped 样式无法穿透，必须放在全局样式块中。
-->
<style>
/* ---- 下拉菜单容器 ---- */
.mode-menu {
  min-width: 160px;
  padding: 4px;
  border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, .1);
}

/* ---- 菜单项 flex 布局 ---- */
.mode-menu .ant-dropdown-menu-item {
  display: flex !important;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  transition: background .15s ease;
}

.mode-menu .ant-dropdown-menu-item:hover {
  background: #f5f6fa;
}

.mode-menu .ant-dropdown-menu-item-selected {
  background: #e6f0ff;
  color: #3077ec;
  font-weight: 500;
}

/* ---- 菜单项内部元素 ---- */
.mode-menu .item-icon {
  font-size: 14px;
  flex-shrink: 0;
}

.mode-menu .item-label {
  flex: 1;
  letter-spacing: .2px;
}

.mode-menu .item-check {
  font-size: 12px;
  color: #3077ec;
  flex-shrink: 0;
  margin-left: 8px;
}

.mode-menu .item-info {
  font-size: 12px;
  color: #7a8499;
  flex-shrink: 0;
  transition: color .15s ease;
  margin-left: 8px;
}

.mode-menu .item-info:hover {
  color: #3077ec;
}

/* ---- 创建 Agent（预留） ---- */
.mode-menu .create-item {
  color: #7a8499;
}

.mode-menu .create-item.ant-dropdown-menu-item {
  cursor: not-allowed;
}

/* ---- Tooltip 弹层间距 ---- */
.mode-info-tooltip.ant-tooltip-placement-right {
  transform: translateX(20px) !important;
}

.mode-info-tooltip .ant-tooltip-arrow {
  left: -12px !important;
}
</style>
