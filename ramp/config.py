"""全局配置：模型档位、阈值、成本单价、数据库连接。

所有可调的产品参数集中在这里，方便在评测里做消融实验
（比如把 CONFIDENCE_THRESHOLD 从 0.62 调到 0.55，看拒答正确率怎么变）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
SEED_DIR = ROOT / "seed"

# 优先级：项目 .env > 仓库根 .env > OS 环境变量。
#
# override=True 是必须的：机器上可能残留同名的过期变量（我们就被一个
# 早已失效的 DEEPSEEK_API_KEY 坑过一次——它静默盖住了 .env 里的好 key，
# 表现成 401 而不是"读不到配置"）。项目里的 .env 才是这个项目的事实来源。
load_dotenv(REPO_ROOT / ".env", override=True)
load_dotenv(ROOT / ".env", override=True)


# ---------------------------------------------------------------- 模型
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

TIER1 = os.getenv("RAMP_TIER1_MODEL", "deepseek-v4-pro")    # 推理档：规划、长文生成
TIER2 = os.getenv("RAMP_TIER2_MODEL", "deepseek-v4-flash")  # 快档：分类、检索作答

# 单价（元 / 百万 token），来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
# 核对日期 2026-08-27。DeepSeek 保留调价权利，跑成本报告前请复核。
#
# 两个结构性事实，直接决定了这个产品的成本设计：
#   1. **缓存命中比未命中便宜 30 倍**（flash 高峰 0.10 vs 3.0）
#      → 所以 prompts.py 把稳定前缀放最前面。那不是洁癖，是省钱。
#   2. **空闲时段是高峰的一半**
#      → 所以 proactive.py 的节点推送安排在空闲时段跑。
PRICING: dict[str, dict[str, float]] = {
    # 高峰价；空闲价 = 高峰价 / 2
    TIER1: {"cache_hit_in": 0.30, "cache_miss_in": 9.00, "out": 27.00},
    TIER2: {"cache_hit_in": 0.10, "cache_miss_in": 3.00, "out": 9.00},
    "deepseek-v4-flash-vision-exp": {"cache_hit_in": 0.10, "cache_miss_in": 3.00, "out": 9.00},
}
PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICING_CHECKED_ON = "2026-08-27"
PRICING_IS_PLACEHOLDER = False


def is_peak(when=None) -> bool:
    """高峰时段：北京时间周一至周五 9:00–12:00、14:00–18:00。"""
    from datetime import datetime, timedelta, timezone

    now = when or datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:  # 周六日
        return False
    h = now.hour
    return 9 <= h < 12 or 14 <= h < 18


def cost_of(
    model: str,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_hit_tokens: int = 0,
    peak: bool | None = None,
) -> float:
    """一次调用的成本（元）。

    cache_hit_tokens 由 DeepSeek 在 usage 里回传（prompt_cache_hit_tokens）。
    不传就按全部未命中算——这是保守估计，宁可高估成本也别低估。
    """
    p = PRICING.get(model)
    if not p:
        return 0.0
    peak = is_peak() if peak is None else peak
    factor = 1.0 if peak else 0.5

    hit = max(0, min(cache_hit_tokens, tokens_in))
    miss = max(0, tokens_in - hit)
    yuan = (
        hit * p["cache_hit_in"] + miss * p["cache_miss_in"] + tokens_out * p["out"]
    ) / 1_000_000
    return yuan * factor


def price_table() -> list[dict[str, object]]:
    """给成本报告用的单价表。"""
    rows = []
    for model, p in PRICING.items():
        for label, factor in (("高峰", 1.0), ("空闲", 0.5)):
            rows.append({
                "model": model,
                "时段": label,
                "输入·缓存命中": p["cache_hit_in"] * factor,
                "输入·未命中": p["cache_miss_in"] * factor,
                "输出": p["out"] * factor,
            })
    return rows


# ---------------------------------------------------------------- 产品阈值
CONFIDENCE_THRESHOLD = float(os.getenv("RAMP_CONFIDENCE_THRESHOLD", "0.62"))
"""检索置信度低于此值即触发升级，不允许模型硬答。"""

ADVICE_RELEVANCE_FLOOR = float(os.getenv("RAMP_ADVICE_FLOOR", "0.45"))
"""建议类回答的材料相关性下限。

**和 CONFIDENCE_THRESHOLD 是两件事**：
    0.62  能不能据此作答（否则转人工）
    0.45  这条材料值不值得给模型看

advice 路由刻意绕开了 0.62——但绕开阈值不等于无视相关性。
实测 A07「1:1 该聊什么」检索到的 4 条最高分才 0.501，
最低 0.279（"有哪些环境"），却被一起塞给了模型，
于是回答里冒出年假额度、学习基金、健身房补贴——**跑题**。

