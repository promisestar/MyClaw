"""Curator — Skill 生命周期维护（确定性状态机）。

空闲 + 距上次运行超过 interval 时，对 curator-managed（created_by=agent）技能：
active → stale → archive。永不硬删；pin 技能跳过。

LLM consolidate 由 consolidate=true 触发（见 run_llm_consolidate，默认关闭）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .usage import (
    SkillUsageStore,
    STATE_ACTIVE,
    STATE_STALE,
    STATE_ARCHIVED,
    latest_activity_at,
)
from . import curator_backup

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 24 * 7
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90
DEFAULT_CONSOLIDATE = False

CURATOR_STATE_FILENAME = ".curator_state"

# 进程级：最近一次聊天活动（供 idle 门控）
_last_chat_activity_at: Optional[datetime] = None


def mark_chat_activity() -> None:
    global _last_chat_activity_at
    _last_chat_activity_at = datetime.now(timezone.utc)


def idle_for_seconds() -> float:
    if _last_chat_activity_at is None:
        return float("inf")
    return (datetime.now(timezone.utc) - _last_chat_activity_at).total_seconds()


def idle_for_seconds_json() -> Optional[float]:
    """HTTP/JSON 安全的空闲秒数；尚无聊天活动时返回 None（内部调度仍用 inf）。"""
    seconds = idle_for_seconds()
    if not math.isfinite(seconds):
        return None
    return seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _default_state() -> Dict[str, Any]:
    return {
        "last_run_at": None,
        "last_run_duration_seconds": None,
        "last_run_summary": None,
        "last_report_path": None,
        "paused": False,
        "run_count": 0,
    }


def load_curator_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 config dict 或 ~/.helloclaw/config.json 读取 curator 段。"""
    cfg = config
    if cfg is None:
        path = Path(os.path.expanduser("~/.helloclaw/config.json"))
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                cfg = raw if isinstance(raw, dict) else {}
            except (OSError, json.JSONDecodeError):
                cfg = {}
        else:
            cfg = {}
    cur = cfg.get("curator") if isinstance(cfg, dict) else None
    if not isinstance(cur, dict):
        cur = {}
    backup = cur.get("backup") if isinstance(cur.get("backup"), dict) else {}
    return {
        "enabled": bool(cur.get("enabled", True)),
        "interval_hours": float(cur.get("interval_hours", DEFAULT_INTERVAL_HOURS)),
        "min_idle_hours": float(cur.get("min_idle_hours", DEFAULT_MIN_IDLE_HOURS)),
        "stale_after_days": float(cur.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS)),
        "archive_after_days": float(cur.get("archive_after_days", DEFAULT_ARCHIVE_AFTER_DAYS)),
        "consolidate": bool(cur.get("consolidate", DEFAULT_CONSOLIDATE)),
        "backup_enabled": bool(backup.get("enabled", True)),
        "backup_keep": int(backup.get("keep", 5)),
    }


