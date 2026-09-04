"""知识检索：分块、混合检索、来源分级、有效期降权。

检索分数的构成（每一项都有产品理由，不是调参凑出来的）：

    raw   = α · cosine + (1-α) · bm25_norm      混合检索，词法与语义互补
    score = raw × level_cap × stale_penalty     来源分级与保鲜作为**乘性上限**

用乘法而不是加法，是因为分级和过期是**否决性**的：一条 L3 口述知识
无论文本多匹配，都不应该越过 0.62 的作答阈值——SOURCE_LEVEL_CAP["L3"]=0.45
从结构上保证了这一点，而不是靠 prompt 里写一句"请谨慎"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi

from . import config, db, embeddings

# ------------------------------------------------------------------ 分词
try:
    import warnings

    with warnings.catch_warnings():  # jieba 在 3.13+ 上有正则转义的 SyntaxWarning
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba

    jieba.setLogLevel(60)

    def tokenize(text: str) -> list[str]:
        toks = [t.strip() for t in jieba.lcut(text or "") if t.strip()]
        return [t for t in toks if not re.fullmatch(r"[\s，。？！、；：""''（）()]+", t)]

except ImportError:  # pragma: no cover - jieba 是硬依赖，这里只是兜底

    def tokenize(text: str) -> list[str]:
        cjk = re.findall(r"[一-鿿]", text or "")
        words = re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
        return cjk + words


# ------------------------------------------------------------------ 结果
@dataclass
class Hit:
    knowledge_id: int
    question: str
    answer: str
    domain: str
    source_level: str
    source_name: str
    confirmed_by: str | None
    is_stale: bool
    citation: str

    raw_score: float          # 混合检索原始分
    score: float              # 经分级与保鲜折减后的最终分
    bm25: float
    dense: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.knowledge_id,
            "question": self.question,
            "answer": self.answer,
            "level": self.source_level,
            "source_name": self.source_name,
            "citation": self.citation,
            "score": round(self.score, 4),
            "raw": round(self.raw_score, 4),
            "bm25": round(self.bm25, 4),
            "dense": round(self.dense, 4),
            "stale": self.is_stale,
        }


@dataclass
class Retrieval:
    hits: list[Hit]
    best_score: float
    confident: bool
    domain: str | None
    degraded: bool = False
    """本次是否降级为纯 BM25（embedding 不可用）。
    必须传上去——运营看 trace 时要能分清"检索质量差"和"当时向量服务挂了"。"""

    @property
    def best(self) -> Hit | None:
        return self.hits[0] if self.hits else None

    def hints(self, k: int = 2) -> list[str]:
        """升级时给用户的"次优线索"——一次合格的拒答必须带这个。"""
        out = []
        for h in self.hits[:k]:
            tag = h.source_level
            if h.is_stale:
                tag += " · 已过期"
            out.append(f"{h.question}（{h.source_name}，{tag}）")
        return out


# ------------------------------------------------------------------ 索引
class KnowledgeIndex:
    """内存索引。52 条知识，进程启动时全量载入即可——
    这个规模用向量数据库是过度工程，理由写在 embeddings.py 顶部。"""

    def __init__(self) -> None:
        self._rows: list[db.Knowledge] = []
        self._tokens: list[list[str]] = []
        self._bm25: BM25Okapi | None = None
        self._matrix: np.ndarray = np.zeros((0, 1), dtype=np.float32)
        self._loaded = False

    # -- 载入 ------------------------------------------------------
    def load(self, session=None) -> "KnowledgeIndex":
        own = session is None
        session = session or db.get_session()
        try:
            rows = list(session.query(db.Knowledge).all())
        finally:
            if own:
                session.close()
        return self.load_rows(rows)

    def load_rows(self, rows: list[db.Knowledge]) -> "KnowledgeIndex":
        self._rows = rows
        self._tokens = [tokenize(f"{r.question} {r.answer}") for r in rows]
        self._bm25 = BM25Okapi(self._tokens) if self._tokens else None
        vecs = []
        for r in rows:
            if r.embedding:
                vecs.append(np.asarray(r.embedding, dtype=np.float32))
            else:
                vecs.append(embeddings.encode([f"{r.question} {r.answer}"])[0])
        self._matrix = np.vstack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)
        self._loaded = True
        return self

    @property
    def size(self) -> int:
        return len(self._rows)

    # -- 检索 ------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        top_k: int = 4,
        threshold: float | None = None,
    ) -> Retrieval:
        """域过滤 → 混合打分 → 分级与保鲜折减 → 排序。

        domain 不是可选的性能优化，是**权限边界**：IT 域的检索拿不到
        HR 域的知识，这是子 Agent 隔离在数据层的落点。
        """
        if not self._loaded:
            self.load()
        thr = config.CONFIDENCE_THRESHOLD if threshold is None else threshold

        if not self._rows or self._bm25 is None:
            return Retrieval([], 0.0, False, domain)

        # 词法
        q_tokens = tokenize(query)
        bm_raw = np.asarray(self._bm25.get_scores(q_tokens), dtype=np.float32)
        bm_max = float(bm_raw.max()) if bm_raw.size else 0.0
        bm_norm = bm_raw / bm_max if bm_max > 1e-9 else bm_raw

        # 语义。**embedding 是外部依赖，它挂了不能让整个请求跟着挂。**
        # 实测百炼抖了一次 APIConnectionError，retrieve 节点直接抛异常，
        # 整个 ask() 崩掉——用户看到的是 500，而不是一个降级但可用的答案。
        #
        # 混合检索的好处正在于此：两条腿走路，断一条还能瘸着走。
        # 降级到纯 BM25 会损失语义召回，但**有答案远好过没答案**。
        degraded = False
        try:
            q_vec = embeddings.encode([query])[0]
            if self._matrix.shape[1] == q_vec.shape[0]:
                dense = np.clip(embeddings.cosine(q_vec, self._matrix), 0.0, 1.0)
            else:  # 维度不一致（换过 embedding 后端且未重建索引）
                dense = np.zeros(len(self._rows), dtype=np.float32)
                degraded = True
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("ramp.knowledge").warning(
                "embedding 不可用（%s），本次检索降级为纯 BM25", type(exc).__name__
            )
            dense = np.zeros(len(self._rows), dtype=np.float32)
            degraded = True

        # 降级时把权重全给 BM25——否则 alpha 那部分是在拿一堆 0 稀释分数，
        # 会让所有条目一起掉到阈值以下，表现成"什么都检索不到"。
        a = 0.0 if degraded else config.HYBRID_ALPHA
        raw = a * dense + (1 - a) * bm_norm

        today = date.today()
        hits: list[Hit] = []
        for i, row in enumerate(self._rows):
            if domain and row.domain != domain:
                continue
            cap = config.SOURCE_LEVEL_CAP.get(row.source_level, 0.5)
            stale = bool(row.expires_on and row.expires_on < today)
            penalty = config.STALE_PENALTY if stale else 1.0
            if degraded:
                penalty *= config.DEGRADED_PENALTY
            final = float(raw[i]) * cap * penalty
            hits.append(
                Hit(
                    knowledge_id=row.id,
                    question=row.question,
                    answer=row.answer,
                    domain=row.domain,
                    source_level=row.source_level,
                    source_name=row.source_name,
                    confirmed_by=row.confirmed_by,
                    is_stale=stale,
                    citation=row.cite(),
                    raw_score=float(raw[i]),
                    score=final,
                    bm25=float(bm_norm[i]),
                    dense=float(dense[i]),
                )
            )

        hits.sort(key=lambda h: h.score, reverse=True)
        hits = hits[:top_k]
        best = hits[0].score if hits else 0.0

        # ---- 排序用混合分，"能不能作答"用余弦 ----
        #
        # 这两件事要分开，是被一组对照实验逼出来的结论。
        #
        # 原实现拿混合分同时干这两件事，结果 10 条「知识库里根本没有」的
        # 提问里 **7 条被判定可作答**：
        #
        #     公司的宠物友好日是哪天？ 0.6327 → 命中「试用期多久？」
        #     团建预算是多少？        0.7242 → 命中「报销标准是多少？」
        #     董事长叫什么名字？      0.6380 → 命中「转正要交什么材料？」
        #
        # 根因：混合分里的 BM25 是 `bm_raw / bm_max` **除以本批最大值**归一化的。
        # 全部候选都不相关时，最像的那个照样得 1.0，直接贡献 0.5 分把
        # 无关条目推过阈值。归一化 BM25 只能回答"这批里哪个最像"，
        # 回答不了"它到底像不像"——**把相对排名当成了绝对相关性。**
        #
        # 实测两个判据的可分性（负样本 10 条 / 正样本 15 条）：
        #
        #     混合分   负 [0.199, 0.846]  正 [0.528, 0.961]  间隔 -0.318  重叠
        #     余弦     负 [0.265, 0.692]  正 [0.745, 0.922]  间隔 +0.054  可分
        #
        # 混合分区间是**重叠**的——它在数学上就不可能作为判据，
        # 调任何阈值都会顾此失彼。余弦有界 [0,1] 且有绝对含义，能分开。
        #
        # 所以：混合分继续排序（BM25 对专有名词的召回有价值），
        # 但闸门只认余弦。三方案对照的结果是
        # A 只看混合分 漏 7/10、B 双条件 误伤 1/15、C 只看余弦 0 漏 0 误伤。
        #
        # ⚠️ 可用区间只有 0.054 宽，不算宽裕。知识库规模变化后要重新标定，
        # 脚本在 `scripts/calibrate_gate.py`。
        if degraded:
            # 没有向量时余弦全是 0，这道闸会把一切拦死。
            # 降级本来就已经乘了 DEGRADED_PENALTY 降自信度，这里退回混合分判据。
            confident = best >= thr
        else:
            confident = (best >= thr
                         and bool(hits)
                         and hits[0].dense >= config.SEMANTIC_FLOOR)

        return Retrieval(hits, best, confident, domain, degraded=degraded)


_index: KnowledgeIndex | None = None


def index() -> KnowledgeIndex:
    global _index
    if _index is None:
        _index = KnowledgeIndex().load()
    return _index


def reload_index() -> KnowledgeIndex:
    """知识写入后调用——飞轮的最后一步是让新知识立刻可被检索到。"""
    global _index
    _index = KnowledgeIndex().load()
    return _index


def search(query: str, **kw: Any) -> Retrieval:
    return index().search(query, **kw)


# ------------------------------------------------------------------ 写入
def add_knowledge(
    session,
    *,
    domain: str,
    question: str,
    answer: str,
    source_level: str = "L2",
    source_name: str = "历史问答沉淀",
    confirmed_by: str | None = None,
    effective_from: date | None = None,
    expires_on: date | None = None,
) -> db.Knowledge:
    """新增一条知识并算好向量。飞轮的 04 步就是调它。"""
    row = db.Knowledge(
        domain=domain,
        question=question.strip(),
        answer=answer.strip(),
        source_level=source_level,
        source_name=source_name,
        confirmed_by=confirmed_by,
        effective_from=effective_from or date.today(),
        expires_on=expires_on,
        embedding=embeddings.encode_one(f"{question} {answer}"),
    )
    session.add(row)
    session.commit()
    return row


def seed_from_file(session, path=None) -> int:
    """把 seed/knowledge.json 灌进库，并预计算向量。"""
    path = path or (config.SEED_DIR / "knowledge.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["items"]

    texts = [f"{i['question']} {i['answer']}" for i in items]
    vecs = embeddings.encode(texts)

    def d(v):
        return date.fromisoformat(v) if v else None

    for item, vec in zip(items, vecs):
        session.add(
            db.Knowledge(
                domain=item["domain"],
                question=item["question"],
                answer=item["answer"],
                source_level=item["source_level"],
                source_name=item["source_name"],
                confirmed_by=item.get("confirmed_by"),
                effective_from=d(item.get("effective_from")),
                expires_on=d(item.get("expires_on")),
                embedding=vec.tolist(),
            )
        )
    session.commit()
    return len(items)
