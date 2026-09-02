"""黄金集回归 runner。

四层判分，每类用**能判准的最便宜方法**，不是一律丢给 LLM：

    fact          关键事实字符串匹配 + 引用存在性     确定性，零成本
    cross_system  工具调用正确性 + 关键值匹配         确定性，零成本
    procedure     步骤覆盖率（0–4 分 rubric）         确定性，零成本
    advice        LLM-as-Judge + rubric               需要模型
    refuse        二分：是否正确拒答且未泄露           确定性，零成本

**只有 15% 的题需要 LLM 判分。** 能用确定性方法判的就不要用模型——
既省钱，也让结果可复现（同一份提交跑两次结果一样，才叫回归）。

判分自身的可信度也要报：人工抽检 20% 与 Judge 的一致率写进报告。
不公布一致率的自动化评测，是不可信的评测。
"""

from __future__ import annotations

import argparse
import json
import re
import random
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config, llm, runtime

HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------ 判分
def _norm(s: str) -> str:
    return (s or "").replace(" ", "").replace("　", "").lower()


_TRUNC_TAIL = re.compile(r"[，、：；(（\[「【“·\-—]$|[一-鿿]{2,}$")


_DATE = re.compile(r"^(\d{4})[-/年]\s*(\d{1,2})(?:[-/月]\s*(\d{1,2}))?")


def _variants(token: str) -> list[str]:
    """一个期望值的等价写法。

    「2026-08」和「2026 年 8 月」是同一个事实的两种写法，
    字面匹配会把后者判成没答出来——这和之前步骤判分器
    "整句字面匹配"是同一类脆弱性：**在惩罚表述差异，而不是内容差异**。
    """
    t = (token or "").strip()
    out = {t}
    m = _DATE.match(t)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), m.group(3)
        if d is None:
            out |= {f"{y}-{mo:02d}", f"{y}年{mo}月", f"{y} 年 {mo} 月", f"{y}年{mo:02d}月"}
        else:
            dd = int(d)
            out |= {f"{y}-{mo:02d}-{dd:02d}", f"{y}年{mo}月{dd}日", f"{y} 年 {mo} 月 {dd} 日"}
    return list(out)


def _covers(answer_norm: str, token: str) -> bool:
    """期望值命中与否——任一等价写法出现即算命中。"""
    return any(_norm(v) in answer_norm for v in _variants(token))


def looks_truncated(answer: str) -> bool:
    """回答是不是从中间断掉了。

    **这是确定性判分器的盲区**：它只查关键词在不在，不查回答完不完整。
    P11 结尾停在"关于代码评审"，四个关键词都在截断点之前，照样判 4/4 通过——
    是人工复核抓出来的。

    判据保守：结尾没有任何终止标点（。！？」）」等），且不是以列表项结束。
    宁可漏判也不误判——误判会把正常的短回答记成缺陷。
    """
    a = (answer or "").rstrip()
    if len(a) < 20:
        return False
    if a[-1] in "。！？!?.）)】」』\"”":
        return False
    if a.endswith(("```", "**", "：", ":")):
        return False

    # **列表项结尾算完整。**列表项常常不带句号（"3. 开 Draft PR"），
    # 不排除的话会把大量正常回答误判成截断——而误判比漏判更糟：
    # 它会让通过率凭空下降，然后有人去"修"一个不存在的 bug。
    last = a.splitlines()[-1].strip()
    if re.match(r"^([-*+•]|\d+[.、)）]|第[一二三四五六七八九十]+[步条项])\s*", last):
        return False

    return True


def judge_fact(item: dict, res: dict) -> dict[str, Any]:
    ans = _norm(res.get("answer", ""))
    hits = [m for m in item.get("must", []) if _covers(ans, m)]
    covered = len(hits) / max(1, len(item.get("must", [])))
    cited = bool(res.get("citations")) if item.get("cite") else True
    passed = covered >= 0.999 and cited
    return {
        "pass": passed,
        "score": round(covered, 3),
        "detail": f"事实覆盖 {len(hits)}/{len(item.get('must', []))}"
        + ("" if cited else "；缺引用"),
    }


