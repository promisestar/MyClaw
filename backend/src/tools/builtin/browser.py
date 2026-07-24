"""Playwright 浏览器自动化工具 — 线程隔离 + 懒初始化。

跨平台实现：
- headless=True（服务器环境必须无头）
- Linux 下追加 --no-sandbox（容器/root 环境）
- ThreadPoolExecutor 隔离 sync_api 事件循环冲突
- 截图保存到 uploads 目录 + base64 data URL 返回
"""

from __future__ import annotations

import atexit
import base64
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse

_IS_LINUX = sys.platform.startswith("linux")


class BrowserSession:
    """Playwright sync_api 线程隔离管理器。

    在专用 ThreadPoolExecutor 线程中运行 Playwright sync API，
    避免与 uvicorn/FastAPI 事件循环冲突。
    懒初始化：首次 action 时才启动 playwright + launch browser。
    """

    def __init__(self, timeout_config: Any = None, uploads_dir: str = "."):
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="playwright"
        )
        self._timeout_config = timeout_config
        self._uploads_dir = Path(uploads_dir)
        self._playwright = None
        self._browser = None
        _context = None
        self._context = _context
        self._page = None
        self._initialized = False
        self._closed = False
        # 注册解释器退出时的兜底清理，避免 agent.shutdown 未被调用时
        # 残留 chromium 进程阻塞退出（L1）。close() 幂等，重复调用安全。
        atexit.register(self.close)

    @property
    def max_timeout_s(self) -> float:
        if self._timeout_config:
            return self._timeout_config.browser_max_ms / 1000
        return 120.0

    @property
    def default_timeout_s(self) -> float:
        if self._timeout_config:
            return self._timeout_config.browser_default_ms / 1000
        return 30.0

    def execute(self, action: str, **kwargs) -> Dict[str, Any]:
        """线程安全执行浏览器操作（阻塞调用）。

        Args:
            action: 操作名称（navigate/click/type/fill/screenshot/evaluate/text/press/scroll/wait/close）
            **kwargs: 操作参数

        Returns:
            操作结果字典

        Raises:
            RuntimeError: Playwright 未安装或操作失败
        """
        if self._closed:
            raise RuntimeError("BrowserSession 已关闭")

        future = self._executor.submit(self._dispatch, action, kwargs)
        try:
            return future.result(timeout=self.max_timeout_s)
        except Exception as e:
            # 超时或异常时取消 future
            future.cancel()
            raise

    def _ensure_initialized(self) -> None:
        """懒初始化 Playwright + 浏览器（在 executor 线程中调用）。"""
        if self._initialized:
            return

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright 未安装，请运行: pip install playwright && playwright install chromium"
            ) from e

        self._playwright = sync_playwright().start()

        # 跨平台 launch 参数
        launch_args: List[str] = []
        if _IS_LINUX:
            launch_args.append("--no-sandbox")
        launch_args.extend(["--disable-gpu", "--disable-dev-shm-usage"])

        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
        )
        self._page = self._context.new_page()
        self._initialized = True

    def _dispatch(self, action: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """在 executor 线程中执行具体操作。"""
        self._ensure_initialized()

        # 设置页面默认超时
        if self._page:
            self._page.set_default_timeout(self.default_timeout_s * 1000)

        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            return {"error": f"未知 action: {action}"}

        return handler(kwargs)

    # ── 具体操作实现 ──

    def _action_navigate(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        url = kwargs.get("url", "")
        if not url:
            return {"error": "navigate 需要 url 参数"}
        if not url.startswith(("http://", "https://")):
            return {"error": f"url 必须以 http:// 或 https:// 开头: {url}"}

        self._page.goto(url, wait_until="domcontentloaded")
        return {
            "action": "navigate",
            "url": self._page.url,
            "title": self._page.title(),
        }

    def _action_click(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        selector = kwargs.get("selector", "")
        if not selector:
            return {"error": "click 需要 selector 参数"}
        self._page.click(selector)
        return {"action": "click", "selector": selector}

    def _action_type(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        selector = kwargs.get("selector", "")
        text = kwargs.get("text", "")
        if not selector:
            return {"error": "type 需要 selector 参数"}
        self._page.type(selector, text)
        return {"action": "type", "selector": selector, "text_length": len(text)}

    def _action_fill(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        selector = kwargs.get("selector", "")
        value = kwargs.get("value", "")
        if not selector:
            return {"error": "fill 需要 selector 参数"}
        self._page.fill(selector, str(value))
        return {"action": "fill", "selector": selector}

    def _action_screenshot(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        selector = kwargs.get("selector")
        full_page = bool(kwargs.get("full_page", False))

        # 截图
        if selector:
            screenshot_bytes = self._page.locator(selector).screenshot()
        else:
            screenshot_bytes = self._page.screenshot(full_page=full_page)

        # 保存到 uploads 目录
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        # 文件名带毫秒 + 随机后缀，避免同秒截图互相覆盖（L3）
        filename = f"screenshot_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.png"
        filepath = self._uploads_dir / filename
        filepath.write_bytes(screenshot_bytes)

        # base64 data URL
        b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"

        return {
            "action": "screenshot",
            "file_path": str(filepath),
            "data_url": data_url,
            "size_bytes": len(screenshot_bytes),
            "url": self._page.url,
            "title": self._page.title(),
            "selector": selector,
            "full_page": full_page,
        }

    def _action_evaluate(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        script = kwargs.get("script", "")
        if not script:
            return {"error": "evaluate 需要 script 参数"}
        result = self._page.evaluate(script)
        # 结果可能是不可序列化的，转字符串
        try:
            import json
            result_str = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            result_str = str(result)
        return {
            "action": "evaluate",
            "result": result_str,
        }

    def _action_text(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        selector = kwargs.get("selector")
        if selector:
            text = self._page.locator(selector).inner_text()
        else:
            text = self._page.inner_text("body")
        # 截断过长文本
        if len(text) > 10000:
            text = text[:5000] + f"\n\n... (已截断，共 {len(text)} 字符) ...\n" + text[-3000:]
        return {
            "action": "text",
            "text": text,
            "url": self._page.url,
            "selector": selector or "body",
        }

    def _action_press(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        key = kwargs.get("key", "")
        if not key:
            return {"error": "press 需要 key 参数"}
        self._page.keyboard.press(key)
        return {"action": "press", "key": key}

    def _action_scroll(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        x = int(kwargs.get("x", 0))
        y = int(kwargs.get("y", 0))
        self._page.evaluate(f"window.scrollTo({x}, {y})")
        return {"action": "scroll", "x": x, "y": y}

    def _action_wait(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        condition = kwargs.get("condition", "load")
        # 钳制超时到 max_timeout_s，防止 LLM 传入超大值永久卡死浏览器会话（M5）
        raw_timeout = kwargs.get("timeout", self.default_timeout_s)
        try:
            timeout_s = float(raw_timeout)
        except (TypeError, ValueError):
            timeout_s = self.default_timeout_s
        timeout_s = min(max(timeout_s, 1.0), self.max_timeout_s)
        timeout_ms = int(timeout_s * 1000)

        if condition == "load":
            self._page.wait_for_load_state("load", timeout=timeout_ms)
        elif condition == "networkidle":
            self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        elif condition == "domcontentloaded":
            self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        elif condition.startswith("selector:"):
            selector = condition[len("selector:"):]
            self._page.wait_for_selector(selector, timeout=timeout_ms)
        elif condition.startswith("timeout:"):
            # 钳制 sleep 到 max_timeout_s，防止永久卡死单 worker 线程（M5）
            seconds = float(condition[len("timeout:"):])
            seconds = min(max(seconds, 0.0), self.max_timeout_s)
            time.sleep(seconds)
        else:
            return {"error": f"未知 wait condition: {condition}"}

        return {"action": "wait", "condition": condition}

    def _action_close(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self._cleanup()
        return {"action": "close"}

    def _cleanup(self) -> None:
        """清理 Playwright 资源（在 executor 线程中调用）。"""
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._initialized = False

    def close(self) -> None:
        """清理资源（agent shutdown 时调用，三端安全）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.submit(self._cleanup).result(timeout=10)
        except Exception:
            pass
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


class BrowserTool(Tool):
    """Playwright 浏览器自动化工具。

    通过 action 参数路由到具体操作：
    navigate / click / type / fill / screenshot / evaluate / text / press / scroll / wait / close

    底层 BrowserSession 在专用线程中运行 Playwright sync_api，
    避免与事件循环冲突。懒初始化，首次使用时才启动浏览器。
    跨平台：Windows/Linux/macOS 通用，headless 模式。
    """

    def __init__(self, session: BrowserSession):
        super().__init__(
            name="browser",
            description=(
                "Playwright 浏览器自动化工具。通过 action 参数执行操作：\n"
                "- navigate: 导航到 URL（参数: url）\n"
                "- click: 点击元素（参数: selector）\n"
                "- type: 在元素中输入文本（参数: selector, text）\n"
                "- fill: 填充表单（参数: selector, value）\n"
                "- screenshot: 截图（参数: selector?, full_page?）\n"
                "- evaluate: 执行 JavaScript（参数: script）\n"
                "- text: 提取文本（参数: selector?）\n"
                "- press: 按键（参数: key）\n"
                "- scroll: 滚动（参数: x, y）\n"
                "- wait: 等待（参数: condition, timeout?）\n"
                "- close: 关闭浏览器\n"
                "selector 使用 CSS 选择器语法。跨平台 headless 模式。"
            ),
            expandable=False,
        )
        self._session = session
        # ContextGuard 元数据
        self.output_size_hint = 4000
        self.has_side_effects = True  # 有状态（page 持久化），不可委托

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作名称：navigate/click/type/fill/screenshot/evaluate/text/press/scroll/wait/close",
                required=True,
            ),
            ToolParameter(
                name="url",
                type="string",
                description="navigate 时的目标 URL（http:// 或 https://）",
                required=False,
            ),
            ToolParameter(
                name="selector",
                type="string",
                description="CSS 选择器（click/type/fill/screenshot/text 时使用）",
                required=False,
            ),
            ToolParameter(
                name="text",
                type="string",
                description="type 时要输入的文本",
                required=False,
            ),
            ToolParameter(
                name="value",
                type="string",
                description="fill 时要填充的值",
                required=False,
            ),
            ToolParameter(
                name="script",
                type="string",
                description="evaluate 时要执行的 JavaScript 代码",
                required=False,
            ),
            ToolParameter(
                name="key",
                type="string",
                description="press 时的按键（如 Enter, Tab, Escape）",
                required=False,
            ),
            ToolParameter(
                name="full_page",
                type="boolean",
                description="screenshot 时是否截整页（默认 false）",
                required=False,
            ),
            ToolParameter(
                name="x",
                type="integer",
                description="scroll 时的水平滚动位置（默认 0）",
                required=False,
            ),
            ToolParameter(
                name="y",
                type="integer",
                description="scroll 时的垂直滚动位置（默认 0）",
                required=False,
            ),
            ToolParameter(
                name="condition",
                type="string",
                description="wait 的等待条件：load|networkidle|domcontentloaded|selector:<css>|timeout:<秒>",
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="number",
                description="wait 的超时秒数（默认 30）",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        action = (parameters.get("action") or "").strip().lower()
        if not action:
            return ToolResponse.error(code="INVALID_INPUT", message="action 不能为空")

        # 收集非 None 参数
        kwargs: Dict[str, Any] = {}
        for key in ("url", "selector", "text", "value", "script", "key",
                     "full_page", "x", "y", "condition", "timeout"):
            val = parameters.get(key)
            if val is not None:
                kwargs[key] = val

        try:
            result = self._session.execute(action, **kwargs)
        except RuntimeError as e:
            return ToolResponse.error(
                code="BROWSER_ERROR",
                message=str(e),
            )
        except Exception as e:
            return ToolResponse.error(
                code="BROWSER_ERROR",
                message=f"浏览器操作失败: {e}",
            )

        # 检查结果中是否有错误
        if "error" in result:
            return ToolResponse.error(
                code="ACTION_FAILED",
                message=result["error"],
            )

        # 构建输出文本
        text = self._format_result(action, result)

        return ToolResponse.success(text=text, data=result)

    def _format_result(self, action: str, result: Dict[str, Any]) -> str:
        """将操作结果格式化为 LLM 可读文本。"""
        if action == "navigate":
            return f"已导航到: {result.get('url', '?')}\n页面标题: {result.get('title', '?')}"
        elif action == "click":
            return f"已点击元素: {result.get('selector', '?')}"
        elif action == "type":
            return f"已在 {result.get('selector', '?')} 中输入 {result.get('text_length', 0)} 个字符"
        elif action == "fill":
            return f"已填充表单: {result.get('selector', '?')}"
        elif action == "screenshot":
            size_kb = result.get("size_bytes", 0) / 1024
            return (
                f"截图已保存: {result.get('file_path', '?')}\n"
                f"页面: {result.get('url', '?')}\n"
                f"标题: {result.get('title', '?')}\n"
                f"大小: {size_kb:.1f} KB\n"
                f"data_url: {result.get('data_url', '')[:100]}..."
            )
        elif action == "evaluate":
            return f"JavaScript 执行结果:\n{result.get('result', '(无返回值)')}"
        elif action == "text":
            return f"页面文本（{result.get('selector', 'body')}）:\n{result.get('text', '')}"
        elif action == "press":
            return f"已按键: {result.get('key', '?')}"
        elif action == "scroll":
            return f"已滚动到 ({result.get('x', 0)}, {result.get('y', 0)})"
        elif action == "wait":
            return f"等待完成: {result.get('condition', '?')}"
        elif action == "close":
            return "浏览器已关闭"
        else:
            return f"操作完成: {action}"

    def close(self) -> None:
        """清理浏览器资源（由 agent shutdown 循环自动调用）。"""
        if self._session:
            self._session.close()
