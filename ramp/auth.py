"""登录与鉴权。

## 为什么这个模块对产品叙事很关键

在它之前，控制台的角色切换是个**演示开关**——谁都能点成 mentor。
那时"提问原文对 mentor 不可见"只是个说法，因为看的人自己就能切。

有了它之后这句话才成立：**登录成 mentor，服务端根本不返回原文**。
可见性矩阵从"设计意图"变成了"能被验证的约束"。

## 三条不能省的实现要求

1. **密码只存哈希**。PBKDF2-SHA256 + 每人独立 salt + 20 万次迭代。
   用标准库，不引依赖——但迭代次数不能省，那是唯一挡住离线爆破的东西。
2. **鉴权在 API 层，不在界面层**。前端隐藏按钮只是体验，
   服务端拒绝返回才是边界。这两件事经常被混为一谈。
3. **会话存库、可撤销、有过期**。签名 token（如 JWT）撤销不了——
   登出之后那个串还是有效的，只是客户端把它丢了。
   对一个"记忆可删、提问原文不外露"的产品来说，登出必须真的失效。

## 演示账号

`bootstrap` 会种四个账号，密码都是 `ramp2026`，并在登录页明示。
**这是演示环境的刻意选择，不是疏忽**——真实部署要走企业 SSO，
见 `docs/` 里的已知边界。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, get_session

# ------------------------------------------------------------------ 角色
ROLES = ("newbie", "mentor", "hr", "ops", "admin")

ROLE_LABEL = {
    "newbie": "新人",
    "mentor": "Mentor",
    "hr": "HR · People Ops",
    "ops": "运营 / 质量",
    "admin": "系统管理员",
}

# 每个角色能看哪些视角。**这是权限的唯一定义处**，
# 前端只是照着渲染，服务端照着放行。
ROLE_VIEWS: dict[str, tuple[str, ...]] = {
    "newbie": ("newbie",),
    "mentor": ("mentor",),
    "hr": ("hr",),
    "ops": ("ops", "review"),
    # admin 能进管理后台，但**看不到新人的提问原文**——
    # 管理员权限管的是"账号和配置"，不是"绕过可见性矩阵"。
    # 这两件事经常被混为一谈：很多系统里 admin 等于上帝，
    # 而这个产品的核心承诺恰恰是"没有人能看到原文"。
    "admin": ("admin", "ops", "review"),
}

HOME = {
    "newbie": "/newbie",
    "mentor": "/mentor",
    "hr": "/hr",
    "ops": "/ops",
    "admin": "/admin",
}

SESSION_HOURS = int(os.getenv("RAMP_SESSION_HOURS", "12"))
PBKDF2_ROUNDS = int(os.getenv("RAMP_PBKDF2_ROUNDS", "200000"))


# ------------------------------------------------------------------ 表
class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))

    # ---- 入职信息。**这些以前在另一张表里，要管理员手动绑定** ----
    #
    # 原设计把一个人拆成「账号」和「员工档案」两个东西：账号回答"能进哪个页面"，
    # 档案回答"哪天入职、谁带你"。管理员注册一个新人要在两个标签页做四步，
    # 中间还要手打一个必须精确匹配的 employee_id。
    #
    # 拆分的理由是"档案属于 HR 系统，账号属于 Ramp" —— 听起来成立，
    # 实际上在这个产品里**两者一一对应，从来没有过一个人有账号没档案
    # 或有档案没账号的合理场景**。为一个不存在的多对多关系付了一个
    # 手动绑定步骤的代价。
    #
    # 现在合并：employee_id 就是 username，绑定这个概念消失。
    team: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """岗位。字段名不叫 role 是因为 role 已经被"系统角色"占了。"""
    onboard_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    mentor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """带教人的 **username**，不是姓名 —— 姓名会改，用户名不会。"""
    domain: Mapped[str] = mapped_column(String(16), default="biz")

    salt: Mapped[str] = mapped_column(String(64))
    pwd_hash: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Session_(Base):
    """会话表。**存库而不是签名 token，就是为了能撤销。**"""

    __tablename__ = "auth_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ------------------------------------------------------------------ 密码
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (salt, hash)。迭代次数写进配置，方便以后调高。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return salt, dk.hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    """**必须用 compare_digest**——普通的 == 会因为提前返回而泄露信息，
    攻击者能靠计时差一位一位地把哈希试出来。"""
    _, actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected)


# ------------------------------------------------------------------ 会话
@dataclass
class Principal:
    """当前登录者。所有鉴权判断都基于它，不基于前端传来的任何字段。"""

    username: str
    display_name: str
    role: str

    @property
    def employee_id(self) -> str:
        """员工 id 就是用户名。

        以前这是一个可空的、要管理员手填的字段，于是有了三种坏状态：
        没绑（工作台打不开）、绑了个不存在的 id（登录报错）、
        绑到别人的 id（读到别人的提问原文）。三种都真实发生过。
        改成恒等之后，这三种状态在结构上不可能出现。
        """
        return self.username

    @property
    def home(self) -> str:
        return HOME.get(self.role, "/login")

    @property
    def views(self) -> tuple[str, ...]:
        return ROLE_VIEWS.get(self.role, ())

    def can_view(self, view: str) -> bool:
        return view in self.views

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": ROLE_LABEL.get(self.role, self.role),
            "employee_id": self.employee_id,
            "views": list(self.views),
            "home": self.home,
        }


def login(username: str, password: str) -> str | None:
    """验证并开一个会话。失败返回 None——**不区分"用户不存在"和"密码错"**，
    否则登录页就成了用户名枚举器。"""
    s = get_session()
    try:
        u = s.get(User, username.strip().lower())
        if u is None or not u.active:
            # 用户不存在也跑一次哈希，避免用响应时间区分出账号是否存在
            hash_password(password)
            return None
        if not verify_password(password, u.salt, u.pwd_hash):
            return None

        token = secrets.token_urlsafe(32)
        s.add(Session_(
            token=token,
            username=u.username,
            expires_at=datetime.now() + timedelta(hours=SESSION_HOURS),
        ))
        s.commit()
        return token
    finally:
        s.close()


def resolve(token: str | None) -> Principal | None:
    """token → Principal。过期会话顺手删掉。"""
    if not token:
        return None
    s = get_session()
    try:
        sess = s.get(Session_, token)
        if sess is None:
            return None
        if sess.expires_at < datetime.now():
            s.delete(sess)
            s.commit()
            return None
        u = s.get(User, sess.username)
        if u is None or not u.active:
            return None
        return Principal(u.username, u.display_name, u.role)
    finally:
        s.close()


def logout(token: str | None) -> bool:
    if not token:
        return False
    s = get_session()
    try:
        sess = s.get(Session_, token)
        if sess is None:
            return False
        s.delete(sess)
        s.commit()
        return True
    finally:
        s.close()


def purge_expired() -> int:
    s = get_session()
    try:
        n = s.query(Session_).filter(Session_.expires_at < datetime.now()).delete()
        s.commit()
        return n
    finally:
        s.close()


def register(username: str, password: str, display_name: str) -> tuple[bool, str]:
    """自助注册。**建成待激活状态，由管理员分配角色。**

    为什么不让人自己选角色：选了就等于自己给自己发权限。
    这个产品里 HR 视角能看到全批新人的聚合数据，mentor 能看到卡点信号——
    这些不该由注册表单决定。**注册只证明"你是谁"，授权是另一件事。**
    """
    u = (username or "").strip().lower()
    if not (3 <= len(u) <= 32) or not u.replace("_", "").isalnum():
        return False, "用户名需 3–32 位，只能用字母、数字、下划线"
    if len(password or "") < 8:
        return False, "密码至少 8 位"
    if not (display_name or "").strip():
        return False, "请填写姓名"

    s = get_session()
    try:
        if s.get(User, u):
            return False, "该用户名已被使用"
        salt, h = hash_password(password)
        s.add(User(username=u, display_name=display_name.strip(), role="newbie",
                   salt=salt, pwd_hash=h, active=False))
        s.commit()
        return True, "注册成功，等待管理员激活并分配角色"
    finally:
        s.close()


def list_users() -> list[dict[str, Any]]:
    """账号清单。入职信息就在同一行，不用再去别的地方查。"""
    s = get_session()
    try:
        rows = s.query(User).order_by(User.created_at.desc()).all()
        names = {u.username: u.display_name for u in rows}
        today = date.today()
        out = []
        for u in rows:
            day = ((today - u.onboard_date).days + 1) if u.onboard_date else None
            out.append({
                "username": u.username,
                "display_name": u.display_name,
                "role": u.role,
                "role_label": ROLE_LABEL.get(u.role, u.role),
                "employee_id": u.username,
                "active": bool(u.active),
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "team": u.team,
                "title": u.title,
                "domain": u.domain,
                "onboard_date": u.onboard_date.isoformat() if u.onboard_date else None,
                "day_index": day,
                "mentor": u.mentor,
                "mentor_name": names.get(u.mentor) if u.mentor else None,
                # 新人角色但没填入职日期 → 时间线和主动推送都跑不起来
                "needs_onboard": u.role == "newbie" and u.onboard_date is None,
                # 指向一个已经不存在的账号
                "mentor_missing": bool(u.mentor and u.mentor not in names),
            })
        return out
    finally:
        s.close()


def mentors() -> list[dict[str, str]]:
    """可选的带教人：系统里 role=mentor 且已激活的账号。

    以前 mentor 是员工档案里的两个自由文本字段（mentor_id + mentor_name），
    于是"陈昊"这个人**在系统里没有任何实体**——他不是账号，不是档案，
    只是别人档案里的一个字符串。改成引用真实账号之后，
    Mentor 登录看到的"我带的人"和新人看到的"我的 Mentor"才是同一份数据。
    """
    s = get_session()
    try:
        return [{"username": u.username, "display_name": u.display_name}
                for u in s.query(User)
                .filter(User.role == "mentor", User.active.is_(True))
                .order_by(User.display_name).all()]
    finally:
        s.close()


def sync_employee(username: str) -> None:
    """把账号里的入职信息同步到 employees 表。

    employees 表还在，因为 memories / sessions / escalations 都按
    subject_id 关联它。但它现在是**账号的投影**，不是需要人维护的东西——
    管理员在界面上再也看不到"员工档案"这四个字。
    """
    from .db import Employee, Memory

    s = get_session()
    try:
        u = s.get(User, username)
        if u is None:
            return
        # 只有会用到工作台的角色才需要档案
        if u.role not in ("newbie", "mentor") or u.onboard_date is None:
            return
        mentor_name = None
        if u.mentor:
            m = s.get(User, u.mentor)
            mentor_name = m.display_name if m else None

        e = s.get(Employee, username)
        if e is None:
            e = Employee(id=username, onboard_date=u.onboard_date)
            s.add(e)
        e.name = u.display_name
        e.team = u.team or "未填"
        e.role = u.title or "未填"
        e.domain = u.domain or "biz"
        e.mentor_id = u.mentor
        e.mentor_name = mentor_name
        e.onboard_date = u.onboard_date
        s.commit()

        # 三条语义记忆是新人工作台右栏读的内容，必须和账号保持一致
        facts = [("岗位", f"{e.team} · {e.role}"),
                 ("Mentor", mentor_name or "未分配"),
                 ("入职日期", u.onboard_date.isoformat())]
        for topic, content in facts:
            m = (s.query(Memory)
                 .filter_by(subject_id=username, kind="semantic", topic=topic).first())
            if m is None:
                s.add(Memory(subject_id=username, kind="semantic", topic=topic,
                             content=content, visible_to_self=True,
                             visible_to_mentor=True, visible_to_hr=False))
            elif m.content != content:
                m.content = content
        s.commit()
    finally:
        s.close()


def update_user(username: str, *, role: str | None = None,
                active: bool | None = None, new_password: str | None = None,
                display_name: str | None = None, team: str | None = None,
                title: str | None = None, domain: str | None = None,
                onboard_date: str | None = None,
                mentor: str | None = None) -> tuple[bool, str]:
    """改账号。入职信息和权限在同一个调用里改完——**这就是"一步搞定"**。"""
    from datetime import date as _date

    s = get_session()
    try:
        u = s.get(User, username)
        if u is None:
            return False, "用户不存在"

        if role is not None:
            if role not in ROLES:
                return False, f"角色非法：{role}"
            u.role = role
        if display_name is not None and display_name.strip():
            u.display_name = display_name.strip()
        if team is not None:
            u.team = team.strip() or None
        if title is not None:
            u.title = title.strip() or None
        if domain is not None:
            u.domain = domain or "biz"
        if onboard_date is not None:
            if onboard_date:
                try:
                    u.onboard_date = _date.fromisoformat(onboard_date)
                except ValueError:
                    return False, "入职日期格式应为 YYYY-MM-DD"
            else:
                # 新人 / Mentor 清空入职日期 = 让他的工作台失效。
                # 第一版这里直接放行，结果一个"清空"操作能把一个正常的人
                # 变成时间线跑不起来的坏状态，**而且没有任何提示**。
                # 要真想解除，先把角色改掉。
                if u.role in ("newbie", "mentor"):
                    return False, (f"{ROLE_LABEL.get(u.role, u.role)}"
                                   "必须有入职日期，否则 30 天时间线和主动推送都跑不起来")
                u.onboard_date = None
        if mentor is not None:
            m = mentor.strip()
            if m:
                if m == username:
                    return False, "不能把自己设成自己的 Mentor"
                target = s.get(User, m)
                if target is None:
                    return False, f"没有 {m} 这个账号"
                if target.role != "mentor":
                    return False, f"{target.display_name} 的角色不是 Mentor"
            u.mentor = m or None

        # 激活一个缺入职日期的新人 / Mentor = 激活出一个用不了的账号
        if active is True and u.role in ("newbie", "mentor") and u.onboard_date is None:
            return False, (f"{ROLE_LABEL.get(u.role, u.role)}"
                           "缺入职日期，先点「设置」填完再激活")

        if active is not None:
            u.active = active
            if not active:
                # 停用要**同时踢掉在线会话**，否则那个人还能用到过期为止
                s.query(Session_).filter(Session_.username == username).delete()
        if new_password:
            if len(new_password) < 8:
                return False, "密码至少 8 位"
            u.salt, u.pwd_hash = hash_password(new_password)
            s.query(Session_).filter(Session_.username == username).delete()
        s.commit()
    finally:
        s.close()

    sync_employee(username)
    return True, "已保存"


def delete_user(username: str) -> tuple[bool, str]:
    """删账号，连同它的档案与记忆。"""
    from .db import Employee, Memory

    s = get_session()
    try:
        u = s.get(User, username)
        if u is None:
            return False, "用户不存在"
        if u.role == "admin" and s.query(User).filter(
                User.role == "admin", User.active.is_(True)).count() <= 1:
            return False, "不能删除最后一个管理员——那会把自己锁在门外"

        # 别人把他当 Mentor 的话，先解开，否则那些人指向一个不存在的账号
        n_ref = s.query(User).filter(User.mentor == username).update(
            {User.mentor: None})
        s.query(Session_).filter(Session_.username == username).delete()
        n_mem = s.query(Memory).filter(Memory.subject_id == username).delete()
        e = s.get(Employee, username)
        if e is not None:
            s.delete(e)
        s.delete(u)
        s.commit()
    finally:
        s.close()
    extra = f"，并解除了 {n_ref} 人的带教关系" if n_ref else ""
    return True, f"已删除账号、档案与 {n_mem} 条记忆{extra}"


def active_sessions() -> list[dict[str, Any]]:
    s = get_session()
    try:
        return [{"username": x.username,
                 "expires_at": x.expires_at.isoformat(),
                 "created_at": x.created_at.isoformat() if x.created_at else None}
                for x in s.query(Session_).order_by(Session_.created_at.desc()).all()]
    finally:
        s.close()


# ------------------------------------------------------------------ 种子
ADMIN_USERNAME = os.getenv("RAMP_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("RAMP_ADMIN_PASSWORD", "ramp2026")


def seed_users(password: str | None = None) -> int:
    """只种一个管理员，其余账号靠自助注册。

    ## 为什么不再种演示账号

    原来种五个（林小雨、陈昊、王倩、运营、管理员），配着一整套虚构的
    社保记录、权限清单、组织架构。演示效果好，但代价是**整个系统里
    没有一条数据是真的**——看的人分不清哪些是产品能力，哪些是我编的剧本。

    现在只种管理员。第一个人登录后自己注册团队成员、自己填入职日期和带教关系，
    产生的每一条数据都是真的。**知识库仍然是虚构的**（那是刻意的：
    只有自建才能精确控制 L1/L2/L3 分级来验证降权机制），但除此之外没有虚构。
    """
    pw = password or ADMIN_PASSWORD
    s = get_session()
    try:
        if s.get(User, ADMIN_USERNAME):
            return 0
        salt, h = hash_password(pw)
        s.add(User(username=ADMIN_USERNAME, display_name="管理员",
                   role="admin", salt=salt, pwd_hash=h, active=True))
        s.commit()
        return 1
    finally:
        s.close()


def wipe_users(keep_admin: bool = True) -> dict[str, int]:
    """清空账号与相关数据。**破坏性操作，只给 CLI 用。**

    连带删除 employees / memories / sessions / messages / escalations /
    push_log —— 这些都是挂在人身上的，人没了留着就是孤儿数据。
    知识库和评测报告不动。
    """
    from .db import (Employee, Escalation, Memory, Message, PushLog,
                     Session as Sess, Trace)

    s = get_session()
    try:
        keep = {ADMIN_USERNAME} if keep_admin else set()
        n: dict[str, int] = {}
        q = s.query(User)
        if keep:
            q = q.filter(~User.username.in_(keep))
        n["users"] = q.delete(synchronize_session=False)
        n["sessions_auth"] = s.query(Session_).filter(
            ~Session_.username.in_(keep) if keep else True
        ).delete(synchronize_session=False)
        n["employees"] = s.query(Employee).delete()
        n["memories"] = s.query(Memory).delete()
        n["messages"] = s.query(Message).delete()
        n["sessions"] = s.query(Sess).delete()
        n["escalations"] = s.query(Escalation).delete()
        n["traces"] = s.query(Trace).delete()
        n["push_log"] = s.query(PushLog).delete()
        s.commit()
        return n
    finally:
        s.close()