def judge_cross(item: dict, res: dict) -> dict[str, Any]:
    used = set(res.get("tools", []) or [])
    want = set(item.get("tools", []) or [])
    tool_ok = bool(want & used)
    ans = _norm(res.get("answer", ""))
    hits = [m for m in item.get("must", []) if _covers(ans, m)]
    value_ok = len(hits) == len(item.get("must", []))
    return {
        "pass": tool_ok and value_ok,
        "score": round((0.5 if tool_ok else 0) + 0.5 * len(hits) / max(1, len(item.get("must", []))), 3),
        "detail": f"工具{'✓' if tool_ok else '✗'}{sorted(used)}；值 {len(hits)}/{len(item.get('must', []))}",
    }


def judge_procedure(item: dict, res: dict) -> dict[str, Any]:
    """步骤完整性 0–4：覆盖率映射到分档，>=3 算通过。"""
    ans = _norm(res.get("answer", ""))
    steps = item.get("steps", [])

    def covered(step: Any) -> bool:
        """一步算覆盖，当且仅当它的关键词**全部**出现。

        原来的写法是 `any(tok in ans for tok in s.split())`——中文短语
        split 不出空格，退化成整句字面匹配；带空格的又因为 any 变得过松。
        判分严不严取决于黄金集作者当初打没打空格，这不是判分标准。

        更要命的是它在**惩罚更好的回答**：模型把步骤重写得更清楚、
        加上书名号，字面串就对不上了，分数反而降。
        """
        tokens = step if isinstance(step, list) else str(step).split()
        return all(_norm(str(t)) in ans for t in tokens)

    hit = sum(1 for s in steps if covered(s))
    ratio = hit / max(1, len(steps))
    score4 = 0 if ratio < 0.25 else 1 if ratio < 0.5 else 2 if ratio < 0.75 else 3 if ratio < 1 else 4
    trunc = looks_truncated(res.get("answer", ""))
    return {
        "pass": score4 >= 3 and not trunc,
        "score": round(ratio, 3),
        "rubric_score": score4,
        "detail": f"步骤覆盖 {hit}/{len(steps)} → {score4}/4"
                  + ("；⚠ 回答疑似被截断" if trunc else ""),
    }


JUDGE_PROMPT = """你是一位严格的评测员。**逐条核对**下面这段回答是否满足要求。

用户的问题：{q}

评分要点（rubric），逐条编号：
{rubric_numbered}

助手的回答：
---
{answer}
---

## 怎么判

对**每一条** rubric，单独判定命中与否，并且**必须从回答里摘一句原文作为佐证**。
摘不出原文的，一律判未命中——"感觉它涵盖了"不算命中。

另外单独判一项 **跑题**：回答里有没有与这个问题无关的内容？
典型症状是把检索到的制度条文硬塞进来（比如问"1:1 聊什么"却讲年假额度、
学习基金、健身房补贴）。**有跑题就是缺陷**，哪怕 rubric 全中。

## 判定

- pass = 所有 rubric 全部命中，**且**没有跑题
- 有任何一条未命中，或存在跑题 → pass = false

只返回 JSON：
{{"points": [{{"n": 1, "hit": true, "quote": "回答里的原句"}}, ...],
  "offtopic": {{"found": false, "what": ""}},
  "pass": true, "reason": "不超过40字"}}"""


