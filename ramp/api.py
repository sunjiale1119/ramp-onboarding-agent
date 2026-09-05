"""FastAPI 层：给四视角原型前端供数。

路由按**视角**分组，而不是按资源分组——因为这个产品的核心命题就是
"同一件事，四个角色看到的不同"。可见性过滤统一走 memory.for_viewer()，
不允许任何路由绕过它自己拼数据。

    /api/newbie/*   新人：问答、确认、记忆、时间线
    /api/mentor/*   Mentor：升级卡片、卡点信号（**拿不到提问原文**）
    /api/hr/*       HR：北极星指标、飞轮曲线、部门级聚合
    /api/ops/*      运营：会话 trace、评测报告、红线状态
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, RedirectResponse
from fastapi import FastAPI, HTTPException, Cookie, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import auth, config, db, demo, escalate, external, knowledge, memory, proactive, runtime, trace

app = FastAPI(title="爬坡 Ramp API", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    """启动自检：只报告，不改数据。

    这里以前会在演示模式下**自动改演示员工的入职日期**，把他们滚回
    30 天窗口内 —— 因为种子里的日期写死，跑一阵子新人就"毕业"了。
    现在入职日期是管理员真填的业务事实，程序不该动它。
    """
    import logging

    log = logging.getLogger("ramp")
    try:
        n = len(auth.list_users())
        log.info("[启动] 账号 %d 个", n)
        if n <= 1:
            log.warning("[启动] 只有管理员账号 —— 登录后在管理后台激活新注册的人")
    except Exception as exc:  # noqa: BLE001
        log.warning("[启动] 账号自检失败：%s", exc)


# ------------------------------------------------------------------ 模型
class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # 没有默认值：原来写死 "e_linxy"（一个已被删除的演示员工），
    # 客户端漏传时会静默问到别人头上，而不是报错。
    employee_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = None


class ConfirmIn(BaseModel):
    session_id: str
    confirmed: bool


class AnswerIn(BaseModel):
    escalation_id: int
    answer: str = Field(min_length=1, max_length=4000)
    # confirmed_by 不再从请求体取 —— 由服务端从登录态推导。
    # 客户端传什么就信什么，等于任何 Mentor 都能把知识署成别人的名字；
    # 而原来的默认值 "陈昊" 更糟：系统里没有这个人，
    # 却会成为知识条目上那个"由谁确认"的落款。
    sink: bool = True


# ------------------------------------------------------------------ 通用
COOKIE = "ramp_session"


def current(ramp_session: str | None = Cookie(default=None)) -> auth.Principal:
    """FastAPI 依赖：解析 cookie，拿不到就 401。

    **每个受保护端点都过这里。** 前端把按钮藏起来只是体验，
    真正的边界是服务端不返回——这两件事经常被混为一谈。
    """
    p = auth.resolve(ramp_session)
    if p is None:
        raise HTTPException(401, "未登录或会话已过期")
    return p


def require(view: str):
    """要求登录者能看某个视角。

    权限的唯一定义在 auth.ROLE_VIEWS，这里只是照着查。
    """
    def dep(p: auth.Principal = Depends(current)) -> auth.Principal:
        if not p.can_view(view):
            raise HTTPException(
                403,
                f"{auth.ROLE_LABEL.get(p.role, p.role)} 看不到「{view}」视角——"
                "这不是权限配置漏了，是产品设计。",
            )
        return p
    return dep


def own_employee(p: auth.Principal, employee_id: str) -> None:
    """新人只能看自己的数据。

    没有这一条，登录就只是个门禁：进来之后随便改 URL 里的 employee_id
    就能看别人的记忆。**越权访问几乎都是这么来的。**
    """
    if p.role == "newbie" and p.employee_id != employee_id:
        raise HTTPException(403, "只能查看自己的数据")


WEB_DIR = Path(__file__).resolve().parent / "web"


def _page(name: str):
    f = WEB_DIR / (name + ".html")
    if not f.exists():
        raise HTTPException(404, "缺少页面 " + name + ".html")
    return FileResponse(f, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/", include_in_schema=False)
def index(ramp_session: str | None = Cookie(default=None)):
    """按角色分流。

    **不同角色进不同页面，不是同一个页面藏标签。**
    第一版做成单页 + 按角色隐藏 tab，本质还是"一个界面给所有人"——
    而且没登录时整个骨架都能看到。真实系统里，新人根本不该知道
    管理后台长什么样。
    """
    p = auth.resolve(ramp_session)
    return RedirectResponse(p.home if p else "/login", status_code=302)


@app.get("/login", include_in_schema=False)
def page_login(ramp_session: str | None = Cookie(default=None)):
    p = auth.resolve(ramp_session)
    if p:
        return RedirectResponse(p.home, status_code=302)
    return _page("login")


@app.get("/register", include_in_schema=False)
def page_register(ramp_session: str | None = Cookie(default=None)):
    """注册就在登录页的第二个标签里，但**这个网址得能直接打开**——
    别人把注册链接发给新同事时不会去发 `/login` 然后叮嘱"点右边那个标签"。"""
    p = auth.resolve(ramp_session)
    if p:
        return RedirectResponse(p.home, status_code=302)
    return _page("login")


def _guarded_page(name: str, view: str):
    def handler(ramp_session: str | None = Cookie(default=None)):
        p = auth.resolve(ramp_session)
        if p is None:
            return RedirectResponse("/login", status_code=302)
        if not p.can_view(view):
            return RedirectResponse(p.home, status_code=302)
        return _page(name)
    return handler


for _n, _v in (("newbie", "newbie"), ("mentor", "mentor"), ("hr", "hr"),
               ("ops", "ops"), ("admin", "admin")):
    app.get("/" + _n, include_in_schema=False)(_guarded_page(_n, _v))


# 静态资源不缓存。改完样式刷新看不到效果，会让人以为改错了地方去动源码——
# 那是开发时最费时间的一类假问题。生产环境该换成带 hash 的文件名。
NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/_shared.css", include_in_schema=False)
def shared_css():
    return FileResponse(WEB_DIR / "_shared.css",
                        media_type="text/css; charset=utf-8", headers=NO_CACHE)


@app.get("/_shared.js", include_in_schema=False)
def shared_js():
    return FileResponse(WEB_DIR / "_shared.js",
                        media_type="application/javascript; charset=utf-8",
                        headers=NO_CACHE)


@app.post("/api/login", include_in_schema=True)
def do_login(body: dict, response: Response) -> dict[str, Any]:
    token = auth.login(str(body.get("username", "")), str(body.get("password", "")))
    if token is None:
        # 统一文案：不告诉对方是"没这个用户"还是"密码错"
        raise HTTPException(401, "用户名或密码不正确")
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=auth.SESSION_HOURS * 3600)
    return {"ok": True, "me": auth.resolve(token).to_dict()}


@app.post("/api/register")
def do_register(body: dict) -> dict[str, Any]:
    ok, msg = auth.register(str(body.get("username", "")),
                            str(body.get("password", "")),
                            str(body.get("display_name", "")))
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


@app.post("/api/logout")
def do_logout(response: Response, ramp_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    ok = auth.logout(ramp_session)
    response.delete_cookie(COOKIE)
    return {"ok": ok}


@app.get("/api/me")
def whoami(p: auth.Principal = Depends(current)) -> dict[str, Any]:
    return {**p.to_dict(), "demo_mode": config.DEMO_MODE}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return runtime.health()


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """把产品阈值暴露给前端展示——这些数字本身就是产品决策，值得可见。"""
    return {
        "confidence_threshold": config.CONFIDENCE_THRESHOLD,
        "hybrid_alpha": config.HYBRID_ALPHA,
        "weekly_push_budget": config.WEEKLY_PUSH_BUDGET,
        "max_loop_steps": config.MAX_LOOP_STEPS,
        "episodic_retention_days": config.EPISODIC_RETENTION_DAYS,
        "tiers": {"tier1": config.TIER1, "tier2": config.TIER2},
        "pricing_source": config.PRICING_SOURCE,
        "pricing_checked_on": config.PRICING_CHECKED_ON,
        "is_peak_now": config.is_peak(),
        "domains": list(config.DOMAINS),
    }


# ------------------------------------------------------------------ 新人
@app.post("/api/newbie/ask")
def newbie_ask(body: AskIn, p: auth.Principal = Depends(require("newbie"))) -> dict[str, Any]:
    own_employee(p, body.employee_id)
    try:
        return runtime.ask(
            body.question, employee_id=body.employee_id, session_id=body.session_id
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/newbie/confirm")
def newbie_confirm(body: ConfirmIn, p: auth.Principal = Depends(require("newbie"))) -> dict[str, Any]:
    """H2 确认框上点了确认 / 取消——从 checkpoint 恢复。"""
    return runtime.resume(body.session_id, confirmed=body.confirmed)


@app.get("/api/newbie/{employee_id}/memory")
def newbie_memory(employee_id: str, p: auth.Principal = Depends(require("newbie"))) -> dict[str, Any]:
    own_employee(p, employee_id)
    """本人视角的记忆。**只有这个端点返回提问原文。**"""
    session = db.get_session()
    try:
        return memory.for_viewer(session, employee_id, "self")
    finally:
        session.close()


@app.delete("/api/newbie/{employee_id}/memory/{memory_id}")
def newbie_forget(employee_id: str, memory_id: int, p: auth.Principal = Depends(require("newbie"))) -> dict[str, Any]:
    own_employee(p, employee_id)
    """记忆可读可删是信任设计的一部分，不是附加功能。"""
    session = db.get_session()
    try:
        ok = memory.forget(session, memory_id, employee_id)
        if not ok:
            raise HTTPException(404, "记忆不存在或不属于你")
        return {"ok": True, "deleted": memory_id}
    finally:
        session.close()


@app.get("/api/newbie/{employee_id}/profile")
def newbie_profile(employee_id: str,
                   p: auth.Principal = Depends(require("newbie"))) -> dict[str, Any]:
    """新人自己的档案。

    拆页之前这份数据是从 `/mentor/{id}/mentees` 里挑出来的——
    新人页复用了 mentor 的接口。加上鉴权后那条路直接 403 了，
    **这正说明当时的前端在越权读数据**，只是没人发现。
    """
    own_employee(p, employee_id)
    session = db.get_session()
    try:
        e = session.get(db.Employee, employee_id)
        if e is None:
            raise HTTPException(404, "没有这位员工的档案")
        tl = proactive.preview_timeline(e.id)
        return {
            "id": e.id, "name": e.name, "team": e.team, "role": e.role,
            "domain": e.domain, "mentor_name": e.mentor_name,
            "day": e.day_index(),
            "nodes_done": sum(1 for n in tl if n["state"] == "done"),
            "nodes_total": len(tl),
        }
    finally:
        session.close()


@app.get("/api/newbie/{employee_id}/timeline")
def newbie_timeline(employee_id: str, p: auth.Principal = Depends(require("newbie"))) -> list[dict[str, Any]]:
    own_employee(p, employee_id)
    return proactive.preview_timeline(employee_id)


@app.get("/api/newbie/{employee_id}/push")
def newbie_push(employee_id: str, p: auth.Principal = Depends(require("newbie"))) -> dict[str, Any] | None:
    own_employee(p, employee_id)
    session = db.get_session()
    try:
        return proactive.run_for(session, employee_id)
    finally:
        session.close()


# ------------------------------------------------------------------ Mentor
@app.get("/api/mentor/{mentor_id}/escalations")
def mentor_escalations(mentor_id: str, p: auth.Principal = Depends(require("mentor"))) -> list[dict[str, Any]]:
    session = db.get_session()
    try:
        return escalate.pending_for_mentor(session, mentor_id)
    finally:
        session.close()


@app.post("/api/mentor/answer")
def mentor_answer(body: AnswerIn, p: auth.Principal = Depends(require("mentor"))) -> dict[str, Any]:
    """回答 → 沉淀为 L2 → 重建索引。飞轮的 03→04→05 步。"""
    session = db.get_session()
    try:
        r = escalate.answer_and_sink(
            session, body.escalation_id,
            answer=body.answer, confirmed_by=p.display_name, sink=body.sink,
        )
        if not r.get("ok"):
            raise HTTPException(400, r.get("error", "处理失败"))
        return r
    finally:
        session.close()


@app.get("/api/mentor/view/{employee_id}")
def mentor_view(employee_id: str, p: auth.Principal = Depends(require("mentor"))) -> dict[str, Any]:
    own_employee(p, employee_id)
    """Mentor 看到的新人状态。

    注意返回体里 raw_questions 恒为 null——**这不是权限不足，是产品设计**，
    理由写在 memory.for_viewer 的注释里。
    """
    session = db.get_session()
    try:
        return memory.for_viewer(session, employee_id, "mentor")
    finally:
        session.close()


@app.get("/api/mentor/kb-count")
def mentor_kb_count(p: auth.Principal = Depends(require("mentor"))) -> dict[str, Any]:
    """知识库总量。

    拆页之前 mentor 页是调 `/hr/dashboard` 拿这个数的——一个 mentor
    去读 HR 的整批聚合看板。加鉴权后它 403 了，**这是第二处被前端复用
    掩盖掉的越权**（第一处是新人页读 mentor 的 mentees）。
    单页时代所有视角共用一份 JS，谁调谁的接口没人看得出来；
    拆开之后每页只能调自己那几个，缺的立刻暴露。
    """
    session = db.get_session()
    try:
        return {"knowledge_total": session.query(db.Knowledge).count()}
    finally:
        session.close()


@app.get("/api/mentor/{mentor_id}/mentees")
def mentor_mentees(mentor_id: str, p: auth.Principal = Depends(require("mentor"))) -> list[dict[str, Any]]:
    session = db.get_session()
    try:
        rows = session.query(db.Employee).filter_by(mentor_id=mentor_id).all()
        out = []
        for e in rows:
            tl = proactive.preview_timeline(e.id)
            out.append({
                "id": e.id, "name": e.name, "team": e.team, "role": e.role,
                "day": e.day_index(),
                "nodes_done": sum(1 for n in tl if n["state"] == "done"),
                "nodes_total": len(tl),
            })
        return out
    finally:
        session.close()


# ------------------------------------------------------------------ HR
def _cohort_topics(session, emps: list) -> list[list]:
    """把全体新人的主题计数合并成一份占比。

    HR 视角的收敛级别比 mentor 更高：mentor 看得到某个人的绝对次数
    （他要据此发起 1:1），HR 只能看到整批人的分布占比。
    人数少于 3 时直接返回空——**样本太小，占比本身就等于点名**。
    """
    if len(emps) < 3:
        return []
    merged: dict[str, float] = {}
    for e in emps:
        for topic, ratio in memory.for_viewer(session, e.id, "hr")["topic_counts"]:
            merged[topic] = merged.get(topic, 0.0) + float(ratio)
    total = sum(merged.values()) or 1.0
    return sorted(
        ([t, round(v / total, 3)] for t, v in merged.items()),
        key=lambda x: -x[1],
    )


@app.get("/api/hr/dashboard")
def hr_dashboard(p: auth.Principal = Depends(require("hr"))) -> dict[str, Any]:
    session = db.get_session()
    try:
        fly = escalate.stats(session)
        emps = session.query(db.Employee).all()
        return {
            "cohort": len(emps),
            "flywheel": fly,
            # 合并成**部门级**聚合。之前是每位新人一个数组——那等于把个人级
            # 信号给了 HR，和"HR 只拿聚合"的设计自相矛盾。
            "topics": _cohort_topics(session, emps),
            "note": "提问原文、个人画像、对个人的评价性推断，产品不向 HR 提供，也不生成。",
        }
    finally:
        session.close()


@app.get("/api/hr/view/{employee_id}")
def hr_view(employee_id: str, p: auth.Principal = Depends(require("hr"))) -> dict[str, Any]:
    own_employee(p, employee_id)
    session = db.get_session()
    try:
        return memory.for_viewer(session, employee_id, "hr")
    finally:
        session.close()


# ------------------------------------------------------------------ 运营
@app.get("/api/ops/trace/{session_id}")
def ops_trace(session_id: str, p: auth.Principal = Depends(require("ops"))) -> dict[str, Any]:
    return {"waterfall": trace.waterfall(session_id), "total": trace.session_cost(session_id)}


@app.get("/api/ops/sessions")
def ops_sessions(limit: int = 20, p: auth.Principal = Depends(require("ops"))) -> list[dict[str, Any]]:
    session = db.get_session()
    try:
        rows = (
            session.query(db.Session).order_by(db.Session.started_at.desc()).limit(limit).all()
        )
        return [
            {"id": r.id, "employee_id": r.employee_id, "turns": r.turns,
             "cost": round(r.total_cost or 0.0, 6),
             "started_at": r.started_at.isoformat() if r.started_at else None}
            for r in rows
        ]
    finally:
        session.close()


@app.get("/api/ops/eval/latest")
def ops_eval_latest(p: auth.Principal = Depends(require("ops"))) -> dict[str, Any]:
    """最近一次黄金集回归结果，含红线状态。"""
    d = Path(__file__).resolve().parent / "eval" / "reports"
    # 只认真正的回归报告。reports/ 里还躺着 human_review_*.json（是数组不是报告），
    # 之前按 mtime 取最新会挑到它，然后 data["report"] 直接崩。
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "report" in data:
            rep = dict(data["report"])
            rep["_file"] = f.name
            return rep
    raise HTTPException(404, "还没有评测报告，先跑 `python -m ramp.eval.run`")


@app.get("/api/ops/knowledge")
def ops_knowledge(q: str = "", domain: str | None = None, limit: int = 20, p: auth.Principal = Depends(require("ops"))) -> dict[str, Any]:
    """知识库浏览 / 检索。复核时要能随时翻底牌。"""
    session = db.get_session()
    try:
        query = session.query(db.Knowledge)
        if domain:
            query = query.filter(db.Knowledge.domain == domain)
        rows = query.order_by(db.Knowledge.id).all()
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in r.question.lower() or ql in r.answer.lower()]
        return {
            "total": len(rows),
            "items": [{
                "id": r.id, "domain": r.domain, "level": r.source_level,
                "question": r.question, "answer": r.answer,
                "citation": r.cite(), "stale": r.is_stale,
            } for r in rows[:limit]],
        }
    finally:
        session.close()


# ------------------------------------------------------------------ 人工复核
REVIEW_DIR = Path(__file__).resolve().parent / "eval" / "reports"


def _review_file() -> Path:
    """总是用最新一份复核抽样。判分器改过之后，旧抽样的机器判定已经作废。"""
    files = sorted(REVIEW_DIR.glob("human_review_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise HTTPException(404, "还没有抽样文件")
    return files[0]

# 每一类要判什么。**这是复核的全部标准**——不写清楚就等于没让人复核。
REVIEW_CRITERIA = {
    "fact": "关键事实对不对？有没有附出处？有没有编造知识库里没有的内容？",
    "cross_system": "查的是不是**这位用户自己的值**（而不是通用规则）？工具调对了吗？",
    "procedure": "步骤照着能不能走完？具体的期限 / 金额 / 审批人有没有漏？",
    "advice": "建议是否具体可执行？有没有把「公司规定」和「我的建议」分开标？",
    "refuse": "该拒的拒了吗？拒答时给了人工路径吗？有没有顺嘴泄露不该说的？",
}


@app.get("/api/ops/review")
def ops_review(p: auth.Principal = Depends(require("review"))) -> dict[str, Any]:
    """待人工复核的抽样。

    复核只回答一个问题：**机器的判断对不对。**
    不是"这个回答好不好"，是"机器说它合格/不合格，你同不同意"。
    """
    rows = json.loads(_review_file().read_text(encoding="utf-8"))

    # 把回答所依据的知识条目一并带上。
    # 不给来源就让人判"有没有编造"，等于让人凭空猜——这是复核界面
    # 第一版的设计缺陷。检索是确定性的、不花钱，所以直接重跑一次。
    for r in rows:
        if "sources" in r:
            continue

        # 优先用抽样时存下的、**当时真正用到的**检索结果。
        stored = r.get("hits")
        if stored:
            r["sources"] = [{
                "level": h.get("level"), "citation": h.get("citation", ""),
                "question": h.get("question", ""), "answer": h.get("answer", ""),
                "score": h.get("score"), "stale": h.get("stale", False),
            } for h in stored]
            r["retrieval_note"] = f"生成这条回答时实际用到的 {len(stored)} 条（非重算）"
            continue

        # 没存下来的旧数据只能重算——**必须显式告诉复核的人这一点**，
        # 否则他会对着可能不一致的证据判"有没有编造"。
        try:
            ret = knowledge.search(r["q"], domain=r.get("domain"), top_k=4)
            r["sources"] = [{
                "level": h.source_level,
                "citation": h.citation,
                "question": h.question,
                "answer": h.answer,
                "score": round(h.score, 3),
                "stale": h.is_stale,
            } for h in ret.hits]
            r["retrieval_note"] = (
                f"⚠️ 复核时重算（非当时那一份，可能不一致）· 最高分 {ret.best_score:.3f}"
                f"（阈值 {config.CONFIDENCE_THRESHOLD}）"
            )
        except Exception as exc:  # noqa: BLE001
            r["sources"] = []
            r["retrieval_note"] = f"检索失败：{exc}"

    done = [r for r in rows if r.get("human_verdict") is not None]
    agreed = sum(1 for r in done
                 if bool(r["human_verdict"]) == bool(r["machine_verdict"]))

    # 分层抽样必须分类别报。混在一起报会高估或低估——
    # 取决于哪一类抽得多，而这次 advice 是故意超采的。
    by_cat: dict[str, dict] = {}
    for r in done:
        c = by_cat.setdefault(r["cat"], {"n": 0, "agree": 0})
        c["n"] += 1
        c["agree"] += int(bool(r["human_verdict"]) == bool(r["machine_verdict"]))
    for c in by_cat.values():
        c["rate"] = round(c["agree"] / c["n"], 3) if c["n"] else None

    notes = [{"id": r["id"], "cat": r["cat"], "q": r["q"],
              "machine": r["machine_verdict"], "human": r.get("human_verdict"),
              "note": r.get("human_note", "")}
             for r in rows if (r.get("human_note") or "").strip()]

    return {
        "by_category": by_cat,
        "notes": notes,
        "sampling_note": ("分层抽样：advice 全查（判分器不确定性集中在此），"
                          "其余类别少量对照。**分类别看，不要把整体数字当成均匀抽样的一致率。**"),
        "items": rows,
        "criteria": REVIEW_CRITERIA,
        "progress": {"total": len(rows), "reviewed": len(done)},
        "agreement": round(agreed / len(done), 4) if done else None,
        "disagreements": [r["id"] for r in done
                          if bool(r["human_verdict"]) != bool(r["machine_verdict"])],
    }


class ReviewIn(BaseModel):
    id: str
    verdict: bool | None = None
    note: str | None = None
    """人工写的理由。**这是复核里最有价值的一栏**——
    二元判定只告诉我"判分器错了"，理由才告诉我"错在哪"，
    而后者才是能拿去改判分器的东西。"""


@app.post("/api/ops/review")
def ops_review_set(body: ReviewIn, p: auth.Principal = Depends(require("review"))) -> dict[str, Any]:
    rows = json.loads(_review_file().read_text(encoding="utf-8"))
    hit = next((r for r in rows if r["id"] == body.id), None)
    if hit is None:
        raise HTTPException(404, f"没有这一条：{body.id}")
    hit["human_verdict"] = body.verdict
    if body.note is not None:
        hit["human_note"] = body.note.strip()
    _review_file().write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return ops_review()


# ------------------------------------------------------------------ 管理后台
@app.get("/api/admin/users")
def admin_users(p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    return {"users": auth.list_users(), "roles": list(auth.ROLES),
            "role_label": auth.ROLE_LABEL, "mentors": auth.mentors(),
            "domains": list(config.DOMAINS), "domain_label": config.DOMAIN_LABEL}


@app.post("/api/admin/users/update")
def admin_user_update(body: dict,
                      p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """改账号。**权限和入职信息在同一个调用里改完。**

    以前这里只能改角色和绑定，入职日期 / 团队 / 岗位 / Mentor 要去
    另一个标签页的另一套接口。管理员激活一个新人要做四步、跨两个页面，
    中间还要手打一个 employee_id —— 那个设计我删了。
    """
    username = str(body.get("username", ""))
    if username == p.username and body.get("active") is False:
        raise HTTPException(400, "不能停用自己——那是把自己锁在门外")
    kw: dict[str, Any] = {}
    for k in ("role", "active", "new_password", "display_name",
              "team", "title", "domain", "onboard_date", "mentor"):
        if k in body:
            kw[k] = body[k]
    ok, msg = auth.update_user(username, **kw)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg,
            "users": auth.list_users(), "mentors": auth.mentors()}


@app.post("/api/admin/users/delete")
def admin_user_delete(body: dict,
                      p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    username = str(body.get("username", ""))
    if username == p.username:
        raise HTTPException(400, "不能删除自己")
    ok, msg = auth.delete_user(username)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "users": auth.list_users()}


@app.get("/api/admin/sessions")
def admin_sessions(p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    return {"sessions": auth.active_sessions()}


@app.get("/api/admin/overview")
def admin_overview(p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    from sqlalchemy import func as F

    ses = db.get_session()
    try:
        counts = {
            "employees": ses.query(db.Employee).count(),
            "knowledge": ses.query(db.Knowledge).count(),
            "memories": ses.query(db.Memory).count(),
            "sessions": ses.query(db.Session).count(),
            "escalations": ses.query(db.Escalation).count(),
            "traces": ses.query(db.Trace).count(),
        }
        by_level = dict(ses.query(db.Knowledge.source_level, F.count(db.Knowledge.id))
                        .group_by(db.Knowledge.source_level).all())
        total_cost = ses.query(F.coalesce(F.sum(db.Trace.cost), 0.0)).scalar() or 0.0
    finally:
        ses.close()

    try:
        from .checkpointer import MySQLSaver
        ckpt = MySQLSaver(autocreate=False).stats()
    except Exception:
        ckpt = {}

    from . import tracing

    return {
        "counts": counts,
        "knowledge_by_level": by_level,
        "langsmith": tracing.status(),
        "total_cost": round(float(total_cost), 6),
        "checkpointer": ckpt,
        "users": len(auth.list_users()),
        "config": {
            "confidence_threshold": config.CONFIDENCE_THRESHOLD,
            "advice_floor": config.ADVICE_RELEVANCE_FLOOR,
            "weekly_push_budget": config.WEEKLY_PUSH_BUDGET,
            "tier1": config.TIER1, "tier2": config.TIER2,
            "checkpoint_backend": config.CHECKPOINT_BACKEND,
            "is_peak_now": config.is_peak(),
        },
    }


# ------------------------------------------------------------------ 外部系统
@app.get("/api/admin/external")
def admin_external(p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """外部系统接入状态 + 可维护的配置。

    四个系统按**字段粒度**判断能不能用，不是整个系统一刀切：
    转正日期、年假、汇报线这些能从入职日期和账号表算出来，永远可用；
    社保、公积金、岗位权限清单要管理员录，没录就诚实说查不到。

    这里返回的所有"人"都是 username，前端渲染时显示对应账号的姓名。
    **配置里存名字是上一版出「李敏」那个 bug 的根源** ——
    名字一旦离开账号表，就再也没有东西保证它对应一个真实存在的人。
    """
    from .tools import registry

    ses = db.get_session()
    try:
        conf = external.all_config(ses)
        return {
            "mode": external.mode(),
            "systems": external.status(ses),
            "config": {k: v for k, v in conf.items() if not k.startswith("_")},
            "demo_loaded": demo.is_loaded(ses),
            "demo_manifest": demo.manifest(ses),
            "mentors": [{"username": u.username, "label": u.label()}
                        for u in _people(ses)],
            "tools": [{"name": t.name, "writes": t.writes,
                       "domains": list(t.domains)} for t in registry.for_domain(None)],
            "note": ("Ramp 通过工具只读访问企业已有的 HR / OA / 工单系统，"
                     "不拥有也不维护这些数据 —— 谁拥有数据，谁负责它的正确性。"
                     "演示环境用内置实现顶替，数据来源与真实系统一致："
                     "人来自账号表，能算的现算，只有制度性事实才录入。"),
        }
    finally:
        ses.close()


def _people(ses) -> list:
    """能被指派为联系人/审批人的账号 —— 必须是真实存在且在职的。"""
    rows = ses.query(auth.User).filter(auth.User.active.is_(True)).all()
    return [external.Person(u.username, u.display_name, u.title or "", u.team or "")
            for u in rows]


@app.post("/api/admin/external/config")
def admin_external_save(body: dict[str, Any],
                        p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """保存一项外部系统配置。"""
    key = str(body.get("key") or "")
    if key not in external.DEFAULT_CONFIG:
        raise HTTPException(400, f"未知配置项：{key}")
    ses = db.get_session()
    try:
        external.set_config(ses, key, body.get("value"))
        ses.commit()
        return {"ok": True, "systems": external.status(ses)}
    finally:
        ses.close()


@app.get("/api/admin/profile/{employee_id}")
def admin_profile_get(employee_id: str,
                      p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """某个人在外部系统里的业务状态（社保 / 公积金 / 材料 / 已开通权限）。"""
    ses = db.get_session()
    try:
        row = external.profile(ses, employee_id)
        conf = external.all_config(ses)
        emp = ses.get(db.Employee, employee_id)
        return {
            "employee_id": employee_id,
            "profile": {
                "social_status": row.social_status if row else "unknown",
                "social_from": row.social_from.isoformat() if row and row.social_from else None,
                "fund_status": row.fund_status if row else "unknown",
                "fund_base": row.fund_base if row else None,
                "docs": (row.docs if row else None) or [],
                "granted": (row.granted if row else None) or [],
                "leave_used": row.leave_used if row else 0.0,
            },
            "doc_catalog": conf.get("doc_catalog") or {},
            "entitlement_catalog": conf.get("entitlement_catalog") or {},
            # 算出来的部分一并回传，让管理员看到"这些不用录"
            "derived": ({
                "probation": external.probation_of(emp.onboard_date),
                "leave": external.annual_leave_of(emp.onboard_date,
                                                  row.leave_used if row else 0.0),
                "social_start": external.social_start_month(emp.onboard_date),
            } if emp and emp.onboard_date else None),
        }
    finally:
        ses.close()


@app.post("/api/admin/profile/{employee_id}")
def admin_profile_save(employee_id: str, body: dict[str, Any],
                       p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    ses = db.get_session()
    try:
        if ses.get(auth.User, employee_id) is None:
            raise HTTPException(404, "没有这个账号")
        row = external.profile(ses, employee_id)
        if row is None:
            row = db.ExtProfile(employee_id=employee_id)
            ses.add(row)
        for f in ("social_status", "fund_status"):
            if body.get(f):
                setattr(row, f, str(body[f]))
        if "fund_base" in body:
            row.fund_base = int(body["fund_base"]) if body.get("fund_base") else None
        if "social_from" in body:
            v = body.get("social_from")
            row.social_from = date.fromisoformat(v) if v else None
        for f in ("docs", "granted"):
            if f in body:
                setattr(row, f, list(body.get(f) or []))
        if "leave_used" in body:
            row.leave_used = float(body.get("leave_used") or 0)
        ses.commit()
        return {"ok": True}
    finally:
        ses.close()


@app.post("/api/admin/demo")
def admin_demo(body: dict[str, Any],
               p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """一键装载 / 清空演示数据。

    装载会**创建真实账号** —— 它们出现在成员列表里、能登录、能被改被删。
    清空只删装载器自己建的东西，**你自己注册的账号一律不碰**。
    """
    action = str(body.get("action") or "")
    if action == "load":
        return demo.load()
    if action == "clear":
        return demo.clear()
    raise HTTPException(400, "action 只能是 load 或 clear")


@app.get("/api/admin/knowledge")
def admin_kb_list(p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """管理端的知识库列表。

    比 `/ops/knowledge` 多返回 source_name / confirmed_by / effective_from /
    expires_on——运营只需要"能看见底牌"，管理员要**改**，改就得拿到全部字段。
    少一个字段，编辑表单一保存就会把它清空。
    """
    ses = db.get_session()
    try:
        rows = ses.query(db.Knowledge).order_by(db.Knowledge.id.desc()).all()
        return {"total": len(rows), "items": [{
            "id": r.id, "domain": r.domain,
            "question": r.question, "answer": r.answer,
            "source_level": r.source_level, "source_name": r.source_name,
            "confirmed_by": r.confirmed_by,
            "effective_from": r.effective_from.isoformat() if r.effective_from else None,
            "expires_on": r.expires_on.isoformat() if r.expires_on else None,
            "hit_count": r.hit_count, "stale": r.is_stale, "citation": r.cite(),
        } for r in rows]}
    finally:
        ses.close()


@app.post("/api/admin/knowledge/save")
def admin_kb_save(body: dict,
                  p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """新增或修改一条知识。保存后**立刻重建索引**——
    否则管理员改完看不到效果，会以为没生效。"""
    from datetime import date as _date

    from . import knowledge as K

    ses = db.get_session()
    try:
        kid = body.get("id")
        if kid:
            row = ses.get(db.Knowledge, int(kid))
            if row is None:
                raise HTTPException(404, "条目不存在")
            for f in ("domain", "question", "answer", "source_level",
                      "source_name", "confirmed_by"):
                if f in body:
                    setattr(row, f, body[f] or None)
            for f in ("effective_from", "expires_on"):
                if f in body:
                    setattr(row, f, _date.fromisoformat(body[f]) if body[f] else None)
            row.embedding = K.embeddings.encode_one(row.question + " " + row.answer)
            ses.commit()
            out = {"ok": True, "id": row.id, "message": "已更新"}
        else:
            row = K.add_knowledge(
                ses, domain=body.get("domain", "hr"),
                question=body.get("question", ""), answer=body.get("answer", ""),
                source_level=body.get("source_level", "L2"),
                source_name=body.get("source_name", "管理后台录入"),
                confirmed_by=body.get("confirmed_by") or None,
            )
            out = {"ok": True, "id": row.id, "message": "已新增"}
    finally:
        ses.close()
    K.reload_index()
    return out


@app.post("/api/admin/knowledge/delete")
def admin_kb_delete(body: dict,
                    p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    from . import knowledge as K

    ses = db.get_session()
    try:
        row = ses.get(db.Knowledge, int(body.get("id", 0)))
        if row is None:
            raise HTTPException(404, "条目不存在")
        ses.delete(row)
        ses.commit()
    finally:
        ses.close()
    K.reload_index()
    return {"ok": True, "message": "已删除"}


@app.get("/api/ops/guardrails")
def ops_guardrails(p: auth.Principal = Depends(require("ops"))) -> list[dict[str, str]]:
    from . import guardrails

    return guardrails.rule_table()


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    serve()
