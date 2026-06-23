# Skill 系统实现与升级说明

## 1. 概览

MyClaw 的 Skill 能力原本依赖外部库 `hello_agents`（`hello_agents/skills/loader.py` 和 `hello_agents/tools/builtin/skill_tool.py`），通过类继承使 `MyClawAgent` 具备 Skill 能力。本次升级将 Skill 系统完全**自实现**，脱离对 `hello_agents` 的依赖，并新增了完整的前端管理界面和聊天框 `/` 触发选择功能。

### 变更对比

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| Skill 加载 | `hello_agents.skills.SkillLoader` | `backend/src/skills/loader.py` — 自实现 |
| Skill Tool | `hello_agents.tools.builtin.SkillTool` | `backend/src/tools/builtin/skill_tool.py` — 自实现 |
| 管理界面 | 无 | 前端 SkillsView.vue（卡片式列表 + 导入弹窗）+ SkillEditor.vue |
| 触发方式 | Agent 自动调用 | Agent 自动调用 + 聊天框 `/` 手动触发 |
| 启用/禁用 | 无 | `SkillStateManager`（持久化到 JSON，与 SKILL.md 分离） |
| 导入 | 无 | 支持本地目录复制 和 Git 仓库克隆 |

---

## 2. 目录与职责

### 2.1 后端 `backend/src/skills/`

```
skills/
├── __init__.py          # 导出 Skill, SkillLoader, SkillStateManager
├── loader.py            # SkillLoader：扫描/加载/导入/编辑/删除/热重载
└── state_manager.py     # SkillStateManager：启用/禁用状态持久化
```

#### 2.1.1 SkillLoader (`loader.py`)

渐进式披露机制，分三层加载：

| 层级 | 内容 | 时机 | Token 估算 |
|------|------|------|-----------|
| Layer 1 | `name` + `description`（YAML frontmatter） | 启动时扫描 | ~100 tokens/skill |
| Layer 2 | `body`（SKILL.md 正文） | 按需加载 | ~2000+ tokens |
| Layer 3 | `scripts/` `references/` `examples/` 等资源 | 按需（通过 Skill Tool 返回提示） | 按需 |

核心方法：

```python
class SkillLoader:
    def __init__(self, skills_dir: Path)
    # 启动时扫描并加载元数据
    def _scan_skills(self)                  # 扫描子目录中的 SKILL.md
    def _parse_frontmatter_only(self, path) # 仅解析 YAML frontmatter（name/description）
    
    # 查询
    def get_skill(self, name) -> Optional[Skill]     # 按需加载完整技能（含 body）
    def list_skills(self, only_enabled=False)        # 列出技能名
    def list_skill_infos(self) -> List[Dict]          # 完整信息（含 enabled）
    def get_descriptions(self, only_enabled=True)     # 格式化描述（用于系统提示词）
    def get_skill_content(self, name)                 # 获取 SKILL.md 原始内容
    def is_enabled(self, name) -> bool
    
    # 修改
    def set_skill_content(self, name, content) -> bool    # 更新 SKILL.md
    def set_enabled(self, name, enabled) -> bool          # 切换启用
    def delete_skill(self, name) -> bool                   # 删除目录
    
    # 导入
    def import_from_path(self, source) -> Optional[Skill]  # 本地目录复制
    def import_from_git(self, repo_url) -> Optional[Skill]  # Git 克隆
    
    # 维护
    def reload(self)    # 热重载
```

**关键设计**：
- `skills_cache`：完整 Skill 对象缓存，首次 `get_skill()` 时按需加载
- `metadata_cache`：仅元数据缓存，启动时一次性扫描
- 导入后显式 `set_enabled(name, True)` 确保默认可用
- `get_skill()` 对已禁用技能返回 `None`

#### 2.1.2 SkillStateManager (`state_manager.py`)

启用/禁用状态独立于 SKILL.md 存储，避免修改原始技能文件。

```python
class SkillStateManager:
    def __init__(self, state_file: Path)     # 状态文件路径（skills_dir/skill_states.json）
    def is_enabled(self, name) -> bool       # 默认 True（不在状态文件中视为启用）
    def set_enabled(self, name, enabled)     
    def remove_state(self, name)             # 删除技能时清理
    def list_disabled(self) -> set
```