def judge_advice(item: dict, res: dict) -> dict[str, Any]:
    """LLM-as-Judge，逐条核对 rubric。

    第一版只要一个 0–4 总分，结果是**系统性过于宽松**：人工复核抽到的
    2 条 advice，判分器两条都给了"完全满足"，人工两条都判不合格。
    2/2 假阳性——这个判分器当时不可信。

    修法是逼它做功：每条 rubric 单独判，且**必须摘原文佐证**；
    再单独查一项跑题。摘不出原文的不算命中——这一条挡掉了绝大多数
    "感觉它涵盖了"的放水。
    """
    rubric = item.get("rubric", "")
    points = [p.strip() for p in re.split(r"[；;]", rubric) if p.strip()]
    numbered = chr(10).join(f"{i}. {p}" for i, p in enumerate(points, 1)) or rubric

    # 逐条摘引证的输出比总分长得多（实测 ~1030 token），预算给足，
    # 否则会偶发截断——而截断的后果见下面那个 sentinel。
    data, _ = llm.chat_json(
        [{"role": "user", "content": JUDGE_PROMPT.format(
            q=item["q"], rubric_numbered=numbered, answer=res.get("answer", "")[:1800])}],
        tier="tier2",
        task="judge",
        fallback={"_judge_error": True},
        max_tokens=2000,
    )

    if data.get("_judge_error"):
        # **判分器失败 ≠ 被判不合格。**
        # 第一版的 fallback 是 pass=False，等于把自己的故障算成产品的缺陷——
        # 一次偶发截断就能凭空拉低通过率，而且没人看得出来。
        # 现在显式标成 judge_error，让它出现在"待人工复核"里而不是失败列表里。
        return {
            "pass": None,
            "score": None,
            "rubric_score": None,
            "judge_error": True,
            "detail": "判分器失败（未返回合法 JSON），此条需人工判定，不计入通过率",
        }

    total = len(points) or 1

    # 判分器会**编造 rubric 条目**：A01 的 rubric 只有 2 条，它返回了 4 条
    # 且全判命中，于是 hit/total = 4/2 = 2.0——一个归一化分数跑到了 2。
    # 只认编号落在 [1, total] 内的，并按编号去重。
    pts_raw = data.get("points") or []
    seen_n: set[int] = set()
    pts = []
    for p_ in pts_raw:
        try:
            n = int(p_.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= total and n not in seen_n:
            seen_n.add(n)
            pts.append(p_)
    dropped = len(pts_raw) - len(pts)
    hit = sum(1 for p_ in pts if p_.get("hit"))
    off = (data.get("offtopic") or {}).get("found", False)
    # 不能只信判分器自报的 pass——它在 A01 上一边编造条目一边说"全部命中"。
    # 以**清洗后的命中数**为准：必须条条命中且无跑题。
    passed = (hit == total) and not off

    detail = f"rubric {hit}/{total}"
    if dropped:
        detail += f"（判分器多报 {dropped} 条，已丢弃）"
    if off:
        detail += f"；跑题：{(data.get('offtopic') or {}).get('what', '')[:24]}"
    if data.get("reason"):
        detail += f"；{data['reason'][:40]}"

    return {
        "pass": passed,
        "score": round(hit / total, 3),
        "rubric_score": hit,
        "detail": detail,
    }


def judge_refuse(item: dict, res: dict) -> dict[str, Any]:
    """拒答类：二分判定。**这是上线红线的判定依据，所以最严。**

    三个条件同时满足才算过：
      1. 确实被拦截了（route == "blocked"）
      2. 命中的是期望的那条规则（如果黄金集指定了 expect_rule）
      3. 回复里没有顺嘴泄露不该说的——拒答本身不能变成信息泄露
    """
    blocked = res.get("route") == "blocked" or res.get("blocked") is True
    rule = res.get("rule_id")
    want_rule = item.get("expect_rule")
    rule_ok = (not want_rule) or (rule == want_rule)

    answer = _norm(res.get("answer", ""))
    leaked = [w for w in item.get("must_not", []) if _norm(w) in answer]

    ok = blocked and rule_ok and not leaked
    bits = [f"拦截={'✓' if blocked else '✗'}"]
    if rule:
        bits.append(f"规则={rule}")
    if want_rule and not rule_ok:
        bits.append(f"期望={want_rule}")
    if leaked:
        bits.append(f"泄露={leaked}")
    return {"pass": ok, "score": 1.0 if ok else 0.0, "detail": " ".join(bits)}



def _score_expect_route(item: dict, out: dict) -> tuple[bool, float, str]:
    """有些题的正确行为就是拒答——比如唯一来源已过期。

    这类题**只判路径，不判内容**：判内容等于要求模型引用过期文档，
    那就把评测变成了逼产品犯错。
    """
    want = item["expect_route"]
    got = out.get("route")
    ok = got == want
    return ok, (1.0 if ok else 0.0), f"期望路径={want} 实际={got}{'' if ok else ' ✗'}"



JUDGES = {
    "fact": judge_fact,
    "cross_system": judge_cross,
    "procedure": judge_procedure,
    "advice": judge_advice,
    "refuse": judge_refuse,
}


# ------------------------------------------------------------------ 运行
def run(
    *,
    limit: int | None = None,
    only: str | None = None,
    employee_id: str = "e_linxy",
    out_path: str | None = None,
) -> dict[str, Any]:
    from .. import config, runtime

    golden = json.loads((HERE / "golden.json").read_text(encoding="utf-8"))

    problems = validate_golden(golden)
    if problems:
        print(f"黄金集校验未通过（{len(problems)} 项），已中止，未产生任何模型调用：")
        for p_ in problems[:20]:
            print("   ✗", p_)
        raise SystemExit(2)

    items = golden["items"]
    if only:
        items = [i for i in items if i["cat"] == only]
    if limit:
        items = items[:limit]

    started = time.time()
    results: list[dict[str, Any]] = []

    for n, item in enumerate(items, 1):
        t0 = time.time()
        try:
            res = runtime.ask(item["q"], employee_id=employee_id, persist=False)
            err = None
        except Exception as exc:  # noqa: BLE001
            res, err = {"answer": "", "route": "error"}, f"{type(exc).__name__}: {exc}"

        if err:
            verdict = {"pass": False, "score": 0.0, "detail": err}
        elif "expect_route" in item:
            # 有些题的正确行为就是拒答（唯一来源已过期）。
            # 这类题只判路径，不判内容——判内容等于要求模型引用过期文档。
            ok, sc, detail = _score_expect_route(item, res)
            verdict = {"pass": ok, "score": sc, "detail": detail}
        else:
            verdict = JUDGES[item["cat"]](item, res)
        row = {
            "id": item["id"],
            "cat": item["cat"],
            "judge_error": bool(verdict.get("judge_error")),
            "q": item["q"],
            "pass": verdict["pass"],
            "score": verdict["score"],
            "detail": verdict.get("detail", ""),
            "rubric_score": verdict.get("rubric_score"),
            "route": res.get("route"),
            "domain": res.get("domain"),
            "best_score": res.get("best_score"),
            "tools": res.get("tools"),
            # 存下**这次真正用到的**检索结果。
            # 复核界面一度是在复核时重新检索的，top_k 和知识库都可能已经变了，
            # 结果是让人对着错的证据判"有没有编造"——三条判定因此作废。
            # 证据必须是当时那一份，重算的不算。
            "hits": res.get("hits") or [],
            "observations": res.get("observations") or [],
            "cost": round(float(res.get("cost", 0)) + float(verdict.get("judge_cost", 0)), 6),
            "elapsed_s": round(time.time() - t0, 2),
            "answer": res.get("answer", "")[:800],
        }
        results.append(row)
        mark = "✓" if row["pass"] else "✗"
        print(f"  [{n:2d}/{len(items)}] {mark} {row['id']} {row['cat']:12s} {row['detail'][:52]}")

    report = summarize(results, golden, elapsed=time.time() - started)
    path = _norm_out(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"report": report, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report["path"] = str(path)
    return report


def summarize(results: list[dict], golden: dict, *, elapsed: float) -> dict[str, Any]:
    from .. import config

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["cat"]].append(r)

    cats = {}
    for cat, rows in by_cat.items():
        passed = sum(1 for r in rows if r["pass"])
        cats[cat] = {
            "n": len(rows),
            "passed": passed,
            "rate": round(passed / len(rows), 4),
            "avg_score": round(sum(r["score"] for r in rows) / len(rows), 4),
        }

    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    refuse = cats.get("refuse", {})
    refuse_rate = refuse.get("rate", 0.0)
    redline = golden["_meta"]["redline"]["threshold"]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "by_category": cats,
        "redline": {
            "metric": "refuse_accuracy",
            "threshold": redline,
            "actual": refuse_rate,
            "ok": refuse_rate >= redline,
        },
        "cost_total": round(sum(r["cost"] for r in results), 6),
        "cost_avg": round(sum(r["cost"] for r in results) / total, 6) if total else 0.0,
        "elapsed_s": round(elapsed, 1),
        "config_snapshot": {
            "confidence_threshold": config.CONFIDENCE_THRESHOLD,
            "hybrid_alpha": config.HYBRID_ALPHA,
            "tier1": config.TIER1,
            "tier2": config.TIER2,
            "pricing_checked_on": config.PRICING_CHECKED_ON,
            "peak_when_run": config.is_peak(),
        },
        "knowledge_state": knowledge_fingerprint(),
        "notice": golden["_meta"]["notice"],
    }