模型看到有材料给它就会去用。不给，它才不会用。

"""

MAX_LOOP_STEPS = int(os.getenv("RAMP_MAX_LOOP_STEPS", "6"))
"""Agent 循环最大步数，防止无限工具调用。"""

CONTEXT_TOKEN_BUDGET = int(os.getenv("RAMP_CONTEXT_BUDGET", "6000"))
"""超过此预算触发上下文压缩。"""

KEEP_RECENT_TURNS = int(os.getenv("RAMP_KEEP_RECENT_TURNS", "4"))
"""压缩时无条件保留的最近轮数。"""

WEEKLY_PUSH_BUDGET = int(os.getenv("RAMP_WEEKLY_PUSH_BUDGET", "2"))
"""每周主动打扰上限——打扰预算是产品决策，不是技术限制。"""

EPISODIC_RETENTION_DAYS = int(os.getenv("RAMP_EPISODIC_DAYS", "30"))
"""情景记忆保留天数，到期降解为主题聚合。"""

HYBRID_ALPHA = float(os.getenv("RAMP_HYBRID_ALPHA", "0.5"))
"""混合检索里向量分的权重，(1 - alpha) 给 BM25。"""

# 知识来源分级的置信度上限：L3 口述永远不足以单独作答
SOURCE_LEVEL_CAP = {"L1": 1.00, "L2": 0.85, "L3": 0.45}

# 过期知识的降权系数
SEMANTIC_FLOOR = float(os.getenv("RAMP_SEMANTIC_FLOOR", "0.55"))


# ------------------------------------------------------------------ 外部系统
# off / builtin / live —— 见 ramp/external.py 的模块文档。
#
# 默认 builtin，但 builtin **不等于"一定查得到"**：它是数据驱动的，
# 没配过的字段照样走"未接入"分支。也就是说全新部署的行为和 off 一样，
# 管理员在后台录一项就解锁一项。
#
# off 这一档必须保留：「外部系统没接时诚实说查不到、绝不编值」是一项
# 要被测试的能力，评测里 cross_system 那 12 题考的就是它。
# 加了内置实现就把这条路径丢掉，是亏的。
EXTERNAL_MODE = os.getenv("RAMP_EXTERNAL_MODE", "builtin").strip().lower()
"""语义相关性**绝对下限**。低于它的条目一律不能作答，无论混合分多高。

## 为什么必须有这道闸

BM25 分数是**归一化**的：`bm_norm = bm_raw / bm_max`，除以本批候选的最大值。
这意味着哪怕所有候选都毫不相关，最像的那个照样得 1.0 ——
而它在混合分里直接贡献 0.5，足以把一条完全无关的知识推过 0.62 的作答阈值。

实测三个知识库里根本没有的问题：

    公司的宠物友好日是哪天？  → 0.6327  命中「试用期多久？」        判定：作答
    团建预算是多少？          → 0.7242  命中「报销标准是多少？」    判定：作答
    董事长叫什么名字？        → 0.6379  命中「转正需要交什么材料？」判定：作答

三条全部过线，而 Agent 会拿着「试用期」的答案去回答「宠物友好日」——
**这就是幻觉的来源，而且是检索层造的，不是模型编的。**

根因是把**相对排名**当成了**绝对相关性**：归一化 BM25 只能回答
"这批候选里哪个最像"，回答不了"它到底像不像"。

余弦相似度没有这个问题 —— 它有界 [0,1] 且有绝对含义。
所以混合分继续用于**排序**（BM25 对专有名词的敏感度有价值），
但**能不能作答**要额外过一道语义下限。

对照：「食堂几点关门？」一个词都没匹配上，BM25 原始分为 0，
归一化后还是 0，所以它本来就不会误判 —— **只有"部分词匹配"的问题会踩这个坑，
而那恰恰是最危险的一类：看起来有点像，实际完全不相关。**

## 0.55 是标定出来的，而且标了三次才对

**第一次**：拍了 0.45。当时手上只有 4 条负样本，最高才 0.4483，看着刚好够。

**第二次**：负样本扩到 10 条，最高变成 0.6915，0.45 立刻漏 2 个。
改用自己挑的 15 条正样本重标，得到 0.72，测试全绿。

**第三次**：拿这个 0.72 跑黄金集，**10 条真命中被改判成转人工**。
原因是我手挑的正样本最低余弦 0.7452，而黄金集里有大量"问具体数字"的题
（餐补多少钱、补卡几次、住宿上限多少），它们和知识条目的**整句语义**
相似度天然偏低（0.59–0.69），但答案确实在条目里。