class SkillCurator:
    """绑定 SkillLoader，对 workspace + global 技能根分别维护。"""

    def __init__(
        self,
        skill_loader,
        *,
        config_getter: Optional[Callable[[], Dict[str, Any]]] = None,
        refresh_callback: Optional[Callable[[], None]] = None,
    ):
        self.skill_loader = skill_loader
        self._config_getter = config_getter
        self._refresh_callback = refresh_callback

    def _cfg(self) -> Dict[str, Any]:
        raw = self._config_getter() if self._config_getter else None
        return load_curator_config(raw)

    def _roots(self) -> List[tuple[str, Path, SkillUsageStore]]:
        roots = [("workspace", self.skill_loader.skills_dir, self.skill_loader._usage_store)]
        if self.skill_loader.global_dir and self.skill_loader._global_usage_store:
            roots.append(
                ("global", self.skill_loader.global_dir, self.skill_loader._global_usage_store)
            )
        return roots

    def state_path(self, skills_dir: Path) -> Path:
        return Path(skills_dir) / CURATOR_STATE_FILENAME

    def load_state(self, skills_dir: Path) -> Dict[str, Any]:
        path = self.state_path(skills_dir)
        if not path.exists():
            return _default_state()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _default_state()
                base.update({k: v for k, v in data.items() if k in base})
                return base
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("Failed to read curator state: %s", e)
        return _default_state()

    def save_state(self, skills_dir: Path, data: Dict[str, Any]) -> None:
        path = self.state_path(skills_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".curator_state_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.debug("Failed to write curator state: %s", e, exc_info=True)

    def status(self) -> Dict[str, Any]:
        cfg = self._cfg()
        scopes = {}
        for scope, root, store in self._roots():
            st = self.load_state(root)
            scopes[scope] = {
                "skills_dir": str(root),
                "state": st,
                "curated_count": len(store.curated_report()),
                "archived_count": len(store.list_archived_names()),
            }
        return {
            "config": cfg,
            "idle_for_seconds": idle_for_seconds_json(),
            "scopes": scopes,
        }

    def set_paused(self, paused: bool) -> None:
        for _scope, root, _store in self._roots():
            st = self.load_state(root)
            st["paused"] = bool(paused)
            self.save_state(root, st)

    def should_run_now(self, *, idle_seconds: Optional[float] = None) -> bool:
        cfg = self._cfg()
        if not cfg["enabled"]:
            return False
        idle = idle_for_seconds() if idle_seconds is None else idle_seconds
        if idle < cfg["min_idle_hours"] * 3600.0:
            return False

        # 任一 scope 满足 interval 且未 pause 即可跑（首次 seed 在 run 内处理）
        for _scope, root, _store in self._roots():
            st = self.load_state(root)
            if st.get("paused"):
                continue
            last = _parse_iso(st.get("last_run_at"))
            if last is None:
                return True  # 首次：run 内 seed defer
            elapsed_h = (_now() - last).total_seconds() / 3600.0
            if elapsed_h >= cfg["interval_hours"]:
                return True
        return False

    def apply_automatic_transitions(
        self,
        *,
        now: Optional[datetime] = None,
        scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        """对 curator-managed 技能做 active/stale/archive 转换。"""
        cfg = self._cfg()
        if now is None:
            now = _now()
        stale_cutoff = now - timedelta(days=cfg["stale_after_days"])
        archive_cutoff = now - timedelta(days=cfg["archive_after_days"])

        totals = {
            "marked_stale": 0,
            "archived": 0,
            "reactivated": 0,
            "checked": 0,
            "seeded": 0,
            "skipped_pinned": 0,
        }

        for sc, root, store in self._roots():
            if scope and sc != scope:
                continue
            for row in store.curated_report():
                totals["checked"] += 1
                name = row["name"]
                if row.get("pinned"):
                    totals["skipped_pinned"] += 1
                    continue

                rec = store.get_record(name)
                # 无 created_at / null：回填时钟并推迟本轮转换
                if not rec.get("created_at"):
                    store.ensure_created_at(name)
                    totals["seeded"] += 1
                    continue

                last_activity = _parse_iso(row.get("last_activity_at"))
                anchor = last_activity or _parse_iso(row.get("created_at")) or now
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=timezone.utc)

                current = row.get("state") or STATE_ACTIVE
                never_used = int(row.get("use_count", 0) or 0) == 0

                if never_used and anchor > stale_cutoff:
                    if current == STATE_STALE:
                        store.set_state(name, STATE_ACTIVE)
                        totals["reactivated"] += 1
                    continue

                if anchor <= archive_cutoff and current != STATE_ARCHIVED:
                    skill_on_disk = (root / name / "SKILL.md").exists()
                    if not skill_on_disk:
                        # usage 孤儿或已不在本 scope：只收口状态，不碰其他 scope 目录
                        store.set_state(name, STATE_ARCHIVED)
                        totals["archived"] += 1
                        continue
                    try:
                        # 强制按当前 scope 磁盘路径归档，避免同名覆盖误伤
                        self.skill_loader.archive_skill(name, scope=sc)
                        totals["archived"] += 1
                    except Exception as e:
                        logger.warning(
                            "curator archive %s scope=%s failed: %s", name, sc, e
                        )
                elif anchor <= stale_cutoff and current == STATE_ACTIVE:
                    store.set_state(name, STATE_STALE)
                    totals["marked_stale"] += 1
                elif anchor > stale_cutoff and current == STATE_STALE:
                    store.set_state(name, STATE_ACTIVE)
                    totals["reactivated"] += 1

        return totals

    def run(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        consolidate: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """执行一次 curator pass。"""
        cfg = self._cfg()
        started = _now()
        summary_parts: List[str] = []
        results: Dict[str, Any] = {
            "started_at": started.isoformat(),
            "force": force,
            "dry_run": dry_run,
            "scopes": {},
            "transitions": {},
            "backups": {},
            "consolidate": None,
        }

        for sc, root, store in self._roots():
            st = self.load_state(root)
            if st.get("paused") and not force:
                results["scopes"][sc] = {"skipped": "paused"}
                continue

            last = _parse_iso(st.get("last_run_at"))
            if last is None and not force:
                # 首次：只 seed 时钟，不立刻跑
                st["last_run_at"] = started.isoformat()
                st["last_run_summary"] = "first-sight seed; deferred full run"
                self.save_state(root, st)
                results["scopes"][sc] = {"deferred": True}
                summary_parts.append(f"{sc}: deferred (first sight)")
                continue

            if not force and last is not None:
                elapsed_h = (started - last).total_seconds() / 3600.0
                if elapsed_h < cfg["interval_hours"]:
                    results["scopes"][sc] = {"skipped": "interval"}
                    continue

            if dry_run:
                report = store.curated_report()
                results["scopes"][sc] = {
                    "dry_run": True,
                    "curated": report,
                }
                continue

            if cfg["backup_enabled"]:
                try:
                    bak = curator_backup.snapshot_skills(
                        root, keep=cfg["backup_keep"], reason="curator-run"
                    )
                    results["backups"][sc] = bak
                except Exception as e:
                    logger.warning("curator backup failed for %s: %s", sc, e)
                    results["backups"][sc] = {"ok": False, "error": str(e)}

            counts = self.apply_automatic_transitions(now=started, scope=sc)
            results["transitions"][sc] = counts
            summary_parts.append(
                f"{sc}: stale={counts['marked_stale']} archived={counts['archived']} "
                f"reactivated={counts['reactivated']}"
            )

            do_consolidate = cfg["consolidate"] if consolidate is None else consolidate
            if do_consolidate:
                cons = self._write_consolidate_report(sc, root, store)
                results["consolidate"] = cons
                summary_parts.append(f"{sc}: consolidate-report={cons.get('report_path')}")

            ended = _now()
            st = self.load_state(root)
            st["last_run_at"] = ended.isoformat()
            st["last_run_duration_seconds"] = (ended - started).total_seconds()
            st["last_run_summary"] = "; ".join(summary_parts) or "ok"
            st["run_count"] = int(st.get("run_count") or 0) + 1
            self.save_state(root, st)
            results["scopes"][sc] = {"ok": True, "counts": counts}

        if self._refresh_callback and not dry_run:
            try:
                self._refresh_callback()
            except Exception:
                pass

        results["finished_at"] = _now().isoformat()
        results["summary"] = "; ".join(summary_parts) or "no-op"
        return results

    def _write_consolidate_report(
        self,
        scope: str,
        skills_dir: Path,
        store: SkillUsageStore,
    ) -> Dict[str, Any]:
        """生成 consolidate 候选报告（默认不自动执行合并）。

        完整 LLM fork 合并可在后续接入；此处保证 consolidate=true 时有可审计产物。
        """
        from .provenance import set_write_origin

        candidates = store.curated_report()
        home = Path(os.path.expanduser("~/.helloclaw"))
        ts = _now().strftime("%Y%m%d-%H%M%S")
        report_dir = home / "logs" / "curator" / f"{scope}-{ts}"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "REPORT.md"
        run_json = report_dir / "run.json"

        lines = [
            f"# Curator Consolidate Report ({scope})",
            "",
            f"- generated_at: {_now().isoformat()}",
            f"- skills_dir: `{skills_dir}`",
            f"- candidates: {len(candidates)}",
            "",
            "## Candidates",
            "",
        ]
        for row in candidates:
            lines.append(
                f"- **{row['name']}** state={row.get('state')} "
                f"use={row.get('use_count')} patch={row.get('patch_count')} "
                f"pinned={row.get('pinned')} last={row.get('last_activity_at')}"
            )
        lines.extend(
            [
                "",
                "## Notes",
                "",
                "- 本报告为 consolidate 审计产物；自动 umbrella 合并需馆员 LLM fork。",
                "- 删除/合并必须经 `skill_manage(delete, absorbed_into=...)`，fail-closed。",
                "- 使用 `skills.provenance.set_write_origin('curator')` 激活后台写守卫。",
                "",
            ]
        )
        report_path.write_text("\n".join(lines), encoding="utf-8")
        payload = {
            "scope": scope,
            "candidates": candidates,
            "report_path": str(report_path),
        }
        run_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        # 演示 provenance 可设置（不改变全局默认）
        try:
            set_write_origin("curator")
            set_write_origin("foreground")
        except Exception:
            pass

        return {
            "enabled": True,
            "status": "report_only",
            "report_path": str(report_path),
            "candidate_count": len(candidates),
        }

    def maybe_run(self) -> Optional[Dict[str, Any]]:
        try:
            if not self.should_run_now():
                return None
            return self.run(force=False)
        except Exception as e:
            logger.debug("maybe_run_curator failed: %s", e, exc_info=True)
            return None
