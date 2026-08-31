# 用户画像 Profile 聚合

Profile 把分散记忆沉淀为 USER.md 中的常驻人设。

## 素材来源

聚合器从 Memory 中按 category 检索 `preference`、`entity`、`decision` 三类长期记忆。

## 触发条件

- 对话轮次间隔达到阈值（例如每 10 轮）
- 或 preference 类记忆数量足够多

## 写入位置

聚合结果写入 USER.md 的 AUTO 区域，进入冻结 system 提示，不与每轮临时记忆注入冲突。
