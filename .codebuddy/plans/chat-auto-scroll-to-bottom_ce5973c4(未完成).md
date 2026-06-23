---
name: chat-auto-scroll-to-bottom
overview: 修复进入聊天界面时未自动滚动到最新对话的问题。根因是 loadSessionHistory 中的 scrollToBottom 调用发生在 initializing 仍为 true 时，消息列表 DOM 尚未渲染，导致滚动无效。
todos:
  - id: fix-autoscroll
    content: 在 onMounted 中 initSession 之后补加 scrollToBottom 调用
    status: pending
---

## 用户需求

修改 `ChatView.vue`，使每次进入聊天界面时自动滚动显示最新对话（底部），而非停留在对话顶部。

## 问题根因

`loadSessionHistory` 内部调用了 `scrollToBottom()`，但此时 `initializing.value` 仍为 `true`，模板渲染的是"加载中"占位符而非消息列表，导致 `scrollHeight` 为 0、滚动无效。`initSession` 将 `initializing` 置为 `false` 后消息列表才渲染，但此后未再次滚动，用户停留在对话顶部。

## 修复方案

在 `onMounted` 中 `await initSession()` 之后补一次 `await scrollToBottom()`。此时 `initializing` 已为 `false`，`nextTick` 后消息列表 DOM 已渲染，滚动生效。改动仅一行，不影响切换会话等现有逻辑。

## 技术栈

- 前端框架：Vue 3 (Composition API, `<script setup>`)
- UI 库：Ant Design Vue
- 滚动机制：直接操作 DOM `scrollTop = scrollHeight`，配合 Vue `nextTick` 等待 DOM 更新

## 实现方案

### 根因分析

1. `scrollToBottom()`（第 509-514 行）内部 `await nextTick()` 后设置 `scrollTop = scrollHeight`
2. `loadSessionHistory()`（第 372 行）在 `messages.value = displayMessages` 之后立即调用 `scrollToBottom()`，但此时 `initializing.value === true`
3. 模板条件 `v-if="initializing"` 渲染的是加载占位符，消息列表 `<template v-else-if="messages.length > 0">` 尚未渲染
4. `initSession()` 在 `loadSessionHistory()` 返回后才设置 `initializing.value = false`（第 401/412/421 行）
5. `initializing` 变 `false` 后消息列表渲染，但无后续滚动调用 → 停留顶部

### 修复

在 `onMounted`（第 499-502 行）中，`await initSession()` 之后、`await refreshContextUsage()` 之前，插入 `await scrollToBottom()`。

此时执行环境：

- `initializing.value` 已为 `false`（initSession 三个分支均已设置）
- `messages.value` 已填充历史消息
- `scrollToBottom` 内 `await nextTick()` 等待 Vue 完成 DOM patch，消息列表已渲染
- `scrollHeight` 反映真实内容高度，滚动到底部生效

### 边界情况

- **新建会话（无消息）**：`messages.length === 0`，模板渲染空状态，`scrollHeight` 等于容器高度，`scrollTop = scrollHeight` 为无操作，无副作用
- **切换会话（watch route.query.session）**：`initializing` 已为 `false`，`loadSessionHistory` 内的 `scrollToBottom` 本就正常工作，无需修改
- **markdown 内容含图片**：`renderMarkdown` 为同步函数，HTML 结构在 `nextTick` 后已就绪；图片异步加载导致的后续高度变化属于既有行为，不在本次修复范围

## 实现备注

- 改动仅一行，位于 `onMounted` 内，不影响任何其他代码路径
- `scrollToBottom` 为现有函数，无需新增逻辑
- 无性能影响：单次 DOM 读写，时间复杂度 O(1)