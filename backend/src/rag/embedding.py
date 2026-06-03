"""统一嵌入模块（实现 + 提供器）

说明（中文）：
- 提供统一的文本嵌入接口与多实现：本地Transformer、TF-IDF兜底。
- 暴露 get_text_embedder()/get_dimension()/refresh_embedder() 供各记忆类型统一使用。
- 通过环境变量优先级：local > tfidf。

环境变量：
- EMBED_MODEL_TYPE: "local" | "tfidf"（默认 local）
- EMBED_MODEL_NAME: 模型名称（local默认 sentence-transformers/all-MiniLM-L6-v2）
- EMBED_MODEL_PATH: 本地模型目录（优先于 EMBED_MODEL_NAME，不访问 Hugging Face）
- EMBED_LOCAL_FILES_ONLY: "1"/"true" 强制仅本地；"0"/"false" 允许联网校验（默认：有缓存则仅本地）
- HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE: 设为 1 时等同强制仅本地
- EMBED_API_KEY: Embedding API Key（统一命名）
- EMBED_BASE_URL: Embedding Base URL（统一命名，可选）
"""

from typing import List, Union, Optional, Tuple
import threading
import os
from pathlib import Path
import numpy as np


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _hf_cache_snapshot_dir(model_name: str) -> Optional[str]:
    """若 Hugging Face 缓存中已有模型，返回 snapshots 目录路径。"""
    try:
        from huggingface_hub import try_to_load_from_cache

        marker = try_to_load_from_cache(model_name, "config.json")
        if marker:
            return str(Path(marker).parent)
    except Exception:
        pass
    return None


def _resolve_local_model(model_name: str) -> Tuple[str, bool]:
    """解析加载路径及是否禁止联网（local_files_only）。

    Returns:
        (model_path_or_id, local_files_only)
    """
    explicit_path = os.getenv("EMBED_MODEL_PATH", "").strip()
    if explicit_path and Path(explicit_path).is_dir():
        return explicit_path, True

    force_offline = _env_flag("EMBED_LOCAL_FILES_ONLY") or _env_flag("HF_HUB_OFFLINE") or _env_flag(
        "TRANSFORMERS_OFFLINE"
    )
    allow_online = os.getenv("EMBED_LOCAL_FILES_ONLY", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )

    cached = _hf_cache_snapshot_dir(model_name)
    if cached:
        # 有本地缓存时默认走快照目录，避免对 huggingface.co 发 HEAD 校验
        local_only = force_offline or not allow_online
        return cached, local_only

    return model_name, force_offline


# ==============
# 抽象与实现
# ==============

class EmbeddingModel:
    """嵌入模型基类（最小接口）"""

    def encode(self, texts: Union[str, List[str]]):
        raise NotImplementedError

    @property
    def dimension(self) -> int:
        raise NotImplementedError


class LocalTransformerEmbedding(EmbeddingModel):
    """本地Transformer嵌入（优先 sentence-transformers，缺失回退 transformers+torch）"""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._load_path, self._local_files_only = _resolve_local_model(model_name)
        self._backend = None  # "st" 或 "hf"
        self._st_model = None
        self._hf_tokenizer = None
        self._hf_model = None
        self._dimension = None
        self._load_backend()

    def _load_backend(self):
        load_kwargs = {}
        if self._local_files_only:
            load_kwargs["local_files_only"] = True

        # 优先 sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer
            self._st_model = SentenceTransformer(self._load_path, **load_kwargs)
            test_vec = self._st_model.encode("test_text")
            self._dimension = len(test_vec)
            self._backend = "st"
            return
        except Exception:
            self._st_model = None

        # 回退 transformers
        try:
            from transformers import AutoTokenizer, AutoModel
            import torch
            self._hf_tokenizer = AutoTokenizer.from_pretrained(
                self._load_path, **load_kwargs
            )
            self._hf_model = AutoModel.from_pretrained(self._load_path, **load_kwargs)
            with torch.no_grad():
                inputs = self._hf_tokenizer("test_text", return_tensors="pt", padding=True, truncation=True)
                outputs = self._hf_model(**inputs)
                test_embedding = outputs.last_hidden_state.mean(dim=1)
                self._dimension = int(test_embedding.shape[1])
            self._backend = "hf"
            return
        except Exception:
            self._hf_tokenizer = None
            self._hf_model = None

        raise ImportError("未找到可用的本地嵌入后端，请安装 sentence-transformers 或 transformers+torch")

    def encode(self, texts: Union[str, List[str]]):
        if isinstance(texts, str):
            inputs = [texts]
            single = True
        else:
            inputs = list(texts)
            single = False

        if self._backend == "st":
            vecs = self._st_model.encode(inputs)
            if hasattr(vecs, "tolist"):
                vecs = [v for v in vecs]
        else:
            import torch
            tokenized = self._hf_tokenizer(inputs, return_tensors="pt", padding=True, truncation=True, max_length=512)
            with torch.no_grad():
                outputs = self._hf_model(**tokenized)
                embeddings = outputs.last_hidden_state.mean(dim=1).cpu().numpy()
            vecs = [v for v in embeddings]

        if single:
            return vecs[0]
        return vecs

    @property
    def dimension(self) -> int:
        return int(self._dimension or 0)