存储格式（`skill_states.json`）：
```json
{
  "skill-a": false,
  "skill-b": true
}
```

#### 2.1.3 Skill 数据类 (`__init__.py`)

```python
@dataclass
class Skill:
    name: str
    description: str
    body: str          # SKILL.md 正文（不含 frontmatter）
    path: Path         # SKILL.md 文件路径
    dir: Path          # 技能目录路径
    enabled: bool = True
```

### 2.2 后端 `backend/src/tools/builtin/skill_tool.py`

自实现版 SkillTool，替代 `hello_agents.tools.builtin.SkillTool`。

```python
class SkillTool(Tool):
    def __init__(self, skill_loader: SkillLoader)
    def get_parameters(self) -> List[ToolParameter]
        # skill: string (required) — 要加载的技能名称
        # args:  string (optional) — 替换 $ARGUMENTS 占位符
    def run(self, parameters) -> ToolResponse
    def refresh_description(self)              # 技能列表变化时刷新工具描述
```

**特性**：
- 渐进式披露：仅在需要时加载完整技能
- 参数替换：支持 `$ARGUMENTS` 占位符
- 资源提示：自动列出 `scripts/` `references/` `assets/` `examples/` 目录
- 动态描述：`refresh_description()` 在导入/删除/切换后更新工具描述

### 2.3 后端 `backend/src/api/skills.py`

RESTful API（前缀 `/api/skills`）：

| 方法 | 端点 | 说明 |
|------|------|------|
| `GET` | `/skills` | 列出所有技能（含 enabled 状态） |
| `GET` | `/skills/{name}` | 获取技能详情 |
| `GET` | `/skills/{name}/content` | 获取 SKILL.md 原始内容 |
| `PUT` | `/skills/{name}/content` | 更新 SKILL.md 内容 |
| `DELETE` | `/skills/{name}` | 删除技能目录 |
| `POST` | `/skills/{name}/toggle` | 切换启用/禁用 |
| `POST` | `/skills/import` | 导入技能（支持 `path` 和 `git` 两种 source_type） |
| `POST` | `/skills/reload` | 热重载技能列表 |

**全局依赖注入**：
- `set_skill_loader(loader)` — 在 `main.py` lifespan 中调用
- `get_skill_loader()` — API 端点获取全局 SkillLoader 实例
- `_refresh_skill_tool()` — 在 import/delete/toggle/update 后刷新 Agent 的 SkillTool 描述

### 2.4 后端集成点

#### `myclaw_agent.py`

```python
# 初始化自实现的 Skill 系统（替代 hello_agents）
self.config = Config(skills_enabled=False, skills_auto_register=False)
self.skill_loader = SkillLoader(skills_dir=Path(workspace_path / "skills"))

# 注册 SkillTool（保存引用以便刷新）
self._skill_tool = SkillTool(skill_loader=self.skill_loader)
registry.register_tool(self._skill_tool)

# 对外暴露刷新方法
def refresh_skill_tool(self):
    if hasattr(self, '_skill_tool') and self._skill_tool:
        self._skill_tool.refresh_description()
```

#### `main.py`

```python
from .api import skills
skills.set_skill_loader(_agent.skill_loader)
app.include_router(skills.router, prefix="/api")
```

#### `chat.py`

```python
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_turn_index: Optional[int] = None
    regenerate: bool = False
    skill: Optional[str] = None  # 用户通过 /技能名 指定的技能

def _inject_skill_context(message, skill_name) -> str:
    """注入 SkillTool 调用提示（不注入 body，避免泄露至会话历史）"""
```

### 2.5 前端 `frontend/src/`

```
frontend/src/
├── api/skills.ts            # Skill API 接口定义
├── views/SkillsView.vue     # 技能管理页面（卡片列表 + 导入弹窗）
├── views/SkillEditor.vue    # SKILL.md 编辑器（带返回按钮）
├── views/ChatView.vue       # 聊天框 / 触发逻辑
├── router/index.ts          # /skills 和 /skills/:name/edit 路由
├── App.vue                  # 侧边栏 "技能" 菜单项
└── api/chat.ts              # SendMessageOptions 增加 skill 字段
```

