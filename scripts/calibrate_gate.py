"""标定「能不能作答」的判据与阈值。

## 为什么需要这个脚本

原实现拿**混合分**同时干两件事：给候选排序，以及判断能不能作答。
排序它做得很好，判断它做不了 —— 因为混合分里的 BM25 是
`bm_raw / bm_max` **除以本批最大值**归一化的。全部候选都不相关时，
最像的那个照样得 1.0，直接贡献 0.5 分把无关条目推过阈值。

**归一化 BM25 只能回答"这批里哪个最像"，回答不了"它到底像不像"。**
把相对排名当成绝对相关性，就会出现这样的假阳性：

    公司的宠物友好日是哪天？ 0.6327 → 命中「试用期多久？」→ 判定可作答

再往下走，Agent 就会拿试用期的答案回答宠物友好日 ——
**这是检索层造出来的幻觉，不是模型编的。**

## 这个脚本做什么

拿两组样本（知识库里没有的 / 有的）跑检索，比较两个判据的**可分性**，
并在可分的那个上找阈值。首次运行的结论：

    混合分   负 [0.199, 0.846]  正 [0.528, 0.961]  间隔 -0.318  重叠
    余弦     负 [0.265, 0.692]  正 [0.745, 0.922]  间隔 +0.054  可分

混合分区间重叠，**调任何阈值都会顾此失彼**；余弦可分，取中点 0.72。

## 什么时候要重跑

知识库规模变化、换 embedding 模型、加新领域 —— 都要重标。
可用区间只有 0.054 宽，不宽裕。

    uv run python scripts/calibrate_gate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ramp import config, knowledge  # noqa: E402

# 知识库里**没有**的问题。它们必须转人工。
NEGATIVE = [
    ("公司的宠物友好日是哪天？", "hr"), ("团建预算是多少？", "hr"),
    ("董事长叫什么名字？", "hr"), ("食堂几点关门？", "hr"),
    ("停车位怎么申请？", "hr"), ("公司有健身房吗？", "hr"),
    ("年会在哪里办？", "hr"), ("工位可以换吗？", "it"),
    ("公司股票代码是多少？", "hr"), ("班车路线有哪些？", "hr"),
]

# 知识库里**有**的问题（含换说法的）。它们必须能作答。
POSITIVE = [
    ("试用期多久？", "hr"), ("年假有多少天？", "hr"),
    ("怎么申请生产库的只读权限？", "it"), ("第一次提交代码要注意什么？", "biz"),
    ("报销标准是多少？", "hr"), ("忘记打卡怎么补？", "hr"),
    ("新人培训必须参加吗？", "hr"), ("转正要交什么材料？", "hr"),
    ("怎么申请 VPN？", "it"), ("代码评审多久有人看？", "biz"),
    ("线上出故障怎么处理？", "biz"), ("社保什么时候开始交？", "hr"),
    ("病假需要什么证明？", "hr"), ("需求是怎么流转的？", "biz"),
    ("技术文档在哪里？", "biz"),
]


def probe(q: str, dom: str) -> tuple[float, float]:
    """返回 (混合分, 余弦)。检索层不调模型，**零随机性**——
    同一个 query 跑一百次结果完全一致，所以这里测出来的是确定性事实，
    不是需要做显著性检验的波动。"""
    r = knowledge.search(q, domain=dom, top_k=1)
    dense = getattr(r.hits[0], "dense", 0.0) if r.hits else 0.0
    return r.best_score, dense


def main() -> None:
    neg = [probe(q, d) for q, d in NEGATIVE]
    pos = [probe(q, d) for q, d in POSITIVE]

    print()
    print("=" * 78)
    print(f"  判据标定   负样本 {len(neg)} 条 · 正样本 {len(pos)} 条")
    print("=" * 78)

    print("\n  可分性")
    print("  " + "-" * 74)
    best_idx = None
    for label, idx in (("混合分", 0), ("余弦", 1)):
        nv = [x[idx] for x in neg]
        pv = [x[idx] for x in pos]
        gap = min(pv) - max(nv)
        verdict = "可分" if gap > 0 else "重叠 · 不能作为判据"
        print(f"  {label:<8} 负 [{min(nv):.4f}, {max(nv):.4f}]   "
              f"正 [{min(pv):.4f}, {max(pv):.4f}]   间隔 {gap:+.4f}  {verdict}")
        if gap > 0 and best_idx is None:
            best_idx = idx

    if best_idx is None:
        print("\n  ✗ 两个判据都不可分 —— 说明检索本身需要改进（换模型、加 rerank），"
              "\n    调阈值解决不了。")
        sys.exit(1)

    nv = [x[best_idx] for x in neg]
    pv = [x[best_idx] for x in pos]
    lo, hi = max(nv), min(pv)
    mid = (lo + hi) / 2
    print(f"\n  可用区间 [{lo:.4f}, {hi:.4f}]  宽 {hi - lo:.4f}")
    print(f"  建议阈值 {mid:.2f}   （取中点，两边裕量最大）")
    print(f"  当前配置 {config.SEMANTIC_FLOOR}")

    fp = sum(1 for x in nv if x >= config.SEMANTIC_FLOOR)
    miss = sum(1 for x in pv if x < config.SEMANTIC_FLOOR)
    print()
    print(f"  按当前配置：漏假阳性 {fp}/{len(nv)}   误伤真命中 {miss}/{len(pv)}")
    if fp or miss:
        print(f"  ✗ 不达标，建议把 RAMP_SEMANTIC_FLOOR 设为 {mid:.2f}")
        sys.exit(1)
    if hi - lo < 0.05:
        print("  ⚠ 可用区间偏窄（< 0.05），扩充知识库后请重跑本脚本")
    print("  ✓ 通过")
    print()


if __name__ == "__main__":
    main()