class TFIDFEmbedding(EmbeddingModel):
    """TF-IDF 简易兜底（在无深度模型时保证可用）"""

    def __init__(self, max_features: int = 1000):
        self.max_features = max_features
        self._vectorizer = None
        self._is_fitted = False
        self._dimension = max_features
        self._init_vectorizer()

    def _init_vectorizer(self):
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self._vectorizer = TfidfVectorizer(max_features=self.max_features, stop_words='english')
        except ImportError:
            raise ImportError("请安装 scikit-learn: pip install scikit-learn")

    def fit(self, texts: List[str]):
        self._vectorizer.fit(texts)
        self._is_fitted = True
        self._dimension = len(self._vectorizer.get_feature_names_out())

    def encode(self, texts: Union[str, List[str]]):
        if not self._is_fitted:
            raise ValueError("TF-IDF模型未训练，请先调用fit()方法")
        if isinstance(texts, str):
            texts = [texts]
            single = True
        else:
            single = False
        tfidf_matrix = self._vectorizer.transform(texts)
        embeddings = tfidf_matrix.toarray()
        if single:
            return embeddings[0]
        return [e for e in embeddings]

    @property
    def dimension(self) -> int:
        return self._dimension

# ==============
# 工厂与回退
# ==============

def create_embedding_model(model_type: str = "local", **kwargs) -> EmbeddingModel:
    """创建嵌入模型实例

    model_type:  "local" | "tfidf"
    kwargs: model_name, api_key
    """
    if model_type in ("local", "sentence_transformer", "huggingface"):
        return LocalTransformerEmbedding(**kwargs)
    elif model_type == "tfidf":
        return TFIDFEmbedding(**kwargs)
    else:
        raise ValueError(f"不支持的模型类型: {model_type}")


def create_embedding_model_with_fallback(preferred_type: str = "dashscope", **kwargs) -> EmbeddingModel:
    """带回退的创建：local -> tfidf"""
    if preferred_type in ("sentence_transformer", "huggingface"):
        preferred_type = "local"
    fallback = ["local", "tfidf"]
    # 将首选放最前
    if preferred_type in fallback:
        fallback.remove(preferred_type)
        fallback.insert(0, preferred_type)
    for t in fallback:
        try:
            return create_embedding_model(t, **kwargs)
        except Exception:
            continue
    raise RuntimeError("所有嵌入模型都不可用，请安装依赖或检查配置")


# ==================
# Provider（单例）
# ==================

_lock = threading.RLock()
_embedder: Optional[EmbeddingModel] = None


def _build_embedder() -> EmbeddingModel:
    preferred = os.getenv("EMBED_MODEL_TYPE", "local").strip()
    # 根据提供商选择默认模型
    default_model = "sentence-transformers/all-MiniLM-L6-v2"
    model_name = os.getenv("EMBED_MODEL_NAME", default_model).strip()
    kwargs = {}
    if model_name:
        kwargs["model_name"] = model_name
    # 仅使用统一命名
    api_key = os.getenv("EMBED_API_KEY")
    if api_key:
        kwargs["api_key"] = api_key
    base_url = os.getenv("EMBED_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return create_embedding_model_with_fallback(preferred_type=preferred, **kwargs)


def get_text_embedder() -> EmbeddingModel:
    """获取全局共享的文本嵌入实例（线程安全单例）"""
    global _embedder
    if _embedder is not None:
        return _embedder
    with _lock:
        if _embedder is None:
            _embedder = _build_embedder()
        return _embedder


def get_dimension(default: int = 384) -> int:
    """获取统一向量维度（失败回退默认值）"""
    try:
        return int(getattr(get_text_embedder(), "dimension", default))
    except Exception:
        return int(default)


def refresh_embedder() -> EmbeddingModel:
    """强制重建嵌入实例（可用于动态切换环境变量）"""
    global _embedder
    with _lock:
        _embedder = _build_embedder()
        return _embedder


if __name__ == "__main__":
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    embedder = LocalTransformerEmbedding(model_name)
    if embedder != None:
        print("embedder is not None")