---
name: skill-upgrade
overview: 对 MyClaw 的 Skill 能力进行全栈升级：前端增加技能管理页面（含导入/编辑/删除/开关）和聊天框 / 触发选择；后端在项目内直接实现 Skill 的 CRUD 与导入逻辑。
design:
  architecture:
    framework: vue
  styleKeywords:
    - Modern
    - Clean
    - Card-based
    - Dark-theme-popup
    - Minimalist
  fontSystem:
    fontFamily: PingFang SC
    heading:
      size: 18px
      weight: 600
    subheading:
      size: 14px
      weight: 500
    body:
      size: 14px
      weight: 400
  colorSystem:
    primary:
      - "#ff5c5c"
      - "#ff7070"
      - "#e64a4a"
    background:
      - "#ffffff"
      - "#f5f5f5"
      - "#fafafa"
    text:
      - "#333333"
      - "#666666"
      - "#999999"
    functional:
      - "#52c41a"
      - "#ff4d4f"
      - "#faad14"
todos:
  - id: explore-codebase
    content: 使用 [subagent:code-explorer] 深入探索现有项目结构和集成点
    status: completed
  - id: design-architecture
    content: 使用 [subagent:code-architect] 设计技能系统架构和模块划分
    status: completed
    dependencies:
      - explore-codebase
  - id: backend-skill-module
    content: 实现 backend/src/skills/ 模块 (SkillLoader, Skill, StateManager)
    status: completed
    dependencies:
      - design-architecture
  - id: backend-skill-tool
    content: 实现 backend/src/tools/builtin/skill_tool.py 自实现版
    status: completed
    dependencies:
      - backend-skill-module
  - id: backend-api
    content: 实现 backend/src/api/skills.py API 接口
    status: completed
    dependencies:
      - backend-skill-module
  - id: backend-integrate
    content: 修改 MyClawAgent 和 main.py 集成新技能系统
    status: completed
    dependencies:
      - backend-skill-tool
      - backend-api
  - id: frontend-api
    content: 创建 frontend/src/api/skills.ts API 封装
    status: completed
  - id: frontend-menu
    content: 修改 App.vue 添加技能菜单项
    status: completed
  - id: frontend-router
    content: 修改 router/index.ts 添加技能路由
    status: completed
  - id: frontend-skills-view
    content: 创建 SkillsView.vue 技能列表页面
    status: completed
    dependencies:
      - frontend-api
      - frontend-router
  - id: frontend-skill-editor
    content: 创建 SkillEditor.vue 技能编辑器
    status: completed
    dependencies:
      - frontend-api
  - id: frontend-chat-trigger
    content: 修改 ChatView.vue 添加 / 触发技能选择
    status: completed
    dependencies:
      - frontend-api
  - id: test-integration
    content: 端到端测试验证技能系统完整功能
    status: completed
    dependencies:
      - backend-integrate
      - frontend-chat-trigger
---

## 产品概述

对 MyClaw 的 Skill 能力进行升级，将原本依赖外部库 `hello_agents` 的 Skill 系统迁移为项目自实现，并增加完整的前后端技能管理能力。

## 核心功能

### 前端功能

1. **技能菜单入口**: 在侧边栏 "会话" 和 "知识库" 之间新增 "技能" 菜单项
2. **技能管理页面**: 展示技能卡片列表，每张卡片包含：

- 技能名称和描述
- 编辑按钮（点击进入 SKILL.md 编辑视图）
- 删除按钮（删除技能目录）
- 开启/关闭开关（控制技能可用状态）
- 左上角返回按钮可返回列表

3. **技能导入**: 右上角导入按钮弹出弹窗，支持输入本地目录路径或 Git 仓库地址，自动加载到技能目录
4. **聊天框技能触发**: 输入 `/` 自动弹出可用技能列表，选择后填充到输入框

### 后端功能

1. **技能管理模块**: 自实现 SkillLoader 和 Skill 数据类，替代 hello_agents 依赖
2. **技能工具**: 自实现 SkillTool，供 Agent 按需加载技能
3. **技能状态管理**: 支持启用/禁用状态的持久化存储
4. **技能导入**: 支持本地目录复制和 Git 仓库克隆
5. **API 接口**: 提供技能的增删改查、导入、状态切换等 REST API

## 技术栈

- **前端**: Vue 3 (Composition API) + TypeScript + Ant Design Vue
- **后端**: Python + FastAPI
- **存储**: 文件系统 (SKILL.md) + JSON 配置文件 (skill_states.json)

## 实现方案

### 架构设计

采用分层架构，保持与现有项目风格一致：

```
┌─────────────────────────────────────────────────────────────┐
│                        前端层 (Vue 3)                        │
├─────────────┬──────────────┬────────────────────────────────┤
│ SkillsView  │ SkillEditor  │ ChatView (skill trigger)       │
└─────────────┴──────────────┴────────────────────────────────┘
                            │ API Calls
┌───────────────────────────┴─────────────────────────────────┐
│                      API 层 (FastAPI)                        │
│              skills.py (CRUD + import endpoints)             │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────┴─────────────────────────────────┐
│                    业务逻辑层                                │
│  ┌──────────────┐  ┌─────────────┐  ┌────────────────────┐  │
│  │ SkillLoader  │  │ SkillTool   │  │ SkillStateManager  │  │
│  └──────────────┘  └─────────────┘  └────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────┴─────────────────────────────────┐
│                      存储层                                  │
│         文件系统 (skills/<name>/SKILL.md)                    │
│         JSON (skill_states.json)                            │
└─────────────────────────────────────────────────────────────┘
```

### 关键技术决策

