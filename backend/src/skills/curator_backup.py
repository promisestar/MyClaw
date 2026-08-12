"""Curator 变更前技能目录快照（tar.gz）与回滚。"""

from __future__ import annotations

import json
import logging
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 5
_EXCLUDE = {".curator_backups", ".venv", "__pycache__", "node_modules"}


def _backups_dir(skills_dir: Path) -> Path:
    return Path(skills_dir) / ".curator_backups"


def _snapshot_id() -> str:
    # Windows-safe：冒号换成短横线
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def snapshot_skills(
    skills_dir: Path,
    *,
    keep: int = DEFAULT_KEEP,
    reason: str = "manual",
) -> Dict[str, Any]:
    skills_dir = Path(skills_dir)
    if not skills_dir.exists():
        return {"ok": False, "error": f"skills_dir missing: {skills_dir}"}

    backups = _backups_dir(skills_dir)
    backups.mkdir(parents=True, exist_ok=True)
    snap_id = _snapshot_id()
    dest_dir = backups / snap_id
    # 同秒冲突
    n = 0
    while dest_dir.exists():
        n += 1
        dest_dir = backups / f"{snap_id}-{n:02d}"
        snap_id = dest_dir.name
    dest_dir.mkdir(parents=True, exist_ok=False)

    tar_path = dest_dir / "skills.tar.gz"
    try:
        with tarfile.open(tar_path, "w:gz") as tar:
            for child in skills_dir.iterdir():
                if child.name in _EXCLUDE:
                    continue
                if child.name.startswith(".curator_backups"):
                    continue
                tar.add(child, arcname=child.name)
        manifest = {
            "id": snap_id,
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "skills_dir": str(skills_dir),
            "tar": str(tar_path.name),
            "size_bytes": tar_path.stat().st_size,
        }
        (dest_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        logger.warning("snapshot failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    _prune(backups, keep=keep)
    return {"ok": True, "id": snap_id, "path": str(dest_dir), "manifest": manifest}


def list_snapshots(skills_dir: Path) -> List[Dict[str, Any]]:
    backups = _backups_dir(Path(skills_dir))
    if not backups.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for d in sorted(backups.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        man = d / "manifest.json"
        if man.exists():
            try:
                rows.append(json.loads(man.read_text(encoding="utf-8")))
                continue
            except (OSError, json.JSONDecodeError):
                pass
        rows.append({"id": d.name, "path": str(d)})
    return rows


def rollback(skills_dir: Path, snapshot_id: str) -> Dict[str, Any]:
    """用指定快照覆盖当前 skills 目录内容（先再拍一份当前状态）。"""
    skills_dir = Path(skills_dir)
    backups = _backups_dir(skills_dir)
    dest = backups / snapshot_id
    tar_path = dest / "skills.tar.gz"
    if not tar_path.exists():
        return {"ok": False, "error": f"snapshot not found: {snapshot_id}"}

    # 回滚前再备份当前树
    pre = snapshot_skills(skills_dir, reason=f"pre-rollback-{snapshot_id}")
    if not pre.get("ok"):
        return {"ok": False, "error": f"pre-rollback snapshot failed: {pre.get('error')}"}

    # 清空当前（保留 .curator_backups）
    for child in list(skills_dir.iterdir()):
        if child.name == ".curator_backups":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass

    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(path=skills_dir)

    return {
        "ok": True,
        "restored": snapshot_id,
        "pre_rollback_snapshot": pre.get("id"),
    }


def _prune(backups: Path, *, keep: int) -> None:
    dirs = sorted(
        [d for d in backups.iterdir() if d.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for d in dirs[max(0, keep) :]:
        shutil.rmtree(d, ignore_errors=True)