#### 2.5.1 SkillsView.vue

**功能**：
- 卡片式技能列表（响应式网格布局）
- 每张卡片：技能图标 + 名称 + 描述 + 目录路径 + 启用开关 + 编辑/删除按钮
- 右上角 "导入" 按钮 → 弹出 Modal（Tabs 切换本地目录 / Git 仓库）
- 导入成功自动刷新列表
- 删除确认弹窗（Popconfirm）

**组件状态**：
```typescript
interface SkillInfo {
  name: string
  description: string
  enabled: boolean
  dir: string
}
```

**开关处理**（乐观更新 + 失败恢复）：
```typescript
const handleToggle = async (skill, checked) => {
  const prev = skill.enabled
  skill.enabled = checked              // 立即切换 UI
  try {
    const res = await skillsApi.toggle(skill.name)
    skill.enabled = res.enabled        // 以服务端为准
  } catch {
    skill.enabled = prev               // 失败恢复
  }
}
```

**Switch 组件**：
```html
<Switch v-model:checked="skill.enabled" :loading="toggleLoading === skill.name"
        @change="(checked) => handleToggle(skill, checked)" />
```
- `v-model:checked` 双向绑定确保点击即切换
- `@change` 而非 `@click`（避免与 Card 点击事件冲突）

#### 2.5.2 SkillEditor.vue

- 左上角 `← 返回` 按钮 → 回到技能列表
- 技能名作为页面标题
- 大文本编辑器区域（预填充 SKILL.md 原始内容）
- 右下角 "保存" 按钮 → PUT 更新内容并显示成功提示
- 保存成功后自动刷新 SkillTool 描述

#### 2.5.3 ChatView.vue `/` 触发

**输入 `/` 后完整流程**：

```
用户输入 / 
  → showSkillDropdown() 判断正则 /^\/[^\s]*$/
  → filterSkills() 调用 GET /api/skills
  → 过滤 s.enabled && (name/description 匹配)
  → skillDropdownVisible = true
  → Transition 动画显示下拉面板
  → 用户选择技能（点击或 ↑↓ Enter）
  → selectSkill() 设置 inputMessage = "/技能名 "
  → 用户补充内容
  → sendMessage() 解析 /(\S+)\s+([\s\S]*) 正则
  → runChatRequest(userContent, { skill: skillName })
  → 前端 UI 显示  /技能名 用户内容
  → chatApi.sendMessageStream(message, callback, { skill })
  → 后端 ChatRequest.skill = "技能名"
  → _inject_skill_context() 注入调用提示
  → Agent 收到提示后调用 SkillTool 加载技能
```

**下拉面板样式**：
- 定位在输入区域正上方（`position: absolute; bottom: 100%` 相对于 `.chat-input-wrapper`）
- 暗色主题卡片式设计
- 支持键盘导航（↑↓ 选择，Enter 确认，Esc 取消）

#### 2.5.4 侧边栏与路由

```typescript
// App.vue — 位于 "会话" 和 "知识库" 之间
<Menu.Item key="skills">
  <RouterLink to="/skills">
    <ThunderboltOutlined />
    <span>技能</span>
  </RouterLink>
</Menu.Item>

// router/index.ts
{ path: '/skills', name: 'skills', component: () => import('../views/SkillsView.vue') },
{ path: '/skills/:name/edit', name: 'skill-editor', component: () => import('../views/SkillEditor.vue') },
```

---

## 3. 技能存储结构

### 3.1 目录布局

```
<workspace>/skills/                     # 默认: ~/.helloclaw/workspace/skills
├── skill_states.json                   # 启用/禁用状态（独立于 SKILL.md）
├── <技能A>/
│   ├── SKILL.md                        # 技能主文件（YAML frontmatter + Markdown body）
│   ├── scripts/                        # 可选：可执行脚本
│   ├── examples/                       # 可选：示例
│   ├── references/                     # 可选：参考文档
│   └── assets/                         # 可选：静态资源
└── <技能B>/
    └── SKILL.md
```

### 3.2 SKILL.md 格式

```markdown
---
name: my-skill
description: 这是一个示例技能的简短描述
---

# 技能正文

详细的技能说明、步骤、注意事项等。

如果有可替换参数，使用 `$ARGUMENTS` 占位符。
```