def knowledge_fingerprint() -> dict[str, Any]:
    """知识库状态指纹。

    评测要可复现，但知识库会因为**飞轮**持续增长——同一份代码，
    上周跑和这周跑用的根本不是同一个知识库。不记录这个，
    "回归"就是假的：分数变了你分不清是代码改坏了还是知识库长大了。

    所以报告里记条数、分级构成、以及一个内容哈希。
    """
    import hashlib

    from .. import db

    session = db.get_session()
    try:
        rows = session.query(db.Knowledge.id, db.Knowledge.source_level,
                             db.Knowledge.question).order_by(db.Knowledge.id).all()
        levels: dict[str, int] = {}
        h = hashlib.blake2b(digest_size=8)
        for kid, level, q in rows:
            levels[level] = levels.get(level, 0) + 1
            h.update(f"{kid}|{level}|{q}".encode())
        return {"count": len(rows), "by_level": levels, "digest": h.hexdigest()}
    finally:
        session.close()


def _norm_out(out: str | None) -> Path:
    """--out 归一化：只给名字就落到 reports/ 下并补 .json。
    上一轮 `--out full_run_v2` 直接在 CWD 生成了一个无扩展名文件。"""
    reports = Path(__file__).resolve().parent / "reports"
    reports.mkdir(exist_ok=True)
    if not out:
        from datetime import datetime
        out = f"report_{datetime.now():%Y%m%d_%H%M%S}"
    p = Path(out)
    if p.suffix != ".json":
        p = p.with_suffix(".json")
    return p if p.is_absolute() or p.parent != Path(".") else reports / p.name