1. **技能状态存储**: 使用独立的 `skill_states.json` 文件存储启用/禁用状态，与 SKILL.md 分离，避免修改原始技能文件

2. **技能目录结构**: 保持与 hello_agents 兼容的结构:

```
skills/
├── skill-a/
│   └── SKILL.md
├── skill-b/
│   └── SKILL.md
└── skill_states.json
```

3. **技能导入实现**:

- 本地路径: 使用 `shutil.copytree` 复制目录
- Git 仓库: 使用 `subprocess.run(["git", "clone"])` 克隆

4. **前端技能触发器**: 在 ChatView 输入框监听 `/` 键，弹出技能选择下拉列表，使用 Ant Design 的 Mentions 或自定义 Dropdown 组件

5. **Agent 集成**: 修改 `MyClawAgent._setup_tools()` 使用项目自实现的 `SkillTool`，通过依赖注入传入 `SkillLoader` 实例

### 目录结构

```
backend/src/
├── skills/
│   ├── __init__.py          # [NEW] 导出 Skill, SkillLoader
│   ├── loader.py            # [NEW] SkillLoader 实现
│   └── state_manager.py     # [NEW] 技能状态管理
├── tools/builtin/
│   ├── __init__.py          # [MODIFY] 添加 SkillTool 导出
│   └── skill_tool.py        # [NEW] 自实现 SkillTool
├── api/
│   ├── __init__.py
│   └── skills.py            # [NEW] 技能管理 API
├── agent/
│   └── myclaw_agent.py      # [MODIFY] 使用新的 SkillLoader
└── main.py                  # [MODIFY] 注册 skills 路由

frontend/src/
├── api/
│   └── skills.ts            # [NEW] 技能 API 封装
├── views/
│   ├── SkillsView.vue       # [NEW] 技能列表页面
│   ├── SkillEditor.vue      # [NEW] 技能编辑器
│   └── ChatView.vue         # [MODIFY] 添加 / 触发技能选择
├── router/
│   └── index.ts             # [MODIFY] 添加 skills 路由
└── App.vue                  # [MODIFY] 添加技能菜单项
```

### 关键接口定义

**Skill 数据结构**:

```python
@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    dir: Path
    enabled: bool = True
```

**SkillLoader 核心方法**:

```python
class SkillLoader:
    def __init__(self, skills_dir: Path, state_file: Path = None)
    def list_skills(self) -> List[str]
    def get_skill(self, name: str) -> Optional[Skill]
    def reload(self) -> None
    def import_from_path(self, source: str, name: str = None) -> Skill
    def import_from_git(self, repo_url: str, name: str = None) -> Skill
    def delete_skill(self, name: str) -> bool
    def set_enabled(self, name: str, enabled: bool) -> bool
```

**API 端点**:

```
GET    /api/skills              # 列出所有技能
POST   /api/skills/import       # 导入技能 {source: "path|git", path/url: string}
GET    /api/skills/{name}       # 获取技能详情
GET    /api/skills/{name}/content  # 获取 SKILL.md 原始内容
PUT    /api/skills/{name}/content  # 更新 SKILL.md 内容
DELETE /api/skills/{name}       # 删除技能
POST   /api/skills/{name}/toggle   # 切换启用状态
```

### 实现注意事项

1. **路径处理**: `skills_dir` 默认相对于 workspace 根目录，确保使用 `Path` 处理跨平台路径

2. **错误处理**: 导入失败时返回详细错误信息（Git 未安装、路径不存在等）

3. **并发安全**: 技能操作期间使用文件锁或依赖 Python GIL（文件操作原子性）

4. **前端状态**: 技能列表使用响应式数据，操作后自动刷新

5. **Git 依赖**: Git 导入功能检测系统是否安装 git，未安装时给出友好提示

6. **SKILL.md 格式**: 解析 YAML frontmatter，格式如下:

```
---
name: skill-name
description: Skill description
---
Skill body content...
```

## 设计风格

采用现代简洁的企业级设计，与现有 MyClaw 界面风格保持一致（龙虾红主题）。技能管理界面采用卡片式布局，直观展示技能信息。

### 页面规划

**技能管理页面 (SkillsView)**:

- 顶部工具栏: 标题 + 导入按钮
- 主体区域: 技能卡片网格布局
- 每张卡片: 技能名(加粗) + 描述(灰色小字) + 操作按钮组(编辑/删除/开关)

**技能编辑器 (SkillEditor)**:

- 顶部导航栏: 返回按钮 + 技能名标题 + 保存按钮
- 主体区域:  Monaco Editor 或 textarea 编辑 SKILL.md 内容

**聊天框技能触发器**:

- 输入 `/` 弹出下拉面板
- 面板分栏: Skills (技能列表) + Commands (命令列表)
- 每项显示: 图标 + 技能名 + 描述(截断)
- 支持键盘上下选择，Enter 确认

### 视觉设计要点

- 卡片悬停效果: 轻微上浮 + 阴影增强
- 开关按钮: 使用 Ant Design Switch 组件
- 导入弹窗: 分 Tab 切换"本地目录"和"Git 仓库"
- 技能选择器: 深色下拉面板，与参考图风格一致

## Agent Extensions

### Skill

- **design-to-code**
- Purpose: 参考实现代码生成规范，确保前端 Vue 组件代码质量
- Expected outcome: 高质量 TypeScript Vue 组件实现

### SubAgent

- **code-architect**
- Purpose: 协助设计技能系统的整体架构和模块划分
- Expected outcome: 提供详细的模块设计蓝图和接口定义

- **code-explorer**
- Purpose: 深入探索现有项目结构和模式，确保新功能与现有架构一致
- Expected outcome: 完整的项目结构分析和集成点识别