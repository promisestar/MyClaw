"""MyClaw 统一评测包。

- L0/L1：见 backend/tests/eval（pytest，CI）
- L2 Agent 场景：evals.agent + run_agent（需后端 + LLM）
- Memory/RAG 检索：run_memory / run_rag / run_rag_expanded（需 Qdrant + Embedding）
- 统一入口：python -m evals --channel ...
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
