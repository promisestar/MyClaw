"""Agent L2 场景评测 runner（SSE + 硬断言）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .agent.harness.scorers import score_scenario
from .agent.harness.sse_client import run_chat_stream
from .agent.harness.types import CaseResult, Scenario
from .agent.suites import load_scenarios
from .paths import AGENT_REPORTS_DIR, EVALS_ROOT

logger = logging.getLogger(__name__)

_BACKEND_ROOT = EVALS_ROOT.parent


async def run_agent_suite(
    scenarios: List[Scenario],
    *,
    base_url: str,
    workspace_path: Optional[str] = None,
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

        try:
            trace = await run_chat_stream(
                base_url,
                message=sc.message,
                mode=sc.mode,
                plan_confirmed=sc.plan_confirmed,
                session_id=sid,
                skill=sc.skill,
                workspace_path=ws,
                timeout_s=sc.timeout_s,
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
        results.append(result)
        status = "PASS" if result.hard_pass else "FAIL"
        print(f"[{status}] {sc.id} — {sc.title}")
        for v in result.violations:
            print(f"    ! {v}")

    return results


def write_agent_report(results: List[CaseResult], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"agent_{ts}.json"
    passed = sum(1 for r in results if r.hard_pass)
    payload = {
        "generated_at": ts,
        "channel": "agent",
        "summary": {
            "total": len(results),
            "hard_pass": passed,
            "hard_fail": len(results) - passed,
            "gate_pass_rate": (passed / len(results)) if results else 0.0,
            "avg_soft_score": (
                sum(r.soft_score for r in results) / len(results) if results else 0.0
            ),
        },
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
) -> List[CaseResult]:
    """运行 Agent L2 场景评测并写报告。"""
    url = base_url or os.getenv("MYCLAW_EVAL_BASE_URL", "http://127.0.0.1:8000")
    out_dir = report_dir or AGENT_REPORTS_DIR

    scenarios = load_scenarios(suite=suite, ids=ids)
    if not scenarios:
        raise RuntimeError("没有匹配的场景")

    print(f"Base URL: {url}")
    print(f"Scenarios ({len(scenarios)}): " + ", ".join(s.id for s in scenarios))

    results = asyncio.run(
        run_agent_suite(scenarios, base_url=url, workspace_path=workspace_path)
    )
    report_path = write_agent_report(results, out_dir)
    passed = sum(1 for r in results if r.hard_pass)
    print(f"\nHard pass: {passed}/{len(results)}")
    print(f"Report: {report_path}")
    return results
