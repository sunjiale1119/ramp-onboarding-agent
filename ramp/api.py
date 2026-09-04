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

from . import auth, config, db, escalate, memory, proactive, runtime, trace, knowledge

app = FastAPI(title="爬坡 Ramp API", version="0.1.0")


@app.on_event("startup")
def _demo_startup() -> None:
    """演示模式：每次启动把演示员工滚回 30 天窗口内。

    容器 restart 策略是 unless-stopped，服务器重启会自动拉起，
    所以这个钩子实际上就是"演示站每次醒来都自我校准一次"。
    """
    if not config.DEMO_MODE:
        return
    try:
        from .bootstrap import refresh_demo_dates

        n = refresh_demo_dates()
        if n:
            import logging

            logging.getLogger("ramp").warning(
                "[演示模式] 已把 %d 位演示员工的入职日期滚回 30 天窗口内", n)
    except Exception as exc:  # noqa: BLE001
        # 演示数据校准失败不该拖垮启动 —— 大不了时间线不好看
        import logging

        logging.getLogger("ramp").warning("[演示模式] 日期校准失败：%s", exc)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地原型演示；上生产必须收窄
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ 模型
class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    employee_id: str = "e_linxy"
    session_id: str | None = None


class ConfirmIn(BaseModel):
    session_id: str
    confirmed: bool


class AnswerIn(BaseModel):
    escalation_id: int
    answer: str = Field(min_length=1, max_length=4000)
    confirmed_by: str = "陈昊"
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
            answer=body.answer, confirmed_by=body.confirmed_by, sink=body.sink,
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
            "role_label": auth.ROLE_LABEL}


@app.post("/api/admin/users/update")
def admin_user_update(body: dict,
                      p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    username = str(body.get("username", ""))
    if username == p.username and body.get("active") is False:
        raise HTTPException(400, "不能停用自己——那是把自己锁在门外")
    kw: dict[str, Any] = {}
    for k in ("role", "active", "new_password"):
        if k in body:
            kw[k] = body[k]
    if "employee_id" in body:
        kw["employee_id"] = body["employee_id"]
    ok, msg = auth.update_user(username, **kw)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "users": auth.list_users()}


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
    """外部系统里有哪些人和数据。**只读，Ramp 不拥有这些。**

    ## 为什么需要这一页

    有人提了个权限工单，Agent 回答"审批人李敏（数据平台负责人）"，
    然后管理员翻遍后台**找不到李敏这个人** —— 看起来像 Agent 编的。

    实际上李敏存在，只是存在于**另一个地方**：系统里的"人"分散在三处 ——

        employees 表                新人档案（Ramp 自己的数据）
        employees.mentor_* 字段     Mentor（不是独立记录）
        外部系统 org_directory      leader、HRBP、行政
        外部系统 it_approvers       各类资源的审批人

    前两处管理后台管得了，后两处**零可见性**。

    ## 为什么不把李敏也塞进 employees 表

    因为那会把只读的外部数据变成 Ramp 自己的数据。真实部署里
    org_directory 和 it_servicedesk 是企业已有的 OA / 工单系统，
    Ramp 通过工具只读访问，不负责维护 —— **谁拥有数据，谁负责它的正确性**。

    所以这一页是**只读的**：让管理员能核对 Agent 说的人名确有其人、
    审批人配的是谁、SLA 是几天，但改不了。要改去源系统改。
    """
    from . import tools as _tools  # noqa: F401  确保 mock 已加载

    from .tools.builtin import mock

    m = mock()
    org = m.get("org_directory", {})
    desk = m.get("it_servicedesk", {})

    # 组织架构里出现过的所有人名，按角色归类
    people: dict[str, dict[str, Any]] = {}

    def note(name: str, role: str, ctx: str) -> None:
        if not name:
            return
        rec = people.setdefault(name, {"name": name, "roles": [], "context": []})
        if role not in rec["roles"]:
            rec["roles"].append(role)
        if ctx and ctx not in rec["context"]:
            rec["context"].append(ctx)

    for emp_id, rec in org.items():
        if emp_id == "contacts" or not isinstance(rec, dict):
            continue
        note(rec.get("leader", ""), "直属 leader", f"{rec.get('team', '')} · 带 {emp_id}")
        note(rec.get("mentor", ""), "Mentor", f"{rec.get('team', '')} · 带 {emp_id}")
    for key, c in (org.get("contacts") or {}).items():
        if isinstance(c, dict):
            note(c.get("name", ""), {"hrbp": "HRBP", "it_helpdesk": "IT 服务台",
                                     "admin": "行政"}.get(key, key),
                 c.get("channel", ""))
    approvers = []
    for res, ap in (desk.get("approvers") or {}).items():
        if not isinstance(ap, dict):
            continue
        approvers.append({"resource": res, "name": ap.get("name", ""),
                          "title": ap.get("title", ""),
                          "sla_days": ap.get("sla_days"),
                          "extra_review": ap.get("extra_review")})
        note(ap.get("name", ""), "审批人", f"{res} · {ap.get('title', '')}")

    return {
        "people": sorted(people.values(), key=lambda x: x["name"]),
        "approvers": sorted(approvers, key=lambda x: x["resource"]),
        "org": [{"employee_id": k, **v} for k, v in org.items()
                if k != "contacts" and isinstance(v, dict)],
        "contacts": [{"key": k, **v} for k, v in (org.get("contacts") or {}).items()
                     if isinstance(v, dict)],
        "source": "seed/mock_systems.json",
        "note": ("这些数据来自外部系统（组织架构、IT 服务台），Ramp 只读。"
                 "演示环境里它们是 mock JSON；真实部署应接企业已有的 OA / 工单系统。"),
    }


