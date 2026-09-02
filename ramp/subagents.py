"""三个域子图（HR / IT / 业务）。

**拆子 Agent 的理由是权限隔离，不是任务复杂度。**
IT 域拿不到薪酬知识库，HR 域调不了工单接口——这个边界同时落在三处：

    知识层  knowledge.search(domain=...)   检索不到别的域
    工具层  registry.for_domain(...)        工具表里根本没有
    提示层  prompts.DOMAIN_PROMPTS[...]     职责与边界写明

三层都写死，是因为只靠提示词的隔离会被绕过，只靠工具表的隔离
说不清"为什么不能"。三个域的子图结构完全相同，差异全在注入的作用域上——
这正好证明了拆域不是为了做三套逻辑。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import config, escalate, knowledge, llm, prompts, trace
from .state import RampState
from .tools import registry


# ------------------------------------------------------------------ 节点
def _make_retrieve(domain: str):
    def retrieve(state: RampState) -> dict[str, Any]:
        sid = state["session_id"]
        with trace.span("retrieve", sid, state.get("step", 0), domain=domain) as sp:
            r = knowledge.search(state["question"], domain=domain, top_k=4)
            sp.detail.update(
                best_score=round(r.best_score, 4),
                confident=r.confident,
                degraded=r.degraded,
                threshold=config.CONFIDENCE_THRESHOLD,
                top_level=r.best.source_level if r.best else None,
                top_stale=r.best.is_stale if r.best else None,
            )
        return {
            "hits": [h.to_dict() for h in r.hits],
            "best_score": r.best_score,
            "confident": r.confident,
            "hints": r.hints(),
            "spans": [sp.to_dict()],
        }

    return retrieve


def _make_answer(domain: str):
    def answer(state: RampState) -> dict[str, Any]:
        sid = state["session_id"]
        hits = state.get("hits", [])

        if state.get("kind") == "procedure":
            task = prompts.PROCEDURE_REPLY.format(
                knowledge_block=prompts.knowledge_block(hits),
                question=state["question"],
            )
        else:
            task = prompts.ANSWER_WITH_KNOWLEDGE.format(
                knowledge_block=prompts.knowledge_block(hits),
                question=state["question"],
                mentor_hint=state.get("_mentor_name") or "你的 Mentor",
            )
        bundle = prompts.build(
            domain=domain,
            session_block=state.get("_session_block"),
            task_block=task,
        )
        with trace.span("answer", sid, state.get("step", 0), domain=domain) as sp:
            # procedure 要列全步骤，输出天然更长。给不够预算会触发
            # llm.chat 的加倍重试——那次重试是真金白银，不如一次给够。
            budget = 2200 if state.get("kind") == "procedure" else 1400
            res = llm.chat(bundle.messages, tier="tier2", task="answer", max_tokens=budget)
            trace.record_llm(sp, res)
            sp.detail["cache_ratio"] = round(bundle.cache_ratio, 3)

        return {
            "answer": res.text,
            "citations": prompts.citation_line(hits),
            "route": "answer",
            "tier_used": "tier2",
            "spans": [sp.to_dict()],
            "cost": res.cost,
            "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out,
        }

    return answer


def _cited_in(answer: str, hit: dict) -> bool:
    """这条材料的实质内容有没有出现在回答里。

    判据是**答案文本里的特征词**，不是模型的自我声明。
    取知识条目答案里较长的词块（≥4 字）与来源文档名，命中任意一个即算用了。

    为什么不信模型自己说：实测它会一边写"这个问题知识库没有覆盖"，
    一边在正文里写"按知识库里那条值班排班的口径"——
    **说和做是两件事，能验证的那件才作数。**
    """
    import re as _re

    text = (answer or "")
    src = (hit.get("source_name") or "").strip()
    if src and src.split("》")[0].lstrip("《")[:6] in text:
        return True
    body = (hit.get("answer") or "")
    chunks = [c for c in _re.split(r"[，。；、\s（）()]+", body) if len(c) >= 5]
    return any(c in text for c in chunks[:12])


def _make_advice(domain: str):
    """主观建议类问题的作答节点。

    **它绕开了拒答阈值**——这是产品决策，不是技术妥协：
    0.62 的阈值只对"有唯一正确答案"的问题成立。"第一次周报怎么写"
    检索分低是正常的，因为它本来就不是制度问题。对这类问题拒答，
    等于把用户想要的启发换成一句"我不确定"，是错的产品行为。
    """

    def advice(state: RampState) -> dict[str, Any]:
        sid = state["session_id"]
        # **按相关性过滤，不是按作答阈值。**理由见 config.ADVICE_RELEVANCE_FLOOR：
        # 低分条目喂给模型，它就会去用，然后跑题。宁可一条不给。
        all_hits = state.get("hits", [])
        hits = [h for h in all_hits
                if (h.get("score") or 0) >= config.ADVICE_RELEVANCE_FLOOR]
        dropped = len(all_hits) - len(hits)

        task = prompts.ADVICE_REPLY.format(
            question=state["question"],
            knowledge_block=prompts.knowledge_block(hits) if hits
            else "（没有足够相关的制度内容——这很正常，说明这本来就不是制度问题）",
        )
        bundle = prompts.build(
            domain=domain,
            session_block=state.get("_session_block"),
            task_block=task,
        )
        with trace.span("advice", sid, state.get("step", 0), domain=domain) as sp:
            # task="advice"（不是 "answer"）——关闭思考，见 llm.THINKING_POLICY。
            res = llm.chat(bundle.messages, tier="tier1", task="advice", max_tokens=1600)
            trace.record_llm(sp, res)
            sp.detail.update(best_score=state.get("best_score", 0.0), bypassed_threshold=True,
                             hits_kept=len(hits), hits_dropped=dropped,
                             relevance_floor=config.ADVICE_RELEVANCE_FLOOR)

        # 引用行由代码给，不靠模型自觉。
        # 没有可用材料时**显式说出来**——空字符串会让前端什么都不显示，
        # 用户便无从判断这段话有没有制度依据。
        # 引用行以**模型的实际声明**为准，不以"检索到了几条"为准。
        #
        # 这两个判断会打架：材料过了 0.45 的相关性下限，但模型读完觉得
        # 用不上——于是正文写"知识库没有覆盖"，末尾却挂着两条出处，
        # 同时告诉用户"没依据"和"有依据"。人工复核正是抓到了这一点。
        #
        # 谁说了算：模型。它读了材料，代码没有。
        # 判"用没用上"要看**正文里有没有出现材料的实质内容**，
        # 不能看模型自己怎么说。
        #
        # 试过让模型自己声明，结果它倒向了更安全的那句"知识库没有覆盖"——
        # 一边这么声明，一边在正文里写"按知识库里那条值班排班的口径"。
        # **自我声明不可信：说和做是两件事。**
        # 依据声明由**代码**给，不由模型自报——理由见 prompts.ADVICE_REPLY 第 0 条。
        # 一个真相来源，而且是可验证的那个。
        used = [h for h in hits if _cited_in(res.text, h)]
        if used:
            cites = prompts.citation_line(used)
            banner = f"以下内容部分依据 {used[0].get('source_name', '')}，其余为建议。"
        else:
            cites = f"本回答无制度依据（相关性下限 {config.ADVICE_RELEVANCE_FLOOR}），全部为建议"
            banner = "这个问题知识库没有覆盖，以下全部是建议，不是公司规定。"

        # 模型可能自己也写了一句类似的（旧习惯或自发），去掉避免重复。
        body = res.text
        for junk in ("这个问题知识库没有覆盖，以下全部是建议，不是公司规定。",
                     "这个问题知识库没有覆盖，以下全部是建议。"):
            body = body.replace(junk, "").lstrip()
        answer = banner + (chr(10) * 2) + body

        return {
            "answer": answer,
            "citations": cites,
            "route": "advice",
            "hits": hits,
            "tier_used": "tier1",
            "spans": [sp.to_dict()],
            "cost": res.cost,
            "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out,
        }

    return advice


def _make_escalate(domain: str):
    def do_escalate(state: RampState) -> dict[str, Any]:
        from . import db

        sid = state["session_id"]
        mentor_id = state.get("_mentor_id")
        mentor_name = state.get("_mentor_name") or "你的 Mentor"

        session = db.get_session()
        try:
            card = escalate.open_escalation(
                session,
                session_id=sid,
                employee_id=state["employee_id"],
                domain=domain,
                question=state["question"],
                best_score=state.get("best_score", 0.0),
                hints=state.get("hints", []),
                mentor_id=mentor_id,
            )
            card_id = card.id
        finally:
            session.close()

        task = prompts.ESCALATE_REPLY.format(
            question=state["question"],
            score=state.get("best_score", 0.0),
            threshold=config.CONFIDENCE_THRESHOLD,
            mentor=mentor_name,
            hints="\n".join(f"- {h}" for h in state.get("hints", [])) or "- （无）",
        )
        bundle = prompts.build(domain=domain, task_block=task)
        with trace.span("escalate", sid, state.get("step", 0), domain=domain) as sp:
            res = llm.chat(bundle.messages, tier="tier2", task="escalate", max_tokens=1400)
            trace.record_llm(sp, res)
            sp.detail.update(escalation_id=card_id, best_score=state.get("best_score", 0.0))

        return {
            "answer": res.text,
            "route": "escalate",
            "escalation_id": card_id,
            "tier_used": "tier2",
            "spans": [sp.to_dict()],
            "cost": res.cost,
            "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out,
        }

    return do_escalate


def _make_act(domain: str):
    """Agent 循环：规划 → 工具 → 观察 → 回填。

    这个节点内部是自研的——LangGraph 管的是节点之间怎么走，
    节点内部这个 while 循环仍然要自己写。
    """

    def act(state: RampState) -> dict[str, Any]:
        sid = state["session_id"]
        ctx = {
            "employee_id": state["employee_id"],
            "employee_name": state.get("_employee_name", ""),
            "domain": domain,
        }
        schemas = registry.schemas(domain)
        messages = prompts.build(
            domain=domain,
            session_block=state.get("_session_block"),
            task_block=state["question"],
        ).messages

        calls: list[dict[str, Any]] = []
        obs: list[dict[str, Any]] = []
        spans: list[dict[str, Any]] = []
        cost = tin = tout = 0
        cost = 0.0
        step = 0
        pending: dict[str, Any] | None = None
        text_parts: list[str] = []

        while step < config.MAX_LOOP_STEPS:
            step += 1
            with trace.span(f"act.plan#{step}", sid, step, domain=domain) as sp:
                res = llm.chat_with_tools(
                    messages, schemas,
                    tier="tier1" if step == 1 else "tier2",
                    task="plan",
                )
                trace.record_llm(sp, res)
            spans.append(sp.to_dict())
            cost += res.cost
            tin += res.tokens_in
            tout += res.tokens_out

            # 模型可以**一边输出正文一边调工具**。原来只在"没有工具调用"
            # 那一步取正文，于是伴随工具调用产出的那段正文被整段丢掉——
            # X01「我的社保交了吗」最后只剩一句补充说明，主干答案没了。
            if res.text:
                text_parts.append(res.text)

            if not res.tool_calls:
                break

            messages.append(res.assistant_message)
            for tc in res.tool_calls:
                calls.append({"name": tc["name"], "args": tc["args"], "step": step})
                with trace.span(f"tool:{tc['name']}", sid, step, domain=domain) as tsp:
                    tr = registry.execute(tc["name"], tc["args"], domain=domain, context=ctx)
                    tsp.ok = tr.ok
                    tsp.detail.update(args=tc["args"], needs_confirmation=tr.needs_confirmation)
                    if tr.error:
                        tsp.detail["error"] = tr.error
                spans.append(tsp.to_dict())
                obs.append(tr.to_dict())

                if tr.needs_confirmation:
                    pending = tr.pending_action
                    break

                payload = tr.data if tr.ok else {"error": tr.user_message}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": _json(payload),
                })
            if pending:
                break

        # 挂起等确认时不要把已产出的正文当最终回答——那会让用户
        # 在确认框之外先看到半截答案。
        final_text = "" if pending else (chr(10) * 2).join(p for p in text_parts if p.strip())

        return {
            "step": step,
            "tool_calls": calls,
            "observations": obs,
            "pending_action": pending,
            "answer": final_text,
            "route": "act",
            "tier_used": "tier1+tier2",
            "spans": spans,
            "cost": cost,
            "tokens_in": tin,
            "tokens_out": tout,
        }

    return act


def _json(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(obj)


def _make_confirm(domain: str):
    """H2 执行前确认。**这里是 interrupt() 的落点。**

    图会在这里挂起并把 pending_action 交给前端；用户点确认后
    以 Command(resume={"confirmed": True}) 恢复，从这一行继续往下走。
    """

    def confirm(state: RampState) -> dict[str, Any]:
        pending = state.get("pending_action") or {}
        decision = interrupt({
            "type": "confirm_write",
            "tool": pending.get("tool"),
            "fields": (pending.get("preview") or {}).get("fields", {}),
            "hint": "提交后有 5 分钟撤回窗口",
        })
        confirmed = bool(decision.get("confirmed")) if isinstance(decision, dict) else bool(decision)
        return {"confirmed": confirmed}

    return confirm


def _make_execute(domain: str):
    def execute(state: RampState) -> dict[str, Any]:
        sid = state["session_id"]
        if not state.get("confirmed"):
            return {
                "answer": "好，这次不提交。需要的时候再跟我说，我把草稿留着。",
                "route": "answer",
                "pending_action": None,
            }
        ctx = {
            "employee_id": state["employee_id"],
            "employee_name": state.get("_employee_name", ""),
            "domain": domain,
        }
        with trace.span("execute", sid, state.get("step", 0), domain=domain) as sp:
            tr = registry.commit(state["pending_action"], context=ctx)
            sp.ok = tr.ok
            sp.detail.update(tool=state["pending_action"].get("tool"))
            if tr.ok and isinstance(tr.data, dict):
                sp.detail["ticket_id"] = tr.data.get("ticket_id")

        if not tr.ok:
            return {"answer": tr.user_message or "提交没成功。", "route": "answer",
                    "pending_action": None, "spans": [sp.to_dict()]}

        d = tr.data
        text = (
            f"工单已提交：**{d['ticket_id']}**，审批人 {d['fields']['审批人']}，"
            f"预计 {d['expected_by']} 前有结果。\n\n"
            f"这件事我先挂起了——有结果我会主动通知你，你不用回来问。\n\n"
            f"撤回窗口还剩 {d['revocable_until_minutes']} 分钟。"
        )
        return {
            "answer": text, "route": "answer", "pending_action": None,
            "action_result": d, "spans": [sp.to_dict()],
        }

    return execute


# ------------------------------------------------------------------ 组装
def _route_entry(state: RampState) -> str:
    return "act" if state.get("route") == "act" else "retrieve"


def _route_after_retrieve(state: RampState) -> str:
    # 建议类问题不适用拒答阈值——理由写在 _make_advice 的 docstring 里
    if state.get("kind") == "advice":
        return "advice"
    return "answer" if state.get("confident") else "escalate"


def _route_after_act(state: RampState) -> str:
    return "confirm" if state.get("pending_action") else END


def build_domain_subgraph(domain: str):
    """三个域共用同一套结构，差异只在注入的作用域。"""
    g = StateGraph(RampState)
    g.add_node("retrieve", _make_retrieve(domain))
    g.add_node("answer", _make_answer(domain))
    g.add_node("escalate", _make_escalate(domain))
    g.add_node("advice", _make_advice(domain))
    g.add_node("act", _make_act(domain))
    g.add_node("confirm", _make_confirm(domain))
    g.add_node("execute", _make_execute(domain))

    g.add_conditional_edges(START, _route_entry, {"retrieve": "retrieve", "act": "act"})
    g.add_conditional_edges("retrieve", _route_after_retrieve,
                            {"answer": "answer", "escalate": "escalate", "advice": "advice"})
    g.add_conditional_edges("act", _route_after_act, {"confirm": "confirm", END: END})
    g.add_edge("confirm", "execute")
    g.add_edge("execute", END)
    g.add_edge("answer", END)
    g.add_edge("escalate", END)
    g.add_edge("advice", END)
    return g


_cache: dict[str, Any] = {}


def subgraph(domain: str):
    if domain not in _cache:
        _cache[domain] = build_domain_subgraph(domain).compile()
    return _cache[domain]