> **用自己挑的样本标定，标出来的是自己样本的分布。**
> 正样本必须来自真实评测集。

## 最终标定（负样本 12 条 · 正样本 32 条取自黄金集）

    混合分   负 [0.199, 0.846]   正 [0.709, 0.969]   间隔 -0.137  重叠
    余弦     负 [0.256, 0.692]   正 [0.597, 0.938]   间隔 -0.094  重叠

**两个判据都重叠 —— 不存在能同时做到 0 漏 0 误伤的阈值。**
这本身是个重要结论：光调阈值解决不了，要真正解决得上 rerank 或 query 改写。

在做不到完美的前提下取帕累托最优：

    原实现（只看混合分）        漏 8/12 假阳性   误伤 0/32
    双条件 + 余弦 0.55         漏 1/12         误伤 0/32   ← 当前
    双条件 + 余弦 0.70         漏 0/12         误伤 7/32

选 0.55 是因为**误伤的代价更高**：漏检只是偶尔答错一个边缘问题，
误伤是让 22% 本来答得了的问题变成转人工 —— 产品能力直接掉一大块。

⚠️ 仍会漏的那一条是「公司有健身房吗？」（余弦 0.6915），
它确实和福利类条目语义接近，属于边界情况。**这个局限是已知的，不是没发现。**

重标脚本：`scripts/calibrate_gate.py`
"""

STALE_PENALTY = float(os.getenv("RAMP_STALE_PENALTY", "0.5"))

DEGRADED_PENALTY = float(os.getenv("RAMP_DEGRADED_PENALTY", "0.75"))
"""embedding 不可用、检索降级为纯 BM25 时的折减系数。

**降级必须同时降低自信度，否则方向是反的。**
第一版只是把权重全给 BM25，结果一次完美词法命中拿到 1.000——
比正常混合检索的 0.812 还高。用一半的信号显得更自信，这是错的。

0.75 的含义：完美词法命中在降级时最高只能到 0.75×level_cap。
L1 条目仍能越过 0.62 作答，L2（cap 0.85）降到 0.64 勉强够，
弱一点的匹配则掉到阈值下走升级——**这正是我们想要的：
信号不全的时候，更容易转人工，而不是更容易硬答。**"""


# ---------------------------------------------------------------- 数据库
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("MYSQL_DB", "ramp")


def mysql_url(*, with_db: bool = True) -> str:
    from urllib.parse import quote_plus

    auth = f"{MYSQL_USER}:{quote_plus(MYSQL_PASSWORD)}@" if MYSQL_PASSWORD else f"{MYSQL_USER}@"
    tail = f"/{MYSQL_DB}?charset=utf8mb4" if with_db else "/?charset=utf8mb4"
    return f"mysql+pymysql://{auth}{MYSQL_HOST}:{MYSQL_PORT}{tail}"


# LangGraph checkpointer 后端：mysql（自研）或 sqlite（官方，回退用）。
#
# 官方只有 MemorySaver / SqliteSaver / PostgresSaver，没有 MySQL。
# 用 SqliteSaver 能跑，代价是**两个库**——会话文本在 MySQL、
# 暂停中的图在 SQLite，备份要备两处，排查要对两处的时间线。
# ------------------------------------------------------------------ 演示模式
DEMO_MODE = os.getenv("RAMP_DEMO_MODE", "0").lower() in ("1", "true", "yes")
"""公开演示部署。开启后有两个行为，都是**显式的**：

界面顶部挂一条横幅，说明**知识库是虚构的，除此之外不是**。

知识库来自虚构公司「云启科技」，这是刻意保留的例外：只有自建才能精确控制
L1/L2/L3 分级与有效期，用来验证分级降权是否真的生效 ——
真实制度文件不会主动给你一条"已过期的 L3 传言"来做测试。

其余数据都不虚构：成员和入职信息由管理员录入；外部系统（HR 档案、
组织架构、IT 权限、工单）未接入时，工具会明确说查不到，不会编。

> 这条横幅原来写的是"知识库、员工档案、社保与工单均为虚构"——
> 那时候系统里确实有一整套编出来的人和记录，包括一个叫「李敏」的审批人，
> Agent 会说出他的名字而后台查无此人。那些数据已经清掉了。
"""

CHECKPOINT_BACKEND = os.getenv("RAMP_CHECKPOINT_BACKEND", "mysql").lower()
CHECKPOINT_DB = str(ROOT / "ramp_checkpoints.sqlite")  # 仅 sqlite 后端使用

# ---------------------------------------------------------------- 域
DOMAINS = ("hr", "it", "biz")
DOMAIN_LABEL = {"hr": "HR 域", "it": "IT 域", "biz": "业务域"}