`_parse_frontmatter_only()` 要求 `name` 和 `description` 均存在，否则该目录不会被识别为技能。

---

## 4. 导入方式

### 4.1 本地目录（`source_type="path"`）

1. 验证源目录存在且包含 `SKILL.md`
2. 解析 frontmatter 获取 `name`
3. `shutil.copytree(source, skills_dir/name, dirs_exist_ok=True)`
4. 更新 `metadata_cache`
5. `set_enabled(name, True)` 确保默认可用

### 4.2 Git 仓库（`source_type="git"`）

1. 检查 `git` 命令可用
2. Clone 到临时目录 `skills_dir/_repo_name`
3. 递归查找所有 `SKILL.md`（排除 `.git` 目录）
4. 对每个找到的技能目录：复制到 `skills_dir/name`，更新 metadata
5. 清理临时目录
6. 适用场景：多技能仓库（一个 Git 仓库可包含多个技能子目录）

---

## 5. 修复记录

本次升级中修复的 bug：

| # | 问题 | 根因 | 修复文件 |
|---|------|------|---------|
| 1 | 导入技能后 `/` 下拉不显示 | 后端 `_inject_skill_context` 注入完整 body 到消息，Agent 保存到会话历史 → 切换页面回来显示 body 全文 | `chat.py`：改为注入简短调用提示，不注入 body |
| 2 | 切换页面回来消息显示技能全文 | 同上 | `chat.py`：同上 |
| 3 | 输入 `/` 后下拉框不可见 | `.skill-dropdown` 定位上下文是 `.chat-view`（整个视图），`bottom: 100%` 将下拉推到视口外 | `ChatView.vue`：将下拉移入 `.chat-input-wrapper`，添加 `position: relative` |
| 4 | 技能开关点击不响应 | Ant Design Switch 使用 `:checked` 单向绑定 + `@click.stop`，视觉无法切换 | `SkillsView.vue`：改为 `v-model:checked` + `@change` + 乐观更新 |
| 5 | toggle_skill API 对不存在技能不报错 | `is_enabled()` 返回 `bool`，`if current is None` 永不成立 | `skills.py`：用 `name not in loader.list_skills()` 检查 |
| 6 | 导入后未重置 enabled 状态 | `import_from_path/git` 未调用 `set_enabled(name, True)`，复用同名禁用状态 | `loader.py`：导入后显式启用 |
| 7 | SkillTool 描述导入后不更新 | 无刷新机制 | `myclaw_agent.py` + `skills.py`：新增 `refresh_skill_tool()` 并在各接口调用 |
| 8 | `showSkillDropdown` 正则过严 | `/^\/[\w\u4e00-\u9fff-]*$/` 不支持 `.` 等字符 | `ChatView.vue`：改为 `/^\/[^\s]*$/` |

---

## 6. API 参考

### 6.1 列出技能

```http
GET /api/skills
```

响应：
```json
{
  "skills": [
    {
      "name": "my-skill",
      "description": "技能描述",
      "enabled": true,
      "dir": "/path/to/skills/my-skill"
    }
  ],
  "total": 1,
  "enabled_count": 1
}
```

### 6.2 导入技能

```http
POST /api/skills/import
Content-Type: application/json

{
  "source_type": "path",  // "path" | "git"
  "source": "/home/user/skills/my-skill"
}
```

响应：
```json
{
  "message": "技能 'my-skill' 导入成功",
  "skill": {
    "name": "my-skill",
    "description": "技能描述",
    "enabled": true,
    "dir": "/path/to/skills/my-skill"
  }
}
```

### 6.3 切换启用状态

```http
POST /api/skills/my-skill/toggle
```

响应：
```json
{
  "message": "技能 'my-skill' 已禁用",
  "enabled": false
}
```

### 6.4 聊天消息（含 Skill）

```http
POST /api/chat/send/stream
Content-Type: application/json

{
  "message": "帮我分析今天的A股走势",
  "session_id": "abc123",
  "skill": "a-stock-analysis"
}
```

`skill` 字段可选。当存在时，后端会调用 `_inject_skill_context()` 将提示注入到消息中，引导 Agent 调用 SkillTool 加载对应技能。
