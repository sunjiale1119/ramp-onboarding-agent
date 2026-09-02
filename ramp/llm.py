"""模型调用层：tier1 / tier2 分层路由 + 用量与成本回传。

只做三件事：
  1. 按档位选模型（tier2 快档处理分类与检索作答，tier1 推理档处理规划与长文）
  2. 把每次调用的 token 用量和成本算出来，交给 trace 记录
  3. 失败时抛出结构化异常，让上层决定降级还是升级

刻意不封装成"Agent"——编排是 LangGraph 的事，这里只负责一次模型调用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from . import config


class LLMError(RuntimeError):
    """模型调用失败。上层据此走降级路径，而不是把异常抛给用户。"""


@dataclass
class LLMResult:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit_tokens: int = 0
    """DeepSeek 回传的 prompt_cache_hit_tokens。命中部分便宜 30 倍，
    所以它是成本报告里最该盯的一个数。"""
    duration_ms: int = 0
    cost: float = 0.0
    raw: Any = field(default=None, repr=False)

    def as_trace(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cost": self.cost,
            "duration_ms": self.duration_ms,
        }


def _usage(resp) -> tuple[int, int, int]:
    """从 response.usage 抠出 (输入, 输出, 缓存命中)。

    DeepSeek 在 usage 里额外给 prompt_cache_hit_tokens / prompt_cache_miss_tokens，
    OpenAI 的字段名则是 prompt_tokens_details.cached_tokens——两边都兼容一下。
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0, 0
    tin = getattr(u, "prompt_tokens", 0) or 0
    tout = getattr(u, "completion_tokens", 0) or 0
    hit = getattr(u, "prompt_cache_hit_tokens", None)
    if hit is None:
        details = getattr(u, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", 0) if details else 0
    return tin, tout, int(hit or 0)


_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not config.DEEPSEEK_API_KEY:
            raise LLMError("DEEPSEEK_API_KEY 未配置，请检查 .env")
        raw = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=60.0,
            max_retries=2,
        )
        # 没配 LANGSMITH_API_KEY 时 wrap() 原样返回，这里不需要判断
        from . import tracing

        _client = tracing.wrap(raw)
    return _client


def model_for(tier: str) -> str:
    return config.TIER1 if tier == "tier1" else config.TIER2


# ------------------------------------------------------------------ 思考模式
#
# DeepSeek V4 **默认开启思考模式，effort 默认 high**，而思维链和正文
# 共享同一个 max_tokens。我们踩过一次：escalate 节点给了 500 token，
# 推理吃掉全部 500，finish_reason=length，content 返回空字符串——
# 表现是"升级流程跑通了但用户什么都没看到"。
#
# 所以思考强度是**按任务配的**，不是全局开关：
#   分类   关掉      结构化 JSON，不需要推理，关掉更快更便宜
#   作答   low       有检索结果垫底，不需要深推
#   拒答   low       格式固定，重点是照着三要素写
#   规划   high      多步事务，这里才真需要推理
#
# 另外两条来自官方文档、不遵守就出错的规则：
#   · 思考模式下 temperature / top_p 等参数**静默失效**（不报错也不生效）
#   · 请求带 tools 时，assistant 的 reasoning_content 必须回传，否则 400
THINKING_POLICY: dict[str, str | None] = {
    "classify": None,     # None = 关闭思考
    "answer": "low",
    "escalate": "low",
    "plan": "high",
    # 判分是"照着 rubric 逐条核对 + 摘原文"，是结构化抽取不是推理。
    # 开思考只会跟正文抢 max_tokens——实测在 1200 和 2000 预算下都偶发
    # 截断，一截断就返回不合法 JSON。关掉之后稳定，而且更便宜。
    # advice 一度沿用 answer 的 "low"，代价是两头都吃亏：
    #   可靠性——tier1 + 思考，思维链吃光输出预算，实测约 20% 概率
    #            连续两次生成为空，最后落到兜底文案（A07 就是这么失败的）
    #   成本——advice 占请求量 5%，却占成本 49%
    # 而它的任务是"给 3 条具体建议"，是结构化生成不是深度推理。
    "advice": None,
    "judge": None,
    "summarize": None,
}


def _thinking_kwargs(effort: str | None) -> dict[str, Any]:
    if effort is None:
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"reasoning_effort": effort, "extra_body": {"thinking": {"type": "enabled"}}}


