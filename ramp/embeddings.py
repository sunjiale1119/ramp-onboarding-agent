"""向量表示：可插拔的 EmbeddingProvider。

⚠️ 选型说明（面试会被问到，理由要能讲）：
DeepSeek 只提供 chat 接口，没有 embedding 端点。对 52 条知识而言，
拉一个 2 GB 的 torch 依赖去跑 dense 模型是过度工程，所以默认后端是
**字符 n-gram 哈希向量 + L2 归一化**——确定性、零依赖、离线可跑，
在这个量级上和 dense 模型的召回差距很小。

如果要在简历上主张"稠密向量检索"，把 RAMP_EMBEDDING_BACKEND 设成
`sentence-transformers`，并 `uv add sentence-transformers`，
模型默认用 BAAI/bge-small-zh-v1.5。接口不变，一行配置切换。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol

import numpy as np

# 必须导入 config —— **不是为了用它的常量，是为了它的导入副作用**。
# config 在导入时跑 load_dotenv()，把 .env 里的 RAMP_EMBEDDING_BACKEND
# 等变量灌进 os.environ。这个模块下面全靠 os.getenv() 选后端。
#
# 没有这一行的时候：谁先导入 config，这里就正常；谁单独导入 embeddings，
# os.getenv 拿到 None，**静默退回 hashing 后端**——不报错、不警告，
# 只是语义检索悄悄变差。部署到服务器时就是这么中招的。
from . import config  # noqa: F401

_CJK = re.compile(r"[一-鿿]")


class EmbeddingProvider(Protocol):
    dimension: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


# ------------------------------------------------------------------ 默认后端
class HashingNGramEmbedding:
    """字符 n-gram 哈希向量。

    中文按 1/2/3-gram 切，英文数字按词切。哈希到固定维度后做 sublinear TF
    和 L2 归一化——本质是一个无需训练的 TF 向量空间，余弦相似度可用。
    """

    def __init__(self, dimension: int = 512) -> None:
        self.dimension = dimension

    @staticmethod
    def _grams(text: str) -> list[str]:
        text = text.lower().strip()
        out: list[str] = []
        # 英文 / 数字按词
        out.extend(re.findall(r"[a-z0-9_.:@-]{2,}", text))
        # 中文按 1–3 gram
        cjk = "".join(_CJK.findall(text))
        for n in (1, 2, 3):
            out.extend(cjk[i : i + n] for i in range(len(cjk) - n + 1))
        return out

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dimension, dtype=np.float32)
        for g in self._grams(text):
            h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
            idx = h % self.dimension
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        # sublinear scaling，压制高频 gram 的支配作用
        vec = np.sign(vec) * np.log1p(np.abs(vec))
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        return np.vstack([self._one(t) for t in texts])


# ------------------------------------------------------------------ 可选后端
class DashScopeEmbedding:
    """阿里云百炼稠密向量（默认 text-embedding-v4，1024 维）。

    走 DashScope 的 OpenAI 兼容端点，返回向量已 L2 归一化，
    所以余弦相似度直接点积即可，不必再归一化一遍。

    批量上限按 10 切分——超出会被服务端拒绝，而不是静默截断。
    向量算完后写进 MySQL 的 JSON 列，索引载入时不再调 API：
    **只有新增知识才产生 embedding 调用**，这也是成本模型里
    检索侧成本几乎为零的原因。
    """

    BATCH = 10

    def __init__(self, model_name: str = "text-embedding-v4", dimension: int = 1024) -> None:
        from openai import OpenAI

        key = os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            raise RuntimeError("DASHSCOPE_API_KEY 未配置，无法使用 dashscope 向量后端")
        self._client = OpenAI(
            api_key=key,
            base_url=os.getenv(
                "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            timeout=60.0,
            max_retries=2,
        )
        self.model_name = model_name
        self.dimension = dimension
        self.tokens_used = 0

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            chunk = [t if t.strip() else "空" for t in texts[i : i + self.BATCH]]
            kw: dict = {"model": self.model_name, "input": chunk}
            if self.model_name.endswith(("v3", "v4")):
                kw["dimensions"] = self.dimension
            resp = self._client.embeddings.create(**kw)
            usage = getattr(resp, "usage", None)
            self.tokens_used += getattr(usage, "total_tokens", 0) or 0
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        arr = np.asarray(out, dtype=np.float32)
        # 服务端已归一化；这里兜一道底，避免换模型后静默出错
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


class SentenceTransformerEmbedding:
    """本地稠密向量后端。需要 `uv add sentence-transformers`（会拉 torch）。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from sentence_transformers import SentenceTransformer  # 延迟导入

        self._model = SentenceTransformer(model_name)
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        return np.asarray(
            self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False),
            dtype=np.float32,
        )


# ------------------------------------------------------------------ 工厂
_provider: EmbeddingProvider | None = None


def provider() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        backend = os.getenv("RAMP_EMBEDDING_BACKEND", "hashing").lower()
        if backend in ("dashscope", "bailian", "aliyun"):
            _provider = DashScopeEmbedding(
                os.getenv("RAMP_EMBEDDING_MODEL", "text-embedding-v4"),
                int(os.getenv("RAMP_EMBEDDING_DIM", "1024")),
            )
        elif backend in ("sentence-transformers", "st", "local"):
            model = os.getenv("RAMP_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
            _provider = SentenceTransformerEmbedding(model)
        else:
            _provider = HashingNGramEmbedding(int(os.getenv("RAMP_EMBEDDING_DIM", "512")))
    return _provider


def encode(texts: list[str]) -> np.ndarray:
    return provider().encode(texts)


def encode_one(text: str) -> list[float]:
    return encode([text])[0].tolist()


def cosine(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """query 与矩阵每一行的余弦相似度。两边都已 L2 归一化，点积即可。"""
    if matrix.size == 0:
        return np.zeros(0, dtype=np.float32)
    return np.clip(matrix @ query, -1.0, 1.0)


def backend_name() -> str:
    p = provider()
    return f"{type(p).__name__}(dim={p.dimension})"
