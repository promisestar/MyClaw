"""L2 评测运行器。

用法（在 backend 目录）：

    uv run python -m eval.runner --suite core
    uv run python -m eval.runner --ids ask_readonly_gate,plan_generate
    uv run python -m eval.runner --base-url http://127.0.0.1:8000 --report-dir ./eval_reports
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# 允许 `python -m eval.runner` 从 backend 根运行
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from eval.harness.scorers import score_scenario  # noqa: E402
from eval.harness.sse_client import run_chat_stream  # noqa: E402
from eval.harness.types import CaseResult, Scenario  # noqa: E402
from eval.suites import load_scenarios  # noqa: E402


async def run_suite(
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


def write_report(results: List[CaseResult], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"eval_report_{ts}.json"
    passed = sum(1 for r in results if r.hard_pass)
    payload = {
        "generated_at": ts,
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MyClaw Agent L2 评测运行器")
    parser.add_argument(
        "--base-url",
        default=os.getenv("MYCLAW_EVAL_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--suite", default="core", help="场景标签过滤，默认 core")
    parser.add_argument("--ids", default="", help="逗号分隔场景 id")
    parser.add_argument(
        "--workspace",
        default=os.getenv("MYCLAW_EVAL_WORKSPACE", ""),
        help="已授权工作区路径（建议指向 eval/fixtures/mini_repo 副本）",
    )
    parser.add_argument(
        "--report-dir",
        default=os.getenv("MYCLAW_EVAL_REPORT_DIR", str(_BACKEND_ROOT / "eval_reports")),
    )
    args = parser.parse_args(argv)

    ids = [x.strip() for x in args.ids.split(",") if x.strip()] or None
    scenarios = load_scenarios(suite=args.suite, ids=ids)
    if not scenarios:
        print("没有匹配的场景", file=sys.stderr)
        return 2

    print(f"Base URL: {args.base_url}")
    print(f"Scenarios ({len(scenarios)}): " + ", ".join(s.id for s in scenarios))
    ws = args.workspace or None

    results = asyncio.run(run_suite(scenarios, base_url=args.base_url, workspace_path=ws))
    report_path = write_report(results, Path(args.report_dir))
    passed = sum(1 for r in results if r.hard_pass)
    print(f"\nHard pass: {passed}/{len(results)}")
    print(f"Report: {report_path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
