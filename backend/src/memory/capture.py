"""记忆捕获管理器 - 自动识别并存储对话中的重要信息"""

import asyncio
import re
from datetime import datetime
from typing import List, Optional, Tuple


# ── 记忆触发规则（扩展到 25+ 条） ──────────────────────────

MEMORY_TRIGGERS: List[Tuple[str, str]] = [
    # ── 明确要求记住 ──
    (r"记住|记下|remember|keep\s+in\s+mind|别忘了|别忘了这", "fact"),

    # ── 偏好表达 ──
    (r"我喜欢|我偏好|prefer|like|love|hate|讨厌|不喜欢|不习惯", "preference"),
    (r"习惯|经常|总是|usually|always|generally|typically", "preference"),
    (r"更愿意|更倾向|宁可|rather|I'?d\s+rather", "preference"),

    # ── 决策/否定/纠正 ──
    (r"决定了|decision|用这个|选定|确定用|就用", "decision"),
    (r"不对|错了|不应该|换一个|改成|应该用|纠正|不是这个", "decision"),
    (r"还是|改成|切换|换到|改用|不用", "decision"),

    # ── 计划/意图/时间承诺 ──
    (r"我想|我打算|计划|plan\s*to|I\s*want\s*to|I\s*plan\s*to", "plan"),
    (r"明天|下周|周五|周[一二三四五六日]|deadline|截止|due\s*date|这周|下周", "plan"),
    (r"日程|安排|schedule|appointment|会议|meeting", "plan"),
    (r"日程|安排|schedule|appointment|会议|meeting|待办|todo|task|需要做|要做", "plan"),

    # ── 实体信息 ──
    (r"\+\d{10,}|\d{3,4}[-\s]?\d{7,8}", "entity"),                   # 电话
    (r"[\w.-]+@[\w.-]+\.\w+", "entity"),                             # 邮箱
    (r"我的\w+是|is my|我的电话|我的邮箱|我的地址|我的名字", "entity"),
    (r"密码是|账号是|token|api[.\-_]?key|密钥|secret\s*key", "entity"),
    (r"我叫|称呼我|call\s*me|我的名字是|我是", "entity"),
    (r"微信号|QQ|GitHub|github|用户名|username", "entity"),

    # ── 事实陈述 ──
    (r"事实上|实际上|the\s+fact\s+is|it\s+turns\s+out", "fact"),
    (r"\d+\s*(岁|年|个月|天|次|个|元|块|美元|欧元)", "fact"),        # 带数字的事实
    (r"版本是|version\s*is|版本号|当前版本", "fact"),

    # ── 关系链 ──
    (r"我同事|我老板|我的团队|my\s*team|我朋友|我家人|我同学", "relationship"),
    (r"我的(领导|上司|下属|同事|合伙人|搭档|导师)", "relationship"),

    # ── URL/路径/引用 ──
    (r"https?://\S+", "reference"),
    (r"文件路径|file\s*path|[a-zA-Z]:[\\/][^\s,;，；]+", "reference"),

    # ── 约束/规则 ──
    (r"禁止|不允许|不能|不可以|don'?t|must\s+not|should\s+not|never", "rule"),
    (r"每次|总是要|always\s+do|must\s+do|should\s+do|务必", "rule"),
    (r"格式要求|output\s*format|回复格式|代码风格", "rule"),
]

# ── 分类关键词（用于辅助分类/展示） ────────────────────────

CATEGORY_KEYWORDS: dict = {
    "preference": ["喜欢", "偏好", "prefer", "like", "love", "hate", "讨厌", "不喜欢", "习惯", "习惯于", "习惯", "经常", "总是"],
    "decision": ["决定", "选定", "用这个", "确定", "choose", "decide", "decision", "不对", "错了", "改成", "纠正", "切换"],
    "entity": ["电话", "邮箱", "地址", "名字", "账号", "phone", "email", "address", "account", "密码", "密钥", "我叫", "GitHub"],
    "fact": ["记住", "记下", "事实", "实际上", "remember", "fact", "版本"],
    "plan": ["计划", "打算", "明天", "下周", "deadline", "截止", "日程", "安排", "待办", "要做"],
    "relationship": ["同事", "老板", "团队", "朋友", "家人", "领导", "同学"],
    "reference": ["http", "路径", "文件"],
    "rule": ["禁止", "不允许", "always", "never", "务必", "格式", "风格"],
}


