"""把采集到的语料整理成可读的编码档案。

采集脚本产出的是原始 JSON，没法直接看，也没法拿去当作品集材料。
这个脚本做三件事：

  1. **只统计 real**——厂商 SEO 软文单独列出，不进任何结论。
     它们读起来也像用户在说话，但那是营销文案在模仿用户口吻。
  2. 按主题分组，每组附几条原文摘录——**主题编码的价值在于能看到原话**，
     只给一个计数是没用的。
  3. 输出待核对清单：`human_checked` 为 null 的都在里面。

用法：
    uv run python scripts/corpus_report.py            # 打印
    uv run python scripts/corpus_report.py --md       # 生成 markdown 档案
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "seed" / "corpus" / "corpus_raw.json"
OUT = ROOT / "seed" / "corpus" / "corpus_coded.md"


def load() -> list[dict]:
    if not SRC.exists():
        raise SystemExit(f"没有语料文件：{SRC}\n先跑 scripts/collect_corpus.py")
    return json.loads(SRC.read_text(encoding="utf-8"))


def build_md(rows: list[dict]) -> str:
    real = [r for r in rows if r["source_quality"] == "real"]
    vendor = [r for r in rows if r["source_quality"] != "real"]
    checked = [r for r in real if r.get("human_checked") is not None]

    L: list[str] = []
    L.append("# 公开语料 · 主题编码档案")
    L.append("")
    L.append("> 采集自公开搜索结果的**标题与摘要**，不是完整帖子。")
    L.append("> 摘要通常 60–120 字，够做主题编码，不够做深度语言分析——")
    L.append("> 要引用具体某句话，必须点开原链接核对。")
    L.append("")
    L.append("## 样本构成")
    L.append("")
    L.append("| 项 | 数量 | 说明 |")
    L.append("|---|---:|---|")
    L.append(f"| 采集总数 | {len(rows)} | |")
    L.append(f"| **真实用户发言 real** | **{len(real)}** | 知乎 / 牛客 / 豆瓣 / V2EX 等，**结论只用这部分** |")
    L.append(f"| 厂商 SEO vendor | {len(vendor)} | HR SaaS、培训机构的软文，**不进任何结论** |")
    L.append(f"| 已人工核对 | {len(checked)} / {len(real)} | 未核对的不能作为研究依据 |")
    L.append("")

    if len(checked) < len(real):
        L.append(f"> ⚠️ **还有 {len(real) - len(checked)} 条未核对。**")
        L.append("> 摘要可能断章取义——用它做判断之前必须打开原链接确认。")
        L.append("")

    L.append("### 为什么要把 vendor 分出去")
    L.append("")
    L.append("那些软文读起来也像用户在诉苦，但它们是**营销文案在模仿用户口吻**，")
    L.append("为搜索排名而写。拿它们当用户研究依据，等于把自己的判断")
    L.append("建立在别人的关键词布局上。")
    L.append("")

    L.append("## 来源分布（仅 real）")
    L.append("")
    L.append("| 域名 | 条数 |")
    L.append("|---|---:|")
    for dom, n in Counter(r["domain"] for r in real).most_common():
        L.append(f"| {dom} | {n} |")
    L.append("")

    L.append("## 主题编码（仅 real）")
    L.append("")
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for r in real:
        by_theme[r["theme"]].append(r)

    for theme, items in sorted(by_theme.items(), key=lambda x: -len(x[1])):
        flag = " ⚠️ 需人工归类" if theme == "未分类" else ""
        L.append(f"### {theme} · {len(items)} 条{flag}")
        L.append("")
        for r in items[:4]:
            mark = "" if r.get("human_checked") else " `未核对`"
            L.append(f"- **[{r['id']}]**{mark} {r['snippet'][:110]}")
            L.append(f"  <br><sub>{r['domain']} · [原链接]({r['url']})</sub>")
        if len(items) > 4:
            L.append(f"- …另有 {len(items) - 4} 条")
        L.append("")

    L.append("## 待核对清单")
    L.append("")
    todo = [r for r in real if r.get("human_checked") is None]
    L.append(f"共 {len(todo)} 条。逐条打开链接确认摘要没有断章取义，")
    L.append("然后把 `corpus_raw.json` 里对应的 `human_checked` 改成 `true` / `false`。")
    L.append("")
    L.append("| id | 主题 | 摘要 | 链接 |")
    L.append("|---|---|---|---|")
    for r in todo[:40]:
        snippet = r["snippet"][:44].replace("|", "｜")
        L.append(f"| {r['id']} | {r['theme']} | {snippet}… | [开]({r['url']}) |")
    if len(todo) > 40:
        L.append(f"| … | | 另有 {len(todo) - 40} 条 | |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*采集脚本：`scripts/collect_corpus.py`｜本档案：`scripts/corpus_report.py`*")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true", help="写出 markdown 档案")
    args = ap.parse_args()

    rows = load()
    real = [r for r in rows if r["source_quality"] == "real"]

    print(f"总计 {len(rows)} 条 ｜ real {len(real)} ｜ vendor {len(rows) - len(real)}")
    print()
    print("主题分布（仅 real）：")
    for theme, n in Counter(r["theme"] for r in real).most_common():
        print(f"  {theme:22s} {n:3d}  {'█' * n}")
    todo = sum(1 for r in real if r.get("human_checked") is None)
    print()
    print(f"待人工核对 {todo} / {len(real)} 条")

    if args.md:
        OUT.write_text(build_md(rows), encoding="utf-8")
        print(f"\n档案已写出 → {OUT}")


if __name__ == "__main__":
    main()
