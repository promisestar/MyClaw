"""Agent L2 场景评测 runner（SSE + 硬断言）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent.harness.judge import (
    JudgeResult,
    TraceJudge,
    build_digest,
    summarize_judge,
)
from .agent.harness.scorers import score_scenario
from .agent.harness.sse_client import cancel_chat, run_chat_stream
from .agent.harness.types import CaseResult, JudgeConfig, Scenario, StreamTrace
from .agent.suites import load_scenarios
from .paths import AGENT_FIXTURES_DIR, AGENT_REPORTS_DIR, EVALS_ROOT

logger = logging.getLogger(__name__)

_BACKEND_ROOT = EVALS_ROOT.parent

# 工作区重置时保留（授权元数据 / 缓存），不从夹具覆盖
_WORKSPACE_PRESERVE = frozenset({".myclaw", ".pytest_cache", ".backups", "__pycache__"})


def reset_workspace_from_fixture(workspace_path: str) -> None:
    """用 mini_repo 夹具覆盖还原评测工作区文件，保留 .myclaw 等元数据。

    每场景开始前调用，避免前序副作用污染后续断言（如 README / sample_app）。
    """
    ws = Path(workspace_path)
    fixture = AGENT_FIXTURES_DIR
    if not ws.is_dir():
        logger.warning("工作区不存在，跳过重置: %s", workspace_path)
        return
    if not fixture.is_dir():
        logger.warning("夹具目录不存在，跳过重置: %s", fixture)
        return

    for src in fixture.iterdir():
        if src.name in _WORKSPACE_PRESERVE:
            continue
        dst = ws / src.name
        try:
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except OSError as exc:
            logger.warning("重置工作区条目失败 %s -> %s: %s", src, dst, exc)


def _resolve_attachments(
    sc: Scenario, workspace_path: Optional[str]
) -> List[Dict[str, Any]]:
    """把场景 attachments 补全后透传。

    size 在实际工作区里现算：YAML 里写死字节数会随夹具改动过期。
    文件不存在时保留 size=0 并告警——后端会以元数据缺失处理，
    但场景仍会发出请求，便于暴露"附件路径写错"这类问题。
    """
    if not sc.attachments:
        return []
    out: List[Dict[str, Any]] = []
    for att in sc.attachments:
        item = dict(att)
        if not item.get("size") and workspace_path:
            p = Path(workspace_path) / str(item.get("stored_path") or "")
            try:
                if p.is_file():
                    item["size"] = p.stat().st_size
                else:
                    logger.warning(
                        "[%s] 附件不存在: %s（工作区 %s）",
                        sc.id,
                        item.get("stored_path"),
                        workspace_path,
                    )
            except OSError as exc:
                logger.warning("[%s] 附件大小读取失败: %s", sc.id, exc)
        out.append(item)
    return out


async def _run_one(
    sc: Scenario,
    *,
    base_url: str,
    session_id: Optional[str],
    workspace_path: Optional[str],
) -> StreamTrace:
    """执行单个场景；timeout_s 到期或 cancel_after_s 触发时调用 /chat/cancel。"""
    attachments = _resolve_attachments(sc, workspace_path)
    stream_task = asyncio.create_task(
        run_chat_stream(
            base_url,
            message=sc.message,
            mode=sc.mode,
            plan_confirmed=sc.plan_confirmed,
            session_id=session_id,
            skill=sc.skill,
            workspace_path=workspace_path,
            attachments=attachments or None,
            timeout_s=sc.timeout_s + 30.0,  # 客户端略宽于服务端 cancel，避免竞态误杀
        )
    )

    # 场景级硬超时：到期 cancel，防止 browser 等挂死拖垮整 suite
    deadlines: List[float] = [float(sc.timeout_s)]
    if sc.cancel_after_s is not None:
        deadlines.append(float(sc.cancel_after_s))
    cancel_after = min(deadlines) if deadlines else None

    if cancel_after is None:
        return await stream_task

    async def _canceller() -> None:
        await asyncio.sleep(cancel_after)
        if stream_task.done():
            return
        try:
            await cancel_chat(base_url)
            logger.info("[%s] 已在 %.1fs 触发 cancel", sc.id, cancel_after)
        except Exception as exc:  # noqa: BLE001 — 取消失败不该让场景崩掉
            logger.warning("[%s] cancel 调用失败: %s", sc.id, exc)

    cancel_task = asyncio.create_task(_canceller())
    try:
        return await stream_task
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)


async def _apply_judge(
    judge: TraceJudge,
    sc: Scenario,
    trace: StreamTrace,
    result: CaseResult,
    *,
    repeat: int,
) -> CaseResult:
    """对单场景跑 judge（repeat>1 时用于自一致性检验）。

    judge 是同步阻塞调用，包在线程里避免卡住事件循环。
    任何失败都只写 ok=False，**不影响 hard_pass**。
    """
    digest = build_digest(sc, trace)
    cfg: JudgeConfig = sc.judge or JudgeConfig(enabled=True)
    runs = []
    for _ in range(max(1, repeat)):
        try:
            runs.append(await asyncio.to_thread(judge.judge, digest, cfg))
        except Exception as exc:  # noqa: BLE001 — judge 失败不该让评测崩掉
            logger.warning("[%s] judge 异常: %s", sc.id, exc)
            runs.append(JudgeResult(ok=False, model=judge.model, error=str(exc)))

    ok_runs = [r for r in runs if r.ok and r.judge_score is not None]
    if not ok_runs:
        payload = runs[0].to_dict()
        payload["repeat"] = repeat
        result.judge = payload
        return result

    # 多次运行：维度分取均值，并记录离散程度（自一致性指标）
    dims = sorted({d for r in ok_runs for d in r.scores})
    merged: Dict[str, Dict[str, Any]] = {}
    for d in dims:
        vals = [float(r.scores[d]["score"]) for r in ok_runs if d in r.scores]
        merged[d] = {
            "score": round(sum(vals) / len(vals), 3),
            "reason": next(
                (r.scores[d].get("reason", "") for r in ok_runs if d in r.scores), ""
            ),
        }
        if len(vals) > 1:
            merged[d]["std"] = round(_pstdev(vals), 4)

    per_run = [float(r.judge_score) for r in ok_runs]
    result.judge = {
        "ok": True,
        "model": ok_runs[0].model,
        "judge_score": round(sum(per_run) / len(per_run), 3),
        "scores": merged,
        "summary": ok_runs[0].summary,
        "red_flags": sorted({f for r in ok_runs for f in r.red_flags}),
        "repeat": repeat,
        "per_run_scores": per_run,
        "score_std": (round(_pstdev(per_run), 4) if len(per_run) > 1 else None),
    }
    return result


def _pstdev(vals: List[float]) -> float:
    """总体标准差；样本量 <2 时为 0。"""
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return (sum((v - mean) ** 2 for v in vals) / n) ** 0.5


async def run_agent_suite(
    scenarios: List[Scenario],
    *,
    base_url: str,
    workspace_path: Optional[str] = None,
    judge: Optional[TraceJudge] = None,
    judge_repeat: int = 1,
) -> List[CaseResult]:
    session_map: Dict[str, str] = {}
    results: List[CaseResult] = []

    for sc in scenarios:
        sid = sc.session_id
        if sc.reuse_session_from:
            sid = session_map.get(sc.reuse_session_from)
            if not sid:
                results.append(
                    CaseResult(
                        id=sc.id,
                        title=sc.title,
                        hard_pass=False,
                        violations=[
                            f"缺少上游会话: reuse_session_from={sc.reuse_session_from}"
                        ],
                    )
                )
                continue

        ws = sc.workspace_path if sc.workspace_path is not None else workspace_path

        if ws:
            try:
                reset_workspace_from_fixture(ws)
            except Exception as exc:  # noqa: BLE001 — 重置失败记警告，仍尝试跑场景
                logger.warning("[%s] 工作区重置失败: %s", sc.id, exc)

        try:
            trace = await _run_one(
                sc, base_url=base_url, session_id=sid, workspace_path=ws
            )
        except Exception as exc:  # noqa: BLE001 — 评测需要吞掉并记失败
            results.append(
                CaseResult(
                    id=sc.id,
                    title=sc.title,
                    hard_pass=False,
                    violations=[f"请求失败: {exc}"],
                )
            )
            continue

        result = score_scenario(sc, trace)
        if trace.session_id:
            session_map[sc.id] = trace.session_id

        # judge 只在显式开启、且场景未声明 enabled:false 时运行
        if judge is not None and (sc.judge is None or sc.judge.enabled):
            await _apply_judge(judge, sc, trace, result, repeat=judge_repeat)

        results.append(result)
        status = "PASS" if result.hard_pass else "FAIL"
        extra = ""
        if result.judge and result.judge.get("ok"):
            extra = f"  judge={result.judge['judge_score']}"
        elif result.judge:
            extra = "  judge=n/a"
        print(f"[{status}] {sc.id} — {sc.title}{extra}")
        for v in result.violations:
            print(f"    ! {v}")

    return results


def _stdev(vals: List[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return (sum((v - mean) ** 2 for v in vals) / n) ** 0.5


def write_agent_report(results: List[CaseResult], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"agent_{ts}.json"
    passed = sum(1 for r in results if r.hard_pass)
    soft_scores = [r.soft_score for r in results]

    summary: Dict[str, Any] = {
        "total": len(results),
        "hard_pass": passed,
        "hard_fail": len(results) - passed,
        "gate_pass_rate": (passed / len(results)) if results else 0.0,
        "avg_soft_score": (sum(soft_scores) / len(results)) if results else 0.0,
        "soft_score_stdev": round(_stdev(soft_scores), 4),
    }

    # L2.1 轨迹指标汇总：通过场景的分数是否真的拉开了
    passing = [r.soft_score for r in results if r.hard_pass]
    if passing:
        summary["soft_score_stdev_passing"] = round(_stdev(passing), 4)
        summary["passing_distinct_scores"] = len(set(round(s, 3) for s in passing))
    penalties = [
        float(r.trace_metrics["weighted_penalty"])
        for r in results
        if r.trace_metrics is not None
    ]
    if penalties:
        summary["avg_trace_penalty"] = round(sum(penalties) / len(penalties), 4)

    # L2.2 judge 汇总
    judge_payloads = [r.judge for r in results if r.judge is not None]
    if judge_payloads:
        summary["judge"] = summarize_judge(judge_payloads)

    payload = {
        "generated_at": ts,
        "channel": "agent",
        "summary": summary,
        "results": [r.to_dict() for r in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_agent_eval(
    *,
    base_url: Optional[str] = None,
    suite: str = "core",
    ids: Optional[List[str]] = None,
    workspace_path: Optional[str] = None,
    report_dir: Optional[Path] = None,
    enable_judge: bool = False,
    judge_repeat: int = 1,
) -> List[CaseResult]:
    """运行 Agent L2 场景评测并写报告。"""
    url = base_url or os.getenv("MYCLAW_EVAL_BASE_URL", "http://127.0.0.1:8000")
    out_dir = report_dir or AGENT_REPORTS_DIR

    scenarios = load_scenarios(suite=suite, ids=ids)
    if not scenarios:
        raise RuntimeError("没有匹配的场景")

    print(f"Base URL: {url}")
    print(f"Scenarios ({len(scenarios)}): " + ", ".join(s.id for s in scenarios))

    judge: Optional[TraceJudge] = None
    if enable_judge:
        judge = TraceJudge()
        if not judge.available:
            # 配置缺失/初始化失败：告警并跳过，绝不因此让评测失败
            print(f"⚠️ judge 未启用：{judge.disabled_reason}", file=sys.stderr)
            judge = None
        else:
            n = sum(1 for s in scenarios if s.judge is None or s.judge.enabled)
            print(f"Judge: {judge.model} — 将评分 {n}/{len(scenarios)} 个场景")

    results = asyncio.run(
        run_agent_suite(
            scenarios,
            base_url=url,
            workspace_path=workspace_path,
            judge=judge,
            judge_repeat=judge_repeat,
        )
    )
    report_path = write_agent_report(results, out_dir)
    passed = sum(1 for r in results if r.hard_pass)
    print(f"\nHard pass: {passed}/{len(results)}")

    passing = [r.soft_score for r in results if r.hard_pass]
    if passing:
        print(
            f"Soft score (passing): avg={sum(passing)/len(passing):.3f} "
            f"stdev={_stdev(passing):.3f} distinct={len(set(round(s,3) for s in passing))}"
        )
    jp = [r.judge for r in results if r.judge is not None]
    if jp:
        js = summarize_judge(jp)
        print(
            f"Judge: scored={js['scored']} skipped={js['skipped']} "
            f"avg={js['avg_judge_score']} red_flags={js['red_flags_count']}"
        )

    print(f"Report: {report_path}")
    return results