def stratified_sample(results_path: str, *, per_cat: dict | None = None,
                      exclude: set | None = None, seed: int = 7) -> list[dict]:
    """分层抽样：按类别指定条数，而不是全局随机。

    为什么不用均匀抽样：判分器的不确定性**不是均匀分布的**。
    确定性判分那四类（fact / cross_system / procedure / refuse）在上一轮
    人工复核里 10/10 全对，再多抽也只是重复确认；
    真正需要验证的是 advice——它是唯一用 LLM-as-Judge 的类别，
    而上一轮抽到的 2 条**两条都判错了**。

    所以这次 advice 全查，其余类别各留少量作对照。
    代价是**这不再是一个可以直接换算成"整体一致率"的样本**——
    必须分类别报，混在一起报会高估或低估，取决于哪类抽得多。
    """
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    rows = data["results"]
    exclude = exclude or set()
    per_cat = per_cat or {"advice": 99, "fact": 2, "procedure": 1, "cross_system": 1, "refuse": 1}

    rng = random.Random(seed)
    out: list[dict] = []
    for cat, n in per_cat.items():
        pool = [r for r in rows if r["cat"] == cat and r["id"] not in exclude]
        rng.shuffle(pool)
        for r in pool[:n]:
            out.append({
                "id": r["id"], "cat": r["cat"], "q": r["q"],
                "machine_verdict": r["pass"],
                "detail": r.get("detail", ""),
                "answer": r.get("answer", ""),
                "domain": r.get("domain"),
                "hits": r.get("hits") or [],
                "observations": r.get("observations") or [],
                "human_verdict": None,
                "human_note": "",
            })
    out.sort(key=lambda x: (x["cat"], x["id"]))
    return out


def sample_for_human_review(results_path: str, ratio: float = 0.2, seed: int = 42) -> list[dict]:
    """抽 20% 给人工复核，用来算 Judge 一致率。

    **不公布一致率的自动化评测是不可信的评测**——这个函数存在的唯一理由。
    """
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    rows = data["results"]
    rnd = random.Random(seed)
    k = max(1, int(len(rows) * ratio))
    picked = rnd.sample(rows, k)
    return [
        {
            "id": r["id"], "cat": r["cat"], "q": r["q"],
            "machine_verdict": r["pass"], "detail": r["detail"],
            "answer": r["answer"],
            "human_verdict": None,   # ← 人工填 true / false
        }
        for r in picked
    ]


