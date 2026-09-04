"""检索层回归：证明「语义下限」拦住了幻觉，且没有误伤真命中。

## 这个检查为什么存在

混合检索里的 BM25 是**归一化**的（除以本批候选最大值），所以哪怕所有
候选都毫不相关，最像的那个照样得 1.0，在混合分里贡献 0.5 分 ——
足以把一条完全无关的知识推过 0.62 的作答阈值。

实测抓到三条：

    公司的宠物友好日是哪天？ → 0.6327  命中「试用期多久？」   判定：作答
    团建预算是多少？         → 0.7242  命中「报销标准是多少？」判定：作答
    董事长叫什么名字？       → 0.6379  命中「转正材料」       判定：作答

Agent 会拿试用期的答案去回答宠物友好日 —— **这是检索层造的幻觉，
不是模型编的。** 根因是把「相对排名」当成了「绝对相关性」。

修法是加一道语义相似度的绝对下限（余弦有界 [0,1]，有绝对含义）。

## 为什么必须两组一起测

只测 A 组的话，把下限调到 1.0 也能"全部通过" —— 那不是修好了，是把产品废了。
一个过滤器必须同时证明「拦住了该拦的」和「没伤到不该拦的」。

用法：
    uv run python scripts/check_retrieval.py
"""

import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from ramp import config, knowledge  # noqa: E402

# A 组：知识库里**根本没有**的问题，应该全部转人工
NEGATIVE = [
    ("公司的宠物友好日是哪天？", "hr"),
    ("团建预算是多少？", "hr"),
    ("董事长叫什么名字？", "hr"),
    ("食堂几点关门？", "hr"),
    ("停车位怎么申请？", "hr"),
    # 「公司有健身房吗？」被我从这一组移走了 —— 它**不是**假阳性。
    # 知识库里「公司有哪些福利？」那条写着"健身房补贴每月 200 元"，
    # 语义分 0.6915 是合理命中，问福利答福利。
    # **是我的测试用例设计错了，不是系统判错了。**
    # 教训：判定过滤器好坏之前，先确认自己的标注是对的。
    ("公司的股票期权怎么分配？", "hr"),
]

# B 组：知识库里**确实有**的问题，应该全部照常作答
POSITIVE = [
    ("试用期多久？", "hr"),
    ("年假有多少天？", "hr"),
    ("怎么申请生产库的只读权限？", "it"),
    ("报销标准是多少？", "hr"),
    ("怎么申请 VPN？", "it"),
    ("第一次提交代码前要注意什么？", "biz"),
    ("转正需要提交什么材料？", "hr"),
    ("忘记打卡怎么补？", "hr"),
    ("公司有健身房吗？", "hr"),   # 福利条目里含"健身房补贴"，属真命中
]


def run(cases, want_confident, label):
    print(f"\n  [{label}]  期望：{'作答' if want_confident else '转人工'}")
    print(f"    {'提问':<28}{'混合分':>8}{'语义':>8}{'BM25':>8}{'判定':>8}  命中")
    print("    " + "-" * 84)
    wrong = []
    for q, dom in cases:
        r = knowledge.search(q, domain=dom, top_k=1)
        h = r.hits[0] if r.hits else None
        dense = getattr(h, "dense", 0.0) if h else 0.0
        bm = getattr(h, "bm25", 0.0) if h else 0.0
        title = getattr(h, "question", "") if h else "（无）"
        ok = (r.confident == want_confident)
        if not ok:
            wrong.append(q)
        print(f"    {q:<28}{r.best_score:>8.4f}{dense:>8.4f}{bm:>8.4f}"
              f"{('作答' if r.confident else '转人工'):>8}  "
              f"{'' if ok else 'X '}{title[:22]}")
    return wrong


print("=" * 92)
print(f"  语义下限对照实验   SEMANTIC_FLOOR = {config.SEMANTIC_FLOOR}"
      f"   作答阈值 = {config.CONFIDENCE_THRESHOLD}")
print("=" * 92)

bad_neg = run(NEGATIVE, False, "A 组 · 知识库没有的问题")
bad_pos = run(POSITIVE, True, "B 组 · 知识库有的问题")

print("\n" + "=" * 92)
print(f"  A 组 假阳性（该拦没拦）: {len(bad_neg)} / {len(NEGATIVE)}   {bad_neg}")
print(f"  B 组 误伤（不该拦却拦了）: {len(bad_pos)} / {len(POSITIVE)}   {bad_pos}")
print("=" * 92)
sys.exit(1 if (bad_neg or bad_pos) else 0)
