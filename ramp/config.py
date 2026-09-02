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
CHECKPOINT_BACKEND = os.getenv("RAMP_CHECKPOINT_BACKEND", "mysql").lower()
CHECKPOINT_DB = str(ROOT / "ramp_checkpoints.sqlite")  # 仅 sqlite 后端使用

# ---------------------------------------------------------------- 域
DOMAINS = ("hr", "it", "biz")
DOMAIN_LABEL = {"hr": "HR 域", "it": "IT 域", "biz": "业务域"}