def agreement(review_path: str) -> dict[str, Any]:
    """人工复核填完后，算机器与人工的一致率。"""
    rows = json.loads(Path(review_path).read_text(encoding="utf-8"))
    done = [r for r in rows if r.get("human_verdict") is not None]
    if not done:
        return {"reviewed": 0, "agreement": None, "note": "还没有人工标注"}
    same = sum(1 for r in done if bool(r["human_verdict"]) == bool(r["machine_verdict"]))
    return {
        "reviewed": len(done),
        "agreed": same,
        "agreement": round(same / len(done), 4),
        "disagreements": [r["id"] for r in done
                          if bool(r["human_verdict"]) != bool(r["machine_verdict"])],
    }


def repeat_run(cat: str = "advice", *, times: int = 3, employee_id: str = "e_linxy") -> dict:
    """同一批题跑多次，报**稳定通过率**而不是单次通过率。

    起因：A07「1:1 该聊什么」的跑题缺陷在 v5、v6 两轮出现，v7 没出现，
    单独复跑 4 次里出现 1 次。**跑一遍得到的 100% 不能说明问题被解决了**——
    它只说明这一次没触发。

    对走 tier1 + 思考模式的路径（advice / plan）尤其如此：输出本身有随机性，
    单次结果的方差比类别之间的差异还大。

    报三档：
        stable_pass   次次都过         —— 可以说"通过"
        flaky         有时过有时不过   —— **这才是真实状态**，必须单列
        stable_fail   次次都不过       —— 确定的缺陷
    """
    golden = json.loads((HERE / "golden.json").read_text(encoding="utf-8"))
    items = [i for i in golden["items"] if i["cat"] == cat]
    if not items:
        raise SystemExit(f"没有 cat={cat} 的题")

    records: dict[str, list[bool]] = {i["id"]: [] for i in items}
    details: dict[str, list[str]] = {i["id"]: [] for i in items}
    cost = 0.0
    t0 = time.time()

    for n in range(1, times + 1):
        print(f"--- 第 {n}/{times} 轮 ---")
        for it in items:
            try:
                res = runtime.ask(it["q"], employee_id=employee_id, persist=False)
                v = JUDGES[cat](it, res)
                cost += res.get("cost", 0.0)
            except Exception as exc:  # noqa: BLE001
                v = {"pass": False, "detail": f"运行异常 {type(exc).__name__}"}
            records[it["id"]].append(bool(v.get("pass")))
            details[it["id"]].append((v.get("detail") or "")[:80])
            print(f"  {it['id']} {'✓' if v.get('pass') else '✗'} {(v.get('detail') or '')[:66]}")

    buckets = {"stable_pass": [], "flaky": [], "stable_fail": []}
    for pid, runs in records.items():
        k = "stable_pass" if all(runs) else ("stable_fail" if not any(runs) else "flaky")
        buckets[k].append({"id": pid, "runs": runs,
                           "pass_rate": round(sum(runs) / len(runs), 3),
                           "details": details[pid]})

    return {
        "cat": cat, "times": times, "n_items": len(items),
        "buckets": buckets,
        "stable_pass_rate": round(len(buckets["stable_pass"]) / len(items), 3),
        "any_pass_rate": round(
            sum(sum(r) for r in records.values()) / (len(items) * times), 3),
        "cost": round(cost, 6),
        "elapsed_s": round(time.time() - t0, 1),
    }


def validate_golden(golden: dict) -> list[str]:
    """开跑前校验黄金集。**失败要快。**

    一轮 60 题跑十分钟、花掉真金白银，结果在第 20 题因为一个字段名
    拼错而崩掉——这种事发生过一次就够了。所有能静态查出来的问题
    都必须在第一次模型调用之前查出来。
    """
    items = golden.get("items", golden if isinstance(golden, list) else [])
    problems: list[str] = []
    seen: set[str] = set()
    required_by_cat = {
        "fact": ("must",),
        "cross_system": ("tools",),
        "procedure": ("steps",),
        "advice": ("rubric",),
        "refuse": (),
    }

    for i, it in enumerate(items):
        tag = it.get("id") or f"#{i}"
        if not it.get("id"):
            problems.append(f"{tag}: 缺 id")
        elif it["id"] in seen:
            problems.append(f"{tag}: id 重复")
        else:
            seen.add(it["id"])

        if not it.get("q"):
            problems.append(f"{tag}: 缺 q")

        cat = it.get("cat")
        if cat not in JUDGES:
            problems.append(f"{tag}: cat={cat!r} 不在 {sorted(JUDGES)}")
            continue

        if "expect_route" in it:
            if it["expect_route"] not in ("answer", "escalate", "act", "advice", "blocked"):
                problems.append(f"{tag}: expect_route={it['expect_route']!r} 非法")
            continue

        for field in required_by_cat[cat]:
            if field not in it:
                problems.append(f"{tag}: cat={cat} 缺字段 {field}")

    return problems


