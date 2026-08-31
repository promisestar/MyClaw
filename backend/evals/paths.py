"""evals 包内路径常量。"""

from __future__ import annotations

from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent
DATASETS_ROOT = EVALS_ROOT / "datasets"
MEMORY_DATASET = DATASETS_ROOT / "memory"
RAG_DATASET = DATASETS_ROOT / "rag"
RAG_CORPUS_DIR = RAG_DATASET / "corpus"
REPORTS_DIR = EVALS_ROOT / "reports"
MEMORY_ID_MAP_PATH = REPORTS_DIR / "memory_id_map.json"
