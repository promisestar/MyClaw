"""JSONL / 报告读写辅助。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"JSONL row must be object at {path}:{line_no}")
            rows.append(obj)
    return rows


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_report_pair(reports_dir: Path, channel: str, report: Dict[str, Any]) -> tuple[Path, Path]:
    """写入 JSON + Markdown 摘要，返回路径。"""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp_slug()
    json_path = reports_dir / f"{channel}_{stamp}.json"
    md_path = reports_dir / f"{channel}_{stamp}.md"
    write_json(json_path, report)

    ks = report.get("ks") or []
    lines = [
        f"# {channel} retrieval eval",
        "",
        f"- collection: `{report.get('collection')}`",
        f"- queries: {report.get('query_count')}",
        f"- top_k: {report.get('top_k')}",
        f"- score_threshold: {report.get('score_threshold')}",
        f"- mean_mrr: {report.get('mean_mrr'):.4f}",
        "",
        "| K | Hit@K | Recall@K | Precision@K |",
        "|---|-------|----------|-------------|",
    ]
    hit = report.get("mean_hit_at") or {}
    recall = report.get("mean_recall_at") or {}
    precision = report.get("mean_precision_at") or {}
    for k in ks:
        sk = str(k)
        lines.append(
            f"| {k} | {hit.get(sk, 0):.4f} | {recall.get(sk, 0):.4f} | {precision.get(sk, 0):.4f} |"
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def normalize_doc_key(path_or_name: str) -> str:
    """文档级命中键：统一为小写 basename。"""
    name = Path(str(path_or_name).replace("\\", "/")).name
    return name.lower()
