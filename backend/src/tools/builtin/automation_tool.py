"""Agent 自助定时任务工具 — 通过 action 参数 CRUD 定时任务。

action: create / list / get / delete / enable / disable
通过 AutomationStore 引用操作持久化数据。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse


class AutomationTool(Tool):
    """定时任务管理工具（Agent 自助）。

    通过 action 参数执行 CRUD 操作：
    - create: 创建定时任务
    - list: 列出所有任务
    - get: 查看任务详情
    - delete: 删除任务
    - enable: 启用任务
    - disable: 禁用任务

    调度类型：
    - once: 一次性任务（schedule_config: {"datetime": "ISO8601"}）
    - interval: 间隔循环（schedule_config: {"interval_seconds": 3600}）
    - rrule: RFC 5545 RRULE（schedule_config: {"rrule": "FREQ=DAILY;BYHOUR=9"}）
    """

    def __init__(self, store_getter=None):
        """初始化。

        Args:
            store_getter: 返回 AutomationStore 实例的回调（延迟绑定，
                         避免 _setup_tools 时 store 尚未创建）
        """
        super().__init__(
            name="automation",
            description=(
                "定时任务管理工具。通过 action 参数执行操作：\n"
                "- create: 创建定时任务（参数: name, prompt, schedule_type, schedule_config, webhook_url?）\n"
                "- list: 列出所有任务\n"
                "- get: 查看任务详情（参数: task_id）\n"
                "- delete: 删除任务（参数: task_id）\n"
                "- enable: 启用任务（参数: task_id）\n"
                "- disable: 禁用任务（参数: task_id）\n"
                "schedule_type: once（一次性）| interval（间隔循环）| rrule（RFC 5545）\n"
                "时间均为 UTC，格式 ISO 8601。"
            ),
            expandable=False,
        )
        self._store_getter = store_getter
        # ContextGuard 元数据
        self.output_size_hint = 500
        self.has_side_effects = True  # 副作用（创建/删除任务），不可委托

    def _get_store(self):
        """获取 AutomationStore 实例。"""
        if self._store_getter:
            return self._store_getter()
        return None

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作名称：create/list/get/delete/enable/disable",
                required=True,
            ),
            ToolParameter(
                name="name",
                type="string",
                description="create 时的任务名称",
                required=False,
            ),
            ToolParameter(
                name="prompt",
                type="string",
                description="create 时要执行的 prompt（Agent 将在定时触发时处理此 prompt）",
                required=False,
            ),
            ToolParameter(
                name="schedule_type",
                type="string",
                description="create 时的调度类型：once | interval | rrule",
                required=False,
            ),
            ToolParameter(
                name="schedule_config",
                type="string",
                description='调度配置 JSON：once: {"datetime":"2026-03-20T14:30:00+00:00"} | interval: {"interval_seconds":3600} | rrule: {"rrule":"FREQ=DAILY;BYHOUR=9"}',
                required=False,
            ),
            ToolParameter(
                name="webhook_url",
                type="string",
                description="结果投递 webhook URL（http/https，可选）",
                required=False,
            ),
            ToolParameter(
                name="task_id",
                type="string",
                description="get/delete/enable/disable 时的任务 ID",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        store = self._get_store()
        if store is None:
            return ToolResponse.error(
                code="STORE_UNAVAILABLE",
                message="AutomationStore 未初始化",
            )

        action = (parameters.get("action") or "").strip().lower()
        if not action:
            return ToolResponse.error(code="INVALID_INPUT", message="action 不能为空")

        if action == "create":
            return self._create(store, parameters)
        elif action == "list":
            return self._list(store)
        elif action == "get":
            return self._get(store, parameters)
        elif action == "delete":
            return self._delete(store, parameters)
        elif action == "enable":
            return self._toggle(store, parameters, True)
        elif action == "disable":
            return self._toggle(store, parameters, False)
        else:
            return ToolResponse.error(
                code="INVALID_ACTION",
                message=f"未知 action: {action}，支持: create/list/get/delete/enable/disable",
            )

    def _create(self, store, parameters: Dict[str, Any]) -> ToolResponse:
        from ..automation.models import AutomationTask

        name = (parameters.get("name") or "").strip()
        prompt = (parameters.get("prompt") or "").strip()
        schedule_type = (parameters.get("schedule_type") or "once").strip().lower()
        schedule_config_raw = parameters.get("schedule_config") or "{}"
        webhook_url = parameters.get("webhook_url") or None

        if not name:
            return ToolResponse.error(code="INVALID_INPUT", message="name 不能为空")
        if not prompt:
            return ToolResponse.error(code="INVALID_INPUT", message="prompt 不能为空")
        if schedule_type not in ("once", "interval", "rrule"):
            return ToolResponse.error(
                code="INVALID_INPUT",
                message=f"schedule_type 必须是 once/interval/rrule， got {schedule_type}",
            )

        # 解析 schedule_config
        try:
            if isinstance(schedule_config_raw, dict):
                schedule_config = schedule_config_raw
            else:
                schedule_config = json.loads(schedule_config_raw)
        except json.JSONDecodeError as e:
            return ToolResponse.error(
                code="INVALID_CONFIG",
                message=f"schedule_config JSON 解析失败: {e}",
            )

        # 验证 webhook URL
        if webhook_url and not webhook_url.startswith(("http://", "https://")):
            return ToolResponse.error(
                code="INVALID_WEBHOOK",
                message="webhook_url 必须以 http:// 或 https:// 开头",
            )

        # 创建任务
        task = AutomationTask(
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_config=schedule_config,
            webhook_url=webhook_url,
            enabled=True,
        )

        # 计算首次运行时间
        next_run = task.compute_next_run()
        if next_run is None:
            return ToolResponse.error(
                code="NO_SCHEDULE",
                message="无法计算下次运行时间，请检查 schedule_config",
            )
        task.next_run_at = next_run.isoformat()

        store.create_task(task)

        return ToolResponse.success(
            text=f"定时任务已创建\nID: {task.id}\n名称: {task.name}\n类型: {task.schedule_type}\n下次运行: {task.next_run_at}",
            data=task.to_dict(),
        )

    def _list(self, store) -> ToolResponse:
        tasks = store.list_tasks()
        if not tasks:
            return ToolResponse.success(text="(无定时任务)", data={"count": 0})

        lines = [f"共 {len(tasks)} 个定时任务：\n"]
        for t in tasks:
            status = "启用" if t.enabled else "禁用"
            lines.append(
                f"- [{t.id}] {t.name} ({t.schedule_type}) [{status}] "
                f"下次: {t.next_run_at or 'N/A'} 运行次数: {t.run_count}"
            )

        return ToolResponse.success(
            text="\n".join(lines),
            data={
                "count": len(tasks),
                "tasks": [t.to_dict() for t in tasks],
            },
        )

    def _get(self, store, parameters: Dict[str, Any]) -> ToolResponse:
        task_id = (parameters.get("task_id") or "").strip()
        if not task_id:
            return ToolResponse.error(code="INVALID_INPUT", message="task_id 不能为空")

        task = store.get_task(task_id)
        if task is None:
            return ToolResponse.error(code="NOT_FOUND", message=f"任务不存在: {task_id}")

        # 获取最近运行记录
        runs = store.list_runs(task_id, limit=5)

        lines = [
            f"任务详情",
            f"ID: {task.id}",
            f"名称: {task.name}",
            f"Prompt: {task.prompt[:200]}{'...' if len(task.prompt) > 200 else ''}",
            f"调度类型: {task.schedule_type}",
            f"调度配置: {json.dumps(task.schedule_config, ensure_ascii=False)}",
            f"状态: {'启用' if task.enabled else '禁用'}",
            f"创建时间: {task.created_at}",
            f"上次运行: {task.last_run_at or 'N/A'}",
            f"下次运行: {task.next_run_at or 'N/A'}",
            f"运行次数: {task.run_count}",
            f"Webhook: {task.webhook_url or '无'}",
        ]

        if runs:
            lines.append(f"\n最近 {len(runs)} 次运行：")
            for r in runs:
                status = "成功" if r.success else f"失败({r.error})"
                lines.append(f"  - {r.started_at} [{status}]")

        return ToolResponse.success(
            text="\n".join(lines),
            data={
                "task": task.to_dict(),
                "recent_runs": [r.to_dict() for r in runs],
            },
        )

    def _delete(self, store, parameters: Dict[str, Any]) -> ToolResponse:
        task_id = (parameters.get("task_id") or "").strip()
        if not task_id:
            return ToolResponse.error(code="INVALID_INPUT", message="task_id 不能为空")

        if store.delete_task(task_id):
            return ToolResponse.success(
                text=f"任务已删除: {task_id}",
                data={"deleted": task_id},
            )
        return ToolResponse.error(code="NOT_FOUND", message=f"任务不存在: {task_id}")

    def _toggle(
        self,
        store,
        parameters: Dict[str, Any],
        enabled: bool,
    ) -> ToolResponse:
        task_id = (parameters.get("task_id") or "").strip()
        if not task_id:
            return ToolResponse.error(code="INVALID_INPUT", message="task_id 不能为空")

        if store.set_enabled(task_id, enabled):
            action = "启用" if enabled else "禁用"
            return ToolResponse.success(
                text=f"任务已{action}: {task_id}",
                data={"task_id": task_id, "enabled": enabled},
            )
        return ToolResponse.error(code="NOT_FOUND", message=f"任务不存在: {task_id}")
