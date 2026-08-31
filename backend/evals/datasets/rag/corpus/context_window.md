# 上下文窗口与裁剪

Agent 需要在有限上下文窗口内组织 system、历史、工具结果。

## ContextGuard

- 按窗口大小动态估算工具输出规模
- 过大结果会被截断或提示改用更小范围读取

## ContextManager

- 管理历史消息压缩与 tool 结果 snip
- `tool_snip_chars` 可随窗口放大

## 原则

优先保护近期对话与关键系统指令；长工具输出不应挤掉核心指令。
