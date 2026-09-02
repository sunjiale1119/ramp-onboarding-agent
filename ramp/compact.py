"""上下文预算与压缩。

30 天的会话不可能全塞进上下文——**这是真实需求，不是补丁。**

压缩策略本身就是产品决策，因为"丢什么"直接决定了体验：
    保留：身份信息、已确认的事实、**未完成的事项**、已做出的决定
    丢弃：寒暄、重复确认、已经解决且不影响后续的细节

"未完成的事项"必须保留是这里最关键的一条。新人最怕的就是
"我上周问过的那个工单后来怎么样了"——如果压缩把它丢了，
产品在用户心里就成了一个健忘的助手，信任直接崩掉。
"""

from __future__ import annotations

from typing import Any

from . import config, llm, prompts, trace

# 粗略 token 估算：中文约 1.5 字/token，英文约 4 字符/token。
# 用估算而不是真 tokenizer，是因为压缩判断只需要量级正确，
# 为此多引一个 tiktoken 依赖不划算。
_CJK_PER_TOKEN = 1.5
_ASCII_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    other = len(text) - cjk
    return int(cjk / _CJK_PER_TOKEN + other / _ASCII_PER_TOKEN) + 1


def messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(str(m.get("content", ""))) for m in messages)


def should_compact(history: list[dict[str, Any]]) -> tuple[bool, int]:
    used = messages_tokens(history)
    return used > config.CONTEXT_TOKEN_BUDGET, used


def compact(
    history: list[dict[str, str]],
    *,
    session_id: str = "",
    keep_recent: int | None = None,
    max_chars: int = 400,
) -> dict[str, Any]:
    """把中段对话压成摘要，保留最近 N 轮原文。

    返回 {"history": 新历史, "summary": 摘要, "saved_tokens": 省下的量}。
    未触发阈值时原样返回，不产生任何模型调用。
    """
    keep = config.KEEP_RECENT_TURNS if keep_recent is None else keep_recent
    need, used = should_compact(history)
    if not need or len(history) <= keep + 2:
        return {"history": history, "summary": "", "saved_tokens": 0, "compacted": False}

    head, tail = history[:-keep], history[-keep:]
    transcript = "\n".join(
        f"{m.get('role', '?')}: {str(m.get('content', ''))[:400]}" for m in head
    )

    task = prompts.SUMMARIZE_HISTORY.format(max_chars=max_chars, history=transcript)
    with trace.span("compact", session_id or "-", 0) as sp:
        res = llm.chat(
            [{"role": "user", "content": task}],
            tier="tier2",
            task="summarize",   # 关闭思考：摘要不需要推理
            max_tokens=800,
        )
        trace.record_llm(sp, res)
        sp.detail.update(dropped_messages=len(head), before_tokens=used)

    summary = res.text.strip()
    new_history: list[dict[str, str]] = [
        {"role": "system", "content": f"## 此前对话摘要\n{summary}"}
    ] + tail
    after = messages_tokens(new_history)

    sp.detail["after_tokens"] = after
    return {
        "history": new_history,
        "summary": summary,
        "saved_tokens": max(0, used - after),
        "compacted": True,
        "span": sp.to_dict(),
        "cost": res.cost,
    }


def budget_report(history: list[dict[str, Any]]) -> dict[str, Any]:
    """给运营看的上下文预算占用。"""
    used = messages_tokens(history)
    return {
        "used_tokens": used,
        "budget": config.CONTEXT_TOKEN_BUDGET,
        "ratio": round(used / config.CONTEXT_TOKEN_BUDGET, 3) if config.CONTEXT_TOKEN_BUDGET else 0,
        "will_compact": used > config.CONTEXT_TOKEN_BUDGET,
        "keep_recent_turns": config.KEEP_RECENT_TURNS,
    }