def chat(
    messages: list[dict[str, str]],
    *,
    tier: str = "tier2",
    task: str = "answer",
    temperature: float = 0.2,
    max_tokens: int = 1200,
    json_mode: bool = False,
    _retry_budget: bool = True,
) -> LLMResult:
    """一次模型调用。tier 决定成本档位，task 决定思考强度。

    带一层**截断自愈**：如果思维链吃光了预算导致正文为空，
    自动用双倍预算重来一次。宁可多花一次钱，也不能让用户看到空白。
    """
    model = model_for(tier)
    effort = THINKING_POLICY.get(task, "low")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        **_thinking_kwargs(effort),
    }
    if effort is None:  # 只有关闭思考时 temperature 才真正生效
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    t0 = time.perf_counter()
    try:
        resp = client().chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    dur = int((time.perf_counter() - t0) * 1000)

    tin, tout, hit = _usage(resp)
    choice = resp.choices[0]
    text = (choice.message.content or "").strip()

    # 截断但**非空**——比空更隐蔽：正文从中间断掉，读起来像写完了。
    # 实测 P11「第一次提交代码前要检查什么」结尾停在"关于代码评审"，
    # 而步骤判分器因为四个关键词都在截断点之前，照样给了 4/4 通过。
    #
    # 根因是思考模式吃掉了大部分输出预算：实测 output=1109 token，
    # 正文只有 279 字——剩下的全是思维链。预算一紧，正文就被切。
    truncated = bool(text) and choice.finish_reason == "length"
    if truncated and _retry_budget:
        import logging

        logging.getLogger("ramp.llm").warning(
            "正文被截断(task=%s, 长度=%d, max_tokens=%d)，加倍预算重试",
            task, len(text), max_tokens,
        )
        retry = chat(
            messages, tier=tier, task=task, temperature=temperature,
            max_tokens=max_tokens * 2, json_mode=json_mode, _retry_budget=False,
        )
        # 重试更长才用它——否则保留原文，别越修越短
        if len(retry.text) > len(text):
            return retry

    if not text and _retry_budget:
        # **只要正文为空就重试，不管 finish_reason 是什么。**
        #
        # 第一版只在 finish_reason == "length" 时重试，漏掉了更隐蔽的一种：
        # 思考跑完了、finish_reason 是 "stop"、但 content 是空字符串。
        # 那一次用户看到的是一片空白——比答错更糟，而且日志里毫无痕迹。
        # 全量评测里 60 条中有 1 条这样，偶发到本地复现两次都复现不出来。
        import logging

        logging.getLogger("ramp.llm").warning(
            "正文为空(task=%s, finish=%s, max_tokens=%d)，加倍预算重试",
            task, choice.finish_reason, max_tokens,
        )
        retry = chat(
            messages, tier=tier, task=task, temperature=temperature,
            max_tokens=max_tokens * 2, json_mode=json_mode, _retry_budget=False,
        )
        if retry.text:
            return retry
        # 重试还是空——最后一道兜底。**用户永远不该看到空白。**
        logging.getLogger("ramp.llm").error("重试后仍为空(task=%s)，返回兜底文案", task)
        retry.text = (
            "抱歉，这次没能生成回答（模型侧异常）。你可以换个说法再问一次，"
            "或者直接找 Mentor 确认——这条我已经记进日志了。"
        )
        return retry

    return LLMResult(
        text=text,
        model=model,
        tokens_in=tin,
        tokens_out=tout,
        cache_hit_tokens=hit,
        duration_ms=dur,
        cost=config.cost_of(model, tin, tout, cache_hit_tokens=hit),
        raw=resp,
    )


def chat_json(
    messages: list[dict[str, str]],
    *,
    tier: str = "tier2",
    fallback: dict[str, Any] | None = None,
    **kw: Any,
) -> tuple[dict[str, Any], LLMResult]:
    """要求模型返回 JSON。解析失败时回落到 fallback，而不是让整条链崩掉。

    这一层的存在本身就是产品决策：模型偶尔会返回不合法 JSON，
    产品不能因此给用户看到一个 500。
    """
    kw.setdefault("task", "classify")
    res = chat(messages, tier=tier, json_mode=True, **kw)
    try:
        return json.loads(res.text), res
    except json.JSONDecodeError:
        # 退一步：尝试从文本里抠出第一个 JSON 对象
        s, e = res.text.find("{"), res.text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(res.text[s : e + 1]), res
            except json.JSONDecodeError:
                pass
        if fallback is None:
            raise LLMError(f"模型未返回合法 JSON: {res.text[:200]}")
        return dict(fallback), res


@dataclass
class ToolCallResult(LLMResult):
    """带工具调用的返回。tool_calls 为空表示模型认为可以直接作答了。"""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_message: dict[str, Any] = field(default_factory=dict)


def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    tier: str = "tier2",
    task: str = "plan",
    max_tokens: int = 2000,
) -> ToolCallResult:
    """一轮 think-act：模型要么给出工具调用，要么给出最终回答。

    注意参数里的 `tools` 是**按域过滤过的**——模型看不到不属于本域的工具，
    所以它不可能"想调却调不到"，只会当那个能力不存在。
    """
    model = model_for(tier)
    t0 = time.perf_counter()
    try:
        resp = client().chat.completions.create(
            model=model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
            max_tokens=max_tokens,
            **_thinking_kwargs(THINKING_POLICY.get(task, "high")),
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    dur = int((time.perf_counter() - t0) * 1000)

    msg = resp.choices[0].message
    tin, tout, hit = _usage(resp)

    calls: list[dict[str, Any]] = []
    raw_calls = getattr(msg, "tool_calls", None) or []
    for tc in raw_calls:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": tc.id, "name": tc.function.name, "args": args})

    assistant_message: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    # 官方规则：请求携带 tools 时，assistant 的 reasoning_content 必须在
    # 后续轮次回传，否则 API 返回 400——即使这一轮没有实际调用工具。
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning:
        assistant_message["reasoning_content"] = reasoning
    if raw_calls:
        assistant_message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in raw_calls
        ]

    return ToolCallResult(
        text=(msg.content or "").strip(),
        model=model,
        tokens_in=tin,
        tokens_out=tout,
        cache_hit_tokens=hit,
        duration_ms=dur,
        cost=config.cost_of(model, tin, tout, cache_hit_tokens=hit),
        raw=resp,
        tool_calls=calls,
        assistant_message=assistant_message,
    )


def health() -> tuple[bool, str]:
    try:
        # 健康检查只关心"API 通不通"，不关心回答内容。
        # _retry_budget=False 很关键：32 token 装不下模型对 "ping" 的回复，
        # 走通用截断重试会**白花第二次调用**，还在日志里刷一条误导性告警——
        # 明明服务是好的，日志却在喊"正文被截断"。
        r = chat([{"role": "user", "content": "ping"}], tier="tier2",
                 task="summarize", max_tokens=32, _retry_budget=False)
        return True, f"{r.model} ok ({r.duration_ms}ms)"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
