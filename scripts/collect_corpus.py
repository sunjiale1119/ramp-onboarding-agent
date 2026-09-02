"""公开语料采集：新人入职困惑的真实表达。

## 这个脚本产出什么

一份**主题编码档案**：每条记录含 原文摘要 / 来源域名 / 链接 / 主题标签 / 来源质量。

## 它不能替代什么

采到的是**搜索结果的标题与摘要**，不是完整帖子。摘要通常 60–120 字，
够做主题编码，不够做深度的语言分析。要引用具体某段话，需要点开原链接核对。

## 来源质量必须分开标

搜索结果里混着两类东西，价值完全不同：

    real      知乎 / 牛客 / 豆瓣 / V2EX / 微博 —— 真实用户自己写的
    vendor    HR SaaS、培训机构、招聘网站的 SEO 软文 —— 为排名而写

vendor 那些读起来也像模像样，但它们是**营销文案在模仿用户口吻**。
拿它们当用户研究依据，等于把自己的判断建立在别人的关键词布局上。
所以脚本把两类分开统计，作品集里只应引用 real 那部分。

用法：
    uv run python scripts/collect_corpus.py --target 120
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "seed" / "corpus"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ------------------------------------------------------------------ 查询集
# 按产品的主题分类来设计，而不是随便想几个词——这样采回来的分布
# 才能和知识库的域划分对得上。
QUERIES: list[tuple[str, str]] = [
    ("不敢问",   "新人入职 不敢问同事 怎么办"),
    ("不敢问",   "职场新人 同一个问题问第二遍 尴尬"),
    ("不敢问",   "实习生 问问题 怕被嫌烦"),
    ("入职材料", "入职材料 没交齐 影响 社保 首月工资"),
    ("入职材料", "新人入职 第一周 要做什么 清单"),
    ("权限账号", "新人入职 账号权限 没开通 干等"),
    ("权限账号", "公司内部系统 权限申请 流程 慢"),
    ("考勤假期", "试用期 请假 年假 怎么算 新人"),
    ("考勤假期", "忘记打卡 补卡 流程 公司"),
    ("报销制度", "新人 第一次报销 不会 发票 流程"),
    ("研发流程", "新人 第一次提交代码 code review 紧张"),
    ("研发流程", "新入职 开发环境 搭不起来 崩溃"),
    ("研发流程", "新人 线上故障 不知道该做什么"),
    ("周报汇报", "第一次写周报 不知道写什么 新人"),
    ("mentor",  "mentor 太忙 没人带 新人 怎么办"),
    ("mentor",  "入职 导师 1v1 聊什么"),
    ("转正",     "试用期 转正 材料 评审 流程"),
    ("融入",     "新人入职一个月 没产出 焦虑"),
    ("融入",     "入职新公司 找不到文档 到处问人"),
    ("融入",     "新人 入职 踩坑 经验 分享"),
]

REAL_DOMAINS = {
    "zhihu.com", "www.zhihu.com", "zhuanlan.zhihu.com",
    "nowcoder.com", "www.nowcoder.com",
    "douban.com", "www.douban.com",
    "v2ex.com", "www.v2ex.com",
    "weibo.com", "www.weibo.com",
    "xiaohongshu.com", "www.xiaohongshu.com",
    "juejin.cn", "segmentfault.com", "cnblogs.com", "www.cnblogs.com",
    "tieba.baidu.com", "maimai.cn", "www.maimai.cn",
}

# 主题编码词典。命中多个时取命中数最多的。
THEMES: dict[str, tuple[str, ...]] = {
    # 词典最初只有"新人视角"（不敢/尴尬/怕被），结果 46 条 real 里
    # **12 条落进未分类**，比任何一个真实主题都多。翻出来一看，
    # 其中 8 条讲的是同一件事：**被问的人怎么看反复提问**。
    #
    #   "问三遍以上说明不尊重回答者"
    #   "同一个问题问 2 次以上就控制不住不耐烦"
    #   "对前辈形成依赖，不经大脑问了又问"
    #
    # 这一半恰恰是产品最硬的论据——它证明新人的顾虑**不是想多了**，
    # 回答者那边确实在计数、确实会烦。漏掉它，产品叙事就只剩
    # "新人玻璃心"，立不住。
    #
    # 教训：**编码词典的盲区，就是你论证的盲区。**
    # 未分类占比过高不是"分类器不准"，是"你没想到那个角度"。
    "回答者视角 · 被问烦": ("不耐烦", "问了又问", "反复问", "问三遍", "问第二遍", "两遍",
                       "不尊重", "依赖", "会烦", "烦实习生", "没时间管", "打扰到同事",
                       "浪费时间", "自己先查"),
    "心理成本 · 不敢问": ("不敢", "尴尬", "怕被", "嫌烦", "内耗", "丢人", "笨", "焦虑", "紧张", "害怕"),
    "入职材料与手续": ("材料", "档案", "社保", "公积金", "合同", "体检", "离职证明", "报到"),
    "账号与权限": ("权限", "账号", "开通", "vpn", "登录", "sso", "密码", "工单"),
    "考勤与假期": ("考勤", "打卡", "请假", "年假", "病假", "调休", "加班"),
    "报销与财务": ("报销", "发票", "差旅", "补贴", "额度"),
    "研发流程": ("代码", "review", "评审", "环境", "发布", "上线", "故障", "分支", "提测", "联调"),
    "汇报与周报": ("周报", "日报", "汇报", "站会", "复盘", "述职"),
    "带教与 mentor": ("mentor", "导师", "带教", "师傅", "1v1", "1:1", "leader", "上级"),
    "转正与考核": ("转正", "试用期", "考核", "绩效", "评审"),
    "文档与信息获取": ("文档", "wiki", "找不到", "没人告诉", "规范", "手册", "知识库"),
    "融入与节奏": ("融入", "产出", "上手", "适应", "节奏", "第一个月", "一周"),
}


def ddg(query: str, timeout: int = 25) -> list[dict]:
    """DuckDuckGo 的 HTML 端点。不用 API，也就没有配额。"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"    ! 请求失败 {type(exc).__name__}", file=sys.stderr)
        return []

    titles = re.findall(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S)
    snips = re.findall(r'result__snippet"[^>]*>(.*?)</a>', raw, re.S)

    def clean(x: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", x)).strip()

    if not titles:
        return []
    out = []
    for i, (href, title) in enumerate(titles):
        real = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            real = urllib.parse.unquote(m.group(1))
        out.append({
            "url": real,
            "title": clean(title),
            "snippet": clean(snips[i]) if i < len(snips) else "",
        })
    return out


def domain_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return "?"


def code_theme(text: str) -> tuple[str, int]:
    """主题编码。返回 (主题, 命中词数)；一个都没命中返回 ('未分类', 0)。"""
    t = text.lower()
    best, hits = "未分类", 0
    for theme, words in THEMES.items():
        n = sum(1 for w in words if w in t)
        if n > hits:
            best, hits = theme, n
    return best, hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=120, help="目标条数")
    ap.add_argument("--sleep", type=float, default=1.5, help="每次查询间隔秒（别把人家打崩）")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    seen_urls: set[str] = set()
    records: list[dict] = []

    backoff = args.sleep
    for i, (bucket, q) in enumerate(QUERIES, 1):
        print(f"[{i:2d}/{len(QUERIES)}] {bucket:10s} {q}")

        # 空结果往往不是"没搜到"，是被限流了。指数退避重试三次——
        # 第一版没有这一层，跑到第二个查询就被封，20 条就收工了。
        results = []
        for attempt in range(3):
            results = ddg(q)
            if results:
                backoff = max(args.sleep, backoff * 0.8)   # 顺利就慢慢降回来
                break
            backoff = min(backoff * 3, 60)
            print(f"    · 空结果，{backoff:.0f}s 后重试（第 {attempt + 1}/3 次）")
            time.sleep(backoff)

        for r in results:
            if r["url"] in seen_urls or not r["snippet"]:
                continue
            seen_urls.add(r["url"])
            dom = domain_of(r["url"])
            text = f"{r['title']} {r['snippet']}"
            theme, hits = code_theme(text)
            records.append({
                "id": f"C{len(records) + 1:03d}",
                "query_bucket": bucket,
                "query": q,
                "title": r["title"],
                "snippet": r["snippet"],
                "url": r["url"],
                "domain": dom,
                "source_quality": "real" if any(dom.endswith(d) for d in REAL_DOMAINS) else "vendor",
                "theme": theme,
                "theme_hits": hits,
                "human_checked": None,   # 你过一遍，改成 true/false
            })
        print(f"           累计 {len(records)} 条")
        if len(records) >= args.target:
            break
        time.sleep(backoff)

    path = OUT / "corpus_raw.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    real = [r for r in records if r["source_quality"] == "real"]
    print()
    print("=" * 62)
    print(f"  采集完成  {len(records)} 条  →  {path}")
    print("=" * 62)
    print(f"  真实用户发言 real   {len(real):3d} 条  ({len(real)/max(1,len(records)):.0%})")
    print(f"  厂商 SEO   vendor  {len(records)-len(real):3d} 条")
    print()
    print("  主题分布（仅 real）：")
    for theme, n in Counter(r["theme"] for r in real).most_common():
        print(f"    {theme:20s} {n:3d}  {'█' * n}")
    print()
    print(f"  未分类 {sum(1 for r in real if r['theme'] == '未分类')} 条——这些要你人工看一眼再归类")
    print()
    print("  ⚠️  下一步必须你自己做：逐条打开链接核对，把 human_checked 改成 true/false。")
    print("      没核对过的语料不能作为用户研究依据——摘要可能断章取义。")


if __name__ == "__main__":
    main()
