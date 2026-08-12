"""Skill usage 遥测 sidecar（Curator / skill_manage 共用）。

每个技能根目录各有一份 ``.usage.json``（工作区与全局互不共享）：
- ``<workspace>/.myclaw/skills/.usage.json``
- ``~/.helloclaw/skills/.usage.json``

设计要点：
- 遥测与 SKILL.md 分离，避免污染用户正文
- 原子写（tempfile + os.replace）+ 跨进程文件锁
- 计数失败只记 DEBUG，不打断工具主路径
- ``created_by: "agent"`` 是 Curator 管理 opt-in 策略标志，不是严格作者证明
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}

USAGE_FILENAME = ".usage.json"
ARCHIVE_DIRNAME = ".archive"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def latest_activity_at(record: Dict[str, Any]) -> Optional[str]:
    """最近一次 use/view/patch 时间（不含 created_at）。"""
    latest_dt: Optional[datetime] = None
    latest_raw: Optional[str] = None
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        raw = record.get(key)
        dt = _parse_iso_timestamp(raw)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_raw = str(raw)
    return latest_raw


def activity_count(record: Dict[str, Any]) -> int:
    total = 0
    for key in ("use_count", "view_count", "patch_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def empty_record() -> Dict[str, Any]:
    return {
        "created_by": None,
        "use_count": 0,
        "view_count": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "patch_count": 0,
        "patch_generation": 0,
        "last_reused_patch_generation": 0,
        "last_patched_at": None,
        "created_at": _now_iso(),
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
    }


def is_curator_managed_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    return record.get("created_by") == "agent" or record.get("agent_created") is True


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class SkillUsageStore:
    """绑定到单个 skills 根目录的 usage sidecar。"""

    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.usage_path = self.skills_dir / USAGE_FILENAME
        self.archive_dir = self.skills_dir / ARCHIVE_DIRNAME

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    @contextmanager
    def _file_lock(self):
        lock_path = self.usage_path.with_suffix(".json.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
            lock_path.write_text(" ", encoding="utf-8")

        fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    def load(self) -> Dict[str, Dict[str, Any]]:
        if not self.usage_path.exists():
            return {}
        try:
            data = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("Failed to read %s: %s", self.usage_path, e)
            return {}
        if not isinstance(data, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                clean[str(k)] = v
        return clean

    def save(self, data: Dict[str, Dict[str, Any]]) -> bool:
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.skills_dir), prefix=".usage_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.usage_path)
                return True
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.debug("Failed to write %s: %s", self.usage_path, e, exc_info=True)
            return False

    def get_record(self, skill_name: str) -> Dict[str, Any]:
        data = self.load()
        rec = data.get(skill_name)
        if not isinstance(rec, dict):
            return empty_record()
        base = empty_record()
        for k, v in base.items():
            rec.setdefault(k, v)
        return rec

    def _mutate(
        self,
        skill_name: str,
        mutator: Callable[[Dict[str, Any]], Any],
    ) -> Any:
        if not skill_name:
            return None
        try:
            with self._file_lock():
                data = self.load()
                rec = data.get(skill_name)
                if not isinstance(rec, dict):
                    rec = empty_record()
                result = mutator(rec)
                data[skill_name] = rec
                if not self.save(data):
                    return None
                return result
        except Exception as e:
            logger.debug(
                "SkillUsageStore._mutate(%s) failed: %s", skill_name, e, exc_info=True
            )
            return None

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------

    def bump_view(self, skill_name: str) -> None:
        def _apply(rec: Dict[str, Any]) -> None:
            rec["view_count"] = _non_negative_int(rec.get("view_count")) + 1
            rec["last_viewed_at"] = _now_iso()

        self._mutate(skill_name, _apply)

    def bump_use(self, skill_name: str) -> None:
        def _apply(rec: Dict[str, Any]) -> None:
            previous = _non_negative_int(rec.get("use_count"))
            patch_generation = _non_negative_int(rec.get("patch_generation"))
            last_reused = min(
                _non_negative_int(rec.get("last_reused_patch_generation")),
                patch_generation,
            )
            reused = previous > 0
            reuse_after_patch = reused and patch_generation > last_reused
            rec["use_count"] = previous + 1
            rec["last_used_at"] = _now_iso()
            rec["patch_generation"] = patch_generation
            rec["last_reused_patch_generation"] = (
                patch_generation if reuse_after_patch else last_reused
            )

        self._mutate(skill_name, _apply)

    def bump_patch(self, skill_name: str) -> None:
        def _apply(rec: Dict[str, Any]) -> None:
            rec["patch_count"] = _non_negative_int(rec.get("patch_count")) + 1
            rec["patch_generation"] = _non_negative_int(rec.get("patch_generation")) + 1
            rec["last_patched_at"] = _now_iso()

        self._mutate(skill_name, _apply)

    def record_created(self, skill_name: str, *, agent_created: bool) -> None:
        def _apply(rec: Dict[str, Any]) -> None:
            rec.clear()
            rec.update(empty_record())
            if agent_created:
                rec["created_by"] = "agent"

        self._mutate(skill_name, _apply)

    def mark_agent_created(self, skill_name: str) -> None:
        def _apply(rec: Dict[str, Any]) -> None:
            rec["created_by"] = "agent"

        self._mutate(skill_name, _apply)

    def set_state(self, skill_name: str, state: str) -> None:
        if state not in _VALID_STATES:
            logger.debug("set_state: invalid state %r for %s", state, skill_name)
            return

        def _apply(rec: Dict[str, Any]) -> None:
            if rec.get("state") == state:
                return
            rec["state"] = state
            if state == STATE_ARCHIVED:
                rec["archived_at"] = _now_iso()
            elif state == STATE_ACTIVE:
                rec["archived_at"] = None

        self._mutate(skill_name, _apply)

    def set_pinned(self, skill_name: str, pinned: bool) -> None:
        def _apply(rec: Dict[str, Any]) -> None:
            rec["pinned"] = bool(pinned)

        self._mutate(skill_name, _apply)

    def is_pinned(self, skill_name: str) -> bool:
        return bool(self.get_record(skill_name).get("pinned"))

    def is_curator_managed(self, skill_name: str) -> bool:
        return is_curator_managed_record(self.get_record(skill_name))

    def forget(self, skill_name: str) -> None:
        if not skill_name:
            return
        try:
            with self._file_lock():
                data = self.load()
                if skill_name in data:
                    del data[skill_name]
                    self.save(data)
        except Exception as e:
            logger.debug(
                "SkillUsageStore.forget(%s) failed: %s", skill_name, e, exc_info=True
            )

    def seed_record_if_missing(self, skill_name: str) -> None:
        if not skill_name:
            return
        try:
            with self._file_lock():
                data = self.load()
                if isinstance(data.get(skill_name), dict):
                    return
                data[skill_name] = empty_record()
                self.save(data)
        except Exception as e:
            logger.debug(
                "seed_record_if_missing(%s) failed: %s", skill_name, e, exc_info=True
            )

    def ensure_created_at(self, skill_name: str) -> bool:
        """确保记录存在且 ``created_at`` 非空。

        Returns:
            True 表示新建记录或回填了 created_at（调用方宜推迟一轮转换）。
        """
        if not skill_name:
            return False
        try:
            with self._file_lock():
                data = self.load()
                rec = data.get(skill_name)
                if not isinstance(rec, dict):
                    data[skill_name] = empty_record()
                    self.save(data)
                    return True
                if not rec.get("created_at"):
                    rec["created_at"] = _now_iso()
                    data[skill_name] = rec
                    self.save(data)
                    return True
                return False
        except Exception as e:
            logger.debug(
                "ensure_created_at(%s) failed: %s", skill_name, e, exc_info=True
            )
            return False

    def rename_record(self, old_name: str, new_name: str) -> None:
        """技能改名时迁移 usage（保留 pin / created_by / 计数）。"""
        if not old_name or not new_name or old_name == new_name:
            return
        try:
            with self._file_lock():
                data = self.load()
                old = data.get(old_name)
                if not isinstance(old, dict):
                    return
                # 新名若已有空壳记录，以旧记录为准（护栏/计数更完整）
                data[new_name] = old
                del data[old_name]
                self.save(data)
        except Exception as e:
            logger.debug(
                "rename_record(%s -> %s) failed: %s",
                old_name,
                new_name,
                e,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def curated_report(self) -> List[Dict[str, Any]]:
        """仅返回 curator-managed 技能的汇总行。"""
        rows: List[Dict[str, Any]] = []
        for name, rec in self.load().items():
            if not is_curator_managed_record(rec):
                continue
            base = empty_record()
            for k, v in base.items():
                rec.setdefault(k, v)
            rows.append(
                {
                    "name": name,
                    "created_by": rec.get("created_by"),
                    "use_count": _non_negative_int(rec.get("use_count")),
                    "view_count": _non_negative_int(rec.get("view_count")),
                    "patch_count": _non_negative_int(rec.get("patch_count")),
                    "state": rec.get("state") or STATE_ACTIVE,
                    "pinned": bool(rec.get("pinned")),
                    "created_at": rec.get("created_at"),
                    "last_activity_at": latest_activity_at(rec),
                    "archived_at": rec.get("archived_at"),
                }
            )
        rows.sort(key=lambda r: r["name"])
        return rows

    def usage_report(self) -> List[Dict[str, Any]]:
        """全部遥测记录（含未 adopt 的用户技能）。"""
        rows: List[Dict[str, Any]] = []
        for name, rec in self.load().items():
            base = empty_record()
            for k, v in base.items():
                rec.setdefault(k, v)
            rows.append(
                {
                    "name": name,
                    "created_by": rec.get("created_by"),
                    "use_count": _non_negative_int(rec.get("use_count")),
                    "view_count": _non_negative_int(rec.get("view_count")),
                    "patch_count": _non_negative_int(rec.get("patch_count")),
                    "state": rec.get("state") or STATE_ACTIVE,
                    "pinned": bool(rec.get("pinned")),
                    "created_at": rec.get("created_at"),
                    "last_activity_at": latest_activity_at(rec),
                    "archived_at": rec.get("archived_at"),
                    "curator_managed": is_curator_managed_record(rec),
                }
            )
        rows.sort(key=lambda r: r["name"])
        return rows

    def list_archived_names(self) -> List[str]:
        if not self.archive_dir.exists():
            return []
        return sorted(
            {p.name for p in self.archive_dir.iterdir() if p.is_dir()}
        )