# ── MemoryCaptureManager ──────────────────────────────────

class MemoryCaptureManager:
    """记忆捕获管理器

    负责在对话结束后自动识别值得记忆的信息，并进行分类和去重。
    写入目标：MemoryVectorStore（Qdrant 向量数据库）。

    使用方式：
        manager = MemoryCaptureManager(memory_store)
        memories = manager.capture("用户：我喜欢简洁的回复风格")
        # 返回: [{"content": "用户喜欢简洁的回复风格", "category": "preference"}]
    """

    def __init__(self, memory_store=None, workspace_manager=None):
        """初始化记忆捕获管理器

        Args:
            memory_store: MemoryVectorStore 实例（新存储）
            workspace_manager: WorkspaceManager 实例（旧存储，过渡期兼容）
        """
        self.memory_store = memory_store
        self.workspace = workspace_manager  # 过渡期兼容

        # 编译正则表达式（每个 pattern 编译一次）
        self._compiled_patterns: List[Tuple[re.Pattern, str]] = [
            (re.compile(pattern, re.IGNORECASE), category)
            for pattern, category in MEMORY_TRIGGERS
        ]

    def capture(self, text: str) -> List[dict]:
        """分析文本并捕获值得记忆的信息

        Args:
            text: 要分析的文本（通常是用户消息或对话摘要）

        Returns:
            捕获到的记忆列表，每项包含 content 和 categories
        """
        memories: List[dict] = []
        seen_contents: set = set()  # 用于去重

        # 按句子分割
        sentences = self._split_sentences(text)

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence or len(sentence) < 5:
                continue

            # 检查是否匹配触发规则（返回所有匹配的分类）
            categories = self._match_trigger(sentence)
            if not categories:
                continue

            # 提取记忆内容
            content = self._extract_memory(sentence, categories[0])
            if not content:
                continue

            # 去重检查
            content_key = content.lower().strip()
            if content_key in seen_contents:
                continue

            # 去重检查（有 workspace 时才做文件去重）
            if self.workspace and self.workspace.check_duplicate_memory(content, threshold=0.7):
                continue

            seen_contents.add(content_key)
            memories.append({
                "content": content,
                "categories": categories,  # 可命中多个分类
                "category": categories[0],  # 主分类（向后兼容）
                "timestamp": datetime.now().strftime("%H:%M"),
            })

        return memories

    async def acapture(self, text: str) -> List[dict]:
        """异步分析文本并捕获值得记忆的信息"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.capture, text)

    def capture_and_store(
        self, text: str, session_id: str = None
    ) -> List[dict]:
        """分析文本并存储捕获到的记忆到 Qdrant

        Args:
            text: 要分析的文本
            session_id: 会话 ID

        Returns:
            实际存储的记忆列表
        """
        memories = self.capture(text)
        stored: List[dict] = []

        for memory in memories:
            try:
                if self.memory_store:
                    # 新路径：写入 Qdrant
                    memory_id = self.memory_store.add_memory(
                        content=memory["content"],
                        category=memory["category"],
                        session_id=session_id,
                        source="capture",
                    )
                    if memory_id:
                        memory["memory_id"] = memory_id
                        stored.append(memory)
                elif self.workspace:
                    # 旧路径（过渡期兼容）
                    self.workspace.append_classified_memory(
                        content=memory["content"],
                        category=memory["category"],
                    )
                    stored.append(memory)
            except Exception as e:
                print(f"⚠️ 存储记忆失败: {e}")

        return stored

    async def acapture_and_store(
        self, text: str, session_id: str = None
    ) -> List[dict]:
        """异步分析文本并存储捕获到的记忆"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.capture_and_store, text, session_id
        )

    # ── 句子分割 ───────────────────────────────────────

    def _split_sentences(self, text: str) -> List[str]:
        """将文本分割为句子

        支持的分隔符：
        - 中文句号、问号、感叹号、逗号（，）、分号（；）
        - 英文句点、问号、感叹号
        - 换行
        - 中文逗号和分号（新增）
        - 转折/因果连词前断句（但是、但、不过、然而、而且、并且、因为、所以、如果、虽然、因此、此外、否则、然后）

        Args:
            text: 输入文本

        Returns:
            句子列表
        """
        # 分隔符：
        # 1. 中文句末标点 + 中文逗号、分号
        # 2. 英文句末标点
        # 3. 换行
        # 4. 转折/因果连词前断句（不在已由逗号分隔的情况下重复切割）
        separators = r'[。！？；，.!?;]\s*|\n+'
        parts = re.split(separators, text)

        # 对每个部分，在转折/因果连词前进一步分割
        connector_pattern = re.compile(
            r'(?<=.)(?=(?:但是|但|不过|然而|而且|并且|因为|所以|如果|虽然|因此|此外|否则|然后))'
        )

        sentences: List[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            sub_parts = connector_pattern.split(part)
            sentences.extend(s.strip() for s in sub_parts if s.strip())

        return [s for s in sentences if s.strip()]

    # ── 触发匹配 ───────────────────────────────────────

    def _match_trigger(self, sentence: str) -> List[str]:
        """检查句子是否匹配触发规则，返回所有匹配的分类列表

        一条句子可以命中多个分类（如 "我打算明天用 GitHub 账号登录" 同时命中 plan + entity）。

        Args:
            sentence: 要检查的句子

        Returns:
            所有匹配的分类列表（去重），无匹配返回空列表
        """
        matched: List[str] = []
        seen: set = set()

        for pattern, category in self._compiled_patterns:
            if pattern.search(sentence):
                if category not in seen:
                    seen.add(category)
                    matched.append(category)

        return matched

    # ── 记忆提取 ───────────────────────────────────────

    def _extract_memory(self, sentence: str, category: str) -> Optional[str]:
        """从句子中提取记忆内容

        Args:
            sentence: 原始句子
            category: 主分类

        Returns:
            提取的记忆内容
        """
        # 清理句子
        content = sentence.strip()

        # 移除前缀（如"用户："、"我："等）
        content = re.sub(r'^(用户|我|你|assistant|user)[：:]\s*', '', content)

        # 移除引号
        content = content.strip('"\'""''')

        # 如果内容太短，可能是噪声
        if len(content) < 5:
            return None

        # 根据分类进行适当格式化
        if category == "preference":
            if not content.startswith("用户") and not content.startswith("I "):
                content = f"用户{content}"

        return content

    # ── 对话分析 ───────────────────────────────────────

    def analyze_conversation(self, messages: List[dict]) -> List[dict]:
        """分析完整对话并提取记忆

        Args:
            messages: 对话消息列表，每项包含 role 和 content

        Returns:
            捕获到的记忆列表
        """
        all_memories: List[dict] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # 只分析用户消息
            if role == "user" and content:
                memories = self.capture(content)
                all_memories.extend(memories)

        return all_memories

    # ── 分类统计 ───────────────────────────────────────

    def get_category_stats(self) -> dict:
        """获取记忆分类统计（优先从 Qdrant，回退到文件系统）

        Returns:
            各分类的记忆数量统计
        """
        if self.memory_store:
            stats = self.memory_store.get_stats()
            cats = stats.get("categories", {})
            total = stats.get("total_count", 0)
            result = {
                "preference": cats.get("preference", 0),
                "decision": cats.get("decision", 0),
                "entity": cats.get("entity", 0),
                "fact": cats.get("fact", 0),
                "plan": cats.get("plan", 0),
                "relationship": cats.get("relationship", 0),
                "reference": cats.get("reference", 0),
                "rule": cats.get("rule", 0),
                "total": total,
            }
            return result

        # 回退到文件系统统计
        if self.workspace:
            today_path = self.workspace.get_daily_memory_path()
            stats = {
                "preference": 0, "decision": 0, "entity": 0, "fact": 0,
                "plan": 0, "relationship": 0, "reference": 0, "rule": 0, "total": 0,
            }
            try:
                with open(today_path, "r", encoding="utf-8") as f:
                    content = f.read()
                for category in stats:
                    if category != "total":
                        pattern = rf'\[{category}\]'
                        count = len(re.findall(pattern, content, re.IGNORECASE))
                        stats[category] = count
                        stats["total"] += count
            except FileNotFoundError:
                pass
            return stats

        return {"total": 0}