# ------------------------------------------------------------------ 员工档案
@app.get("/api/admin/employees")
def admin_employees(p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """员工档案清单，附带「已被哪个账号绑定」。

    这一栏是补上来的：原来档案只能靠 bootstrap 从种子文件灌进去，
    管理后台只能把账号绑到**已存在**的档案上。
    于是一个新注册的人卡在半路——账号激活了、角色派了，
    但没有档案，工作台打不开，而管理员在界面上**找不到任何地方能建档案**。
    """
    ses = db.get_session()
    try:
        rows = ses.query(db.Employee).order_by(db.Employee.onboard_date.desc()).all()
        bound = {u["employee_id"]: u["username"]
                 for u in auth.list_users() if u["employee_id"]}
        # mentor 不在 employees 表里，是以 mentor_id 字段存在的，单独汇总
        mentors: dict[str, str] = {}
        for e in rows:
            if e.mentor_id:
                mentors.setdefault(e.mentor_id, e.mentor_name or e.mentor_id)
        return {
            "employees": [{
                "id": e.id, "name": e.name, "team": e.team, "role": e.role,
                "domain": e.domain, "mentor_id": e.mentor_id,
                "mentor_name": e.mentor_name,
                "onboard_date": e.onboard_date.isoformat(),
                "day_index": e.day_index(),
                "bound_to": bound.get(e.id),
            } for e in rows],
            "mentors": [{"id": k, "name": v} for k, v in sorted(mentors.items())],
            "domains": list(config.DOMAINS),
            "domain_label": config.DOMAIN_LABEL,
        }
    finally:
        ses.close()


@app.post("/api/admin/employees/save")
def admin_employee_save(body: dict,
                        p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """新建或修改员工档案。

    新建时会**顺带写入三条语义记忆**（岗位 / Mentor / 入职日期）——
    因为新人工作台右栏那个「Ramp 记得关于我的」读的就是这三条，
    不写的话新人第一次打开看到的是空的。
    bootstrap 里做了这件事，手工建档案时也必须做，否则两条路径产出的数据不一致。
    """
    from datetime import date as _date

    emp_id = str(body.get("id", "")).strip()
    if not emp_id or not emp_id.replace("_", "").isalnum():
        raise HTTPException(400, "员工 ID 只能用字母、数字、下划线，例如 e_zhangsan")
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "请填写姓名")
    try:
        onboard = _date.fromisoformat(str(body.get("onboard_date", "")))
    except ValueError:
        raise HTTPException(400, "入职日期格式应为 YYYY-MM-DD") from None

    ses = db.get_session()
    try:
        row = ses.get(db.Employee, emp_id)
        created = row is None
        if created:
            row = db.Employee(id=emp_id)
            ses.add(row)
        row.name = name
        row.team = str(body.get("team", "")).strip() or "未填"
        row.role = str(body.get("role", "")).strip() or "未填"
        row.domain = str(body.get("domain", "biz"))
        row.mentor_id = str(body.get("mentor_id", "")).strip() or None
        row.mentor_name = str(body.get("mentor_name", "")).strip() or None
        row.onboard_date = onboard
        ses.commit()

        # 语义记忆对账：三条事实必须和档案一致（新建时补齐，修改时更新）
        facts = [("岗位", f"{row.team} · {row.role}"),
                 ("Mentor", row.mentor_name or "未分配"),
                 ("入职日期", row.onboard_date.isoformat())]
        for topic, content in facts:
            m = (ses.query(db.Memory)
                 .filter_by(subject_id=emp_id, kind="semantic", topic=topic).first())
            if m is None:
                ses.add(db.Memory(subject_id=emp_id, kind="semantic", topic=topic,
                                  content=content, visible_to_self=True,
                                  visible_to_mentor=True, visible_to_hr=False))
            elif m.content != content:
                m.content = content
        ses.commit()
    finally:
        ses.close()
    return {"ok": True, "id": emp_id,
            "message": "已建档并写入语义记忆" if created else "已更新"}


@app.post("/api/admin/employees/delete")
def admin_employee_delete(body: dict,
                          p: auth.Principal = Depends(require("admin"))) -> dict[str, Any]:
    """删除档案。**绑着账号的删不掉**——先解绑再删。

    否则那个账号会变成「绑了一个不存在的 id」，登录后工作台直接报错，
    而管理员在用户列表里只看到一个红色的「查无此档案」，
    完全不知道是自己刚才删的。
    """
    emp_id = str(body.get("id", "")).strip()
    owner = next((u["username"] for u in auth.list_users()
                  if u["employee_id"] == emp_id), None)
    if owner:
        raise HTTPException(400, f"档案 {emp_id} 还绑在账号 {owner} 上，请先解绑再删除")

    ses = db.get_session()
    try:
        row = ses.get(db.Employee, emp_id)
        if row is None:
            raise HTTPException(404, "档案不存在")
        n_mem = ses.query(db.Memory).filter_by(subject_id=emp_id).delete()
        ses.delete(row)
        ses.commit()
    finally:
        ses.close()
    return {"ok": True, "message": f"已删除档案与 {n_mem} 条记忆"}


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
