"""定时任务持久化层 — JSON 文件存储，原子写入。

跨平台：os.replace 三端原子，tempfile 安全创建。
存储路径：.myclaw/automations/automations.json + runs/{task_id}/{run_id}.json
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import AutomationRun, AutomationTask


class AutomationStore:
    """定时任务持久化存储。

    线程安全：通过 threading.Lock 保护 load-modify-save 复合操作，
    避免跨线程（事件循环线程 vs worker 线程）后写覆盖先写导致丢更新。
    原子写入（tempfile + os.replace）防撕裂，不使用平台专属文件锁（fcntl/msvcrt）。
    """

    def __init__(self, automations_dir: str):
        """初始化存储。

        Args:
            automations_dir: automations 目录路径（.myclaw/automations/）
        """
        self._dir = Path(automations_dir)
        self._tasks_file = self._dir / "automations.json"
        self._runs_dir = self._dir / "runs"
        # 复合操作（load-modify-save）线程锁，跨平台通用
        self._lock = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """确保目录结构存在。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._runs_dir.mkdir(parents=True, exist_ok=True)

    # ── 任务 CRUD ──

    def load_tasks(self) -> List[AutomationTask]:
        """加载所有定时任务。"""
        if not self._tasks_file.is_file():
            return []
        try:
            text = self._tasks_file.read_text(encoding="utf-8", errors="replace")
            data = json.loads(text)
            if not isinstance(data, list):
                return []
            return [AutomationTask.from_dict(item) for item in data if isinstance(item, dict)]
        except (json.JSONDecodeError, OSError):
            return []

    def save_tasks(self, tasks: List[AutomationTask]) -> None:
        """原子写入所有任务到 JSON 文件。"""
        self._ensure_dirs()
        data = [task.to_dict() for task in tasks]
        self._atomic_write_json(self._tasks_file, data)

    def get_task(self, task_id: str) -> Optional[AutomationTask]:
        """获取单个任务。"""
        for task in self.load_tasks():
            if task.id == task_id:
                return task
        return None

    def list_tasks(self, enabled_only: bool = False) -> List[AutomationTask]:
        """列出所有任务。"""
        tasks = self.load_tasks()
        if enabled_only:
            tasks = [t for t in tasks if t.enabled]
        return tasks

    def create_task(self, task: AutomationTask) -> AutomationTask:
        """创建新任务。"""
        with self._lock:
            tasks = self.load_tasks()
            tasks.append(task)
            self.save_tasks(tasks)
        return task

    def update_task(self, task: AutomationTask) -> bool:
        """更新任务。"""
        with self._lock:
            tasks = self.load_tasks()
            for i, t in enumerate(tasks):
                if t.id == task.id:
                    tasks[i] = task
                    self.save_tasks(tasks)
                    return True
        return False

    def delete_task(self, task_id: str) -> bool:
        """删除任务（同时删除运行记录）。"""
        with self._lock:
            tasks = self.load_tasks()
            new_tasks = [t for t in tasks if t.id != task_id]
            if len(new_tasks) == len(tasks):
                return False
            self.save_tasks(new_tasks)
        # 清理运行记录目录
        task_runs_dir = self._runs_dir / task_id
        if task_runs_dir.exists():
            import shutil
            shutil.rmtree(task_runs_dir, ignore_errors=True)
        return True

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        """启用/禁用任务。"""
        with self._lock:
            task = self.get_task(task_id)
            if task is None:
                return False
            task.enabled = enabled
            # update_task 内部也会加锁，此处直接 save 避免重入死锁
            tasks = self.load_tasks()
            for i, t in enumerate(tasks):
                if t.id == task_id:
                    tasks[i] = task
                    self.save_tasks(tasks)
                    return True
        return False

    # ── 运行记录 ──

    def save_run(self, run: AutomationRun) -> None:
        """保存执行记录到 runs/{task_id}/{run_id}.json。"""
        task_runs_dir = self._runs_dir / run.task_id
        task_runs_dir.mkdir(parents=True, exist_ok=True)
        run_file = task_runs_dir / f"{run.run_id}.json"
        self._atomic_write_json(run_file, run.to_dict())

    def list_runs(self, task_id: str, limit: int = 20) -> List[AutomationRun]:
        """列出任务的执行记录（按时间倒序）。"""
        task_runs_dir = self._runs_dir / task_id
        if not task_runs_dir.is_dir():
            return []
        runs: List[AutomationRun] = []
        for entry in task_runs_dir.iterdir():
            if not entry.name.endswith(".json"):
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8", errors="replace"))
                runs.append(AutomationRun.from_dict(data))
            except (json.JSONDecodeError, OSError):
                continue
        # 按 started_at 倒序
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    # ── 内部工具 ──

    @staticmethod
    def _atomic_write_json(filepath: Path, data: Any) -> None:
        """原子写入 JSON 文件（跨平台安全）。

        使用 tempfile + os.replace，三端原子操作。
        """
        filepath.parent.mkdir(parents=True, exist_ok=True)
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        # 写入临时文件
        fd, tmp_path = tempfile.mkstemp(
            dir=str(filepath.parent),
            suffix=".tmp",
            prefix=filepath.stem,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json_str)
            # 原子替换（os.replace 三端原子，比 os.rename 在 Windows 上更可靠）
            os.replace(tmp_path, str(filepath))
        except Exception:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