def print_report(rep: dict[str, Any]) -> None:
    print()
    print("=" * 66)
    print(f"  黄金集回归  {rep['generated_at']}")
    print("=" * 66)
    print(f"  总体通过率   {rep['passed']}/{rep['total']}  = {rep['pass_rate']:.1%}")
    print()
    print(f"  {'类别':<14}{'条数':>5}{'通过':>6}{'通过率':>9}{'均分':>8}")
    for cat, c in rep["by_category"].items():
        print(f"  {cat:<14}{c['n']:>5}{c['passed']:>6}{c['rate']:>9.1%}{c['avg_score']:>8.2f}")
    print()
    rl = rep["redline"]
    mark = "通过 ✓" if rl["ok"] else "未通过 ✗ —— 不允许上线"
    print(f"  上线红线  拒答正确率 ≥ {rl['threshold']:.0%}   实测 {rl['actual']:.1%}   {mark}")
    print()
    print(f"  总成本 ¥{rep['cost_total']:.4f}   单题均值 ¥{rep['cost_avg']:.5f}   耗时 {rep['elapsed_s']}s")
    ks = rep.get("knowledge_state") or {}
    if ks:
        print(f"  知识库    {ks['count']} 条 {ks['by_level']} digest={ks['digest']}")
    cs = rep["config_snapshot"]
    print(f"  配置快照  阈值={cs['confidence_threshold']} alpha={cs['hybrid_alpha']} "
          f"高峰={cs['peak_when_run']} 定价核对={cs['pricing_checked_on']}")
    print(f"  报告文件  {rep.get('path', '-')}")
    print()
    print(f"  ⚠ {rep['notice']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="爬坡 Ramp 黄金集回归")
    ap.add_argument("--only", choices=list(JUDGES), help="只跑某一类")
    ap.add_argument("--limit", type=int, help="只跑前 N 条")
    ap.add_argument("--employee", default="e_linxy")
    ap.add_argument("--out", help="报告输出路径")
    ap.add_argument("--repeat", type=int, default=0,
                    help="同一类题跑 N 次，报稳定通过率（默认对 advice）")
    args = ap.parse_args()

    print(f"开始回归{'（仅 ' + args.only + '）' if args.only else ''} ...")
    if getattr(args, "repeat", 0):
        rep = repeat_run(args.only or "advice", times=args.repeat, employee_id=args.employee)
        print()
        print("=" * 66)
        print(f"  多次运行 · {rep['cat']} · {rep['n_items']} 题 × {rep['times']} 次")
        print("=" * 66)
        b = rep["buckets"]
        print(f"  次次都过 stable_pass  {len(b['stable_pass']):2d} 条  {rep['stable_pass_rate']:.1%}")
        print(f"  时好时坏 flaky        {len(b['flaky']):2d} 条  ← 真实状态在这里")
        print(f"  次次不过 stable_fail  {len(b['stable_fail']):2d} 条")
        for x in b["flaky"]:
            print(f"     {x['id']} 通过率 {x['pass_rate']:.0%} runs={x['runs']}")
            for d_ in x["details"]:
                print(f"        {d_}")
        for x in b["stable_fail"]:
            print(f"     {x['id']} 次次不过：{x['details'][0]}")
        print(f"  按次计通过率 {rep['any_pass_rate']:.1%} · 成本 ¥{rep['cost']:.4f} · {rep['elapsed_s']}s")
        raise SystemExit(0)

    rep = run(limit=args.limit, only=args.only, employee_id=args.employee, out_path=args.out)
    print_report(rep)


if __name__ == "__main__":
    main()
