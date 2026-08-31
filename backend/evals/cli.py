"""MyClaw 统一评测 CLI。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

# 保证从 backend 根目录可 import src.* 与 evals.*
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# 与 src.main 一致：加载 backend/.env（QDRANT_URL / EMBED_* 等）
try:
    from dotenv import load_dotenv

    load_dotenv(_BACKEND_ROOT / ".env")
except ImportError:
    pass


def _parse_ks(raw: str) -> List[int]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("ks must be non-empty, e.g. 1,3,5,10")
    return [int(p) for p in parts]


def _parse_ids(raw: str) -> Optional[List[str]]:
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return ids or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals",
        description=(
            "MyClaw 统一评测：Agent L2 场景（--channel agent）或 "
            "Memory/RAG 离线检索（--channel memory|rag|rag_expanded|retrieval）"
        ),
    )
    p.add_argument(
        "--channel",
        choices=("agent", "memory", "rag", "rag_expanded", "retrieval", "all"),
        default="retrieval",
        help=(
            "评测通道：agent=Agent L2 场景；memory/rag/rag_expanded=单通道检索；"
            "retrieval/all=全部检索通道（默认 retrieval）"
        ),
    )

    # --- Agent L2 ---
    agent = p.add_argument_group("Agent L2（--channel agent）")
    agent.add_argument(
        "--base-url",
        default=None,
        help="后端地址（默认 MYCLAW_EVAL_BASE_URL 或 http://127.0.0.1:8000）",
    )
    agent.add_argument("--suite", default="core", help="场景标签过滤，默认 core")
    agent.add_argument(
        "--ids",
        default="",
        help="逗号分隔场景 id，如 ask_readonly_gate,plan_generate",
    )
    agent.add_argument(
        "--workspace",
        default=None,
        help="已授权工作区路径（建议指向 evals/agent/fixtures/mini_repo 副本）",
    )

    # --- Memory / RAG 检索 ---
    retrieval = p.add_argument_group("Memory / RAG 检索")
    retrieval.add_argument(
        "--ks",
        type=_parse_ks,
        default=[1, 3, 5, 10],
        help="Comma-separated K values for Hit/Recall/Precision (default: 1,3,5,10)",
    )
    retrieval.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Retrieval limit (default: max(ks))",
    )
    retrieval.add_argument(
        "--score-threshold",
        type=float,
        default=None,
        help="Similarity threshold. Memory default 0.3; RAG default None",
    )
    retrieval.add_argument(
        "--enable-mqe",
        action="store_true",
        default=True,
        help="RAG expanded: enable MQE (default on for rag_expanded)",
    )
    retrieval.add_argument(
        "--disable-mqe",
        dest="enable_mqe",
        action="store_false",
        help="RAG expanded: disable MQE",
    )
    retrieval.add_argument(
        "--enable-hyde",
        action="store_true",
        default=True,
        help="RAG expanded: enable HyDE (default on for rag_expanded)",
    )
    retrieval.add_argument(
        "--disable-hyde",
        dest="enable_hyde",
        action="store_false",
        help="RAG expanded: disable HyDE",
    )
    retrieval.add_argument(
        "--mqe-expansions",
        type=int,
        default=2,
        help="RAG expanded: number of MQE extra queries",
    )
    retrieval.add_argument(
        "--reseed",
        action="store_true",
        help="Clear eval collection/namespace and re-index corpus",
    )

    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return p


def _resolve_retrieval_channels(channel: str) -> List[str]:
    if channel in ("retrieval", "all"):
        return ["memory", "rag", "rag_expanded"]
    return [channel]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.channel == "agent":
        import os

        from .run_agent import run_agent_eval

        ws = args.workspace or os.getenv("MYCLAW_EVAL_WORKSPACE") or None
        try:
            results = run_agent_eval(
                base_url=args.base_url,
                suite=args.suite,
                ids=_parse_ids(args.ids),
                workspace_path=ws,
            )
        except Exception as e:
            logging.exception("agent eval failed: %s", e)
            return 1
        passed = sum(1 for r in results if r.hard_pass)
        return 0 if passed == len(results) else 1

    channels = _resolve_retrieval_channels(args.channel)
    exit_code = 0

    for ch in channels:
        try:
            if ch == "memory":
                from .run_memory import run_memory_eval

                thr = 0.3 if args.score_threshold is None else args.score_threshold
                report = run_memory_eval(
                    ks=args.ks,
                    top_k=args.top_k,
                    score_threshold=thr,
                    reseed=args.reseed,
                )
            elif ch == "rag":
                from .run_rag import run_rag_eval

                report = run_rag_eval(
                    ks=args.ks,
                    top_k=args.top_k,
                    score_threshold=args.score_threshold,
                    reseed=args.reseed,
                )
            else:
                from .run_rag_expanded import run_rag_expanded_eval

                report = run_rag_expanded_eval(
                    ks=args.ks,
                    top_k=args.top_k,
                    score_threshold=args.score_threshold,
                    enable_mqe=args.enable_mqe,
                    enable_hyde=args.enable_hyde,
                    mqe_expansions=args.mqe_expansions,
                    reseed=args.reseed,
                )
            print(
                f"[{ch}] queries={report.query_count} "
                f"MRR={report.mean_mrr:.4f} "
                f"Hit@3={report.mean_hit_at.get('3', 0):.4f} "
                f"Recall@5={report.mean_recall_at.get('5', 0):.4f}"
            )
        except Exception as e:
            logging.exception("%s eval failed: %s", ch, e)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
