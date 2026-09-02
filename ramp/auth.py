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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Boolean, DateTime, String, func
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
    employee_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """newbie 角色绑定到哪位员工；mentor 绑定到他带的人由 mentor_id 反查。"""

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
    employee_id: str | None

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
        return Principal(u.username, u.display_name, u.role, u.employee_id)
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
                   employee_id=None, salt=salt, pwd_hash=h, active=False))
        s.commit()
        return True, "注册成功，等待管理员激活并分配角色"
    finally:
        s.close()


def list_users() -> list[dict[str, Any]]:
    """账号清单。**带上绑定的那份档案是谁**——这一栏不是装饰。

    只显示一个 `e_zhouyu` 的话，管理员没法判断绑对没有；
    显示成 `e_zhouyu · 孙佳乐`，"某人的账号绑到另一个人档案上"这种事
    一眼就能看出来。**能算出来的信息不该让人去脑补。**
    """
    from .db import Employee

    s = get_session()
    try:
        rows = s.query(Employee).all()
        emps = {e.id: e for e in rows}
        # mentor 不在 employees 表里，只以 mentor_id 存在 —— 单独建一张映射
        mentors: dict[str, tuple[str, int]] = {}
        for e in rows:
            if e.mentor_id:
                name, n = mentors.get(e.mentor_id, (e.mentor_name or e.mentor_id, 0))
                mentors[e.mentor_id] = (name, n + 1)

        out = []
        for u in s.query(User).order_by(User.created_at.desc()).all():
            e = emps.get(u.employee_id) if u.employee_id else None
            m = mentors.get(u.employee_id) if (u.employee_id and e is None) else None
            out.append({
                "username": u.username, "display_name": u.display_name,
                "role": u.role, "role_label": ROLE_LABEL.get(u.role, u.role),
                "employee_id": u.employee_id, "active": bool(u.active),
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "employee_name": e.name if e else (m[0] if m else None),
                "employee_team": (f"{e.team} · {e.role}" if e
                                  else (f"带 {m[1]} 位新人" if m else None)),
                # id 填了却哪儿都查不到 → 这个账号打不开工作台
                "binding_missing": bool(u.employee_id and e is None and m is None),
                # 账号名和档案名对不上 → 可能绑错人了，只提醒不拦
                "name_mismatch": bool((e and e.name != u.display_name)
                                      or (m and m[0] != u.display_name)),
            })
        return out
    finally:
        s.close()


def _check_binding(s, username: str, employee_id: str | None) -> tuple[bool, str]:
    """账号 ↔ 员工档案的绑定校验。

    ## 为什么这件事不能是个自由文本框

    绑定关系决定了这个账号能读**谁的**提问原文。`own_employee()` 那道
    横向越权防线比对的就是它——所以绑错之后，那道防线不但不报警，
    还会一本正经地放行错误的数据。**权限检查只能保证"符合绑定"，
    保证不了"绑定是对的"。**

    我自己就在管理后台手滑过一次：验证注册流程时随手填了一个 id，
    于是新注册的账号绑到了**另一个人的档案**上，从此能读那个人的记忆。
    当时前端没有任何提示——填什么收什么，连"这个 id 是谁"都不显示。

    一个承诺"没有人能看到你的提问原文"的产品，
    不该让管理员在一个空输入框里手滑就把它破掉。

    三条规则：档案必须存在、不能一档双号、名字对不上要说出来
    （只提醒不拦——现实里同一个人确实可能账号名和花名不一致）。
    """
    from .db import Employee

    if not employee_id:
        return True, ""

    # 唯一性**先查，且对两种 id 一视同仁**。
    # 第一版把这段写在"新人档案"分支里，mentor 分支漏了——
    # 于是 HR 账号能把自己绑到 m_chenhao 上，跟着陈昊的新人一起走。
    # 这类漏洞的形状很固定：**规则写在分支里，就会漏掉后加的那个分支。**
    other = (s.query(User)
             .filter(User.employee_id == employee_id, User.username != username)
             .first())
    if other is not None:
        return False, (f"{employee_id} 已经绑给账号 {other.username} 了。"
                       "一个 id 只能属于一个账号——否则两个人会读到同一份数据")

    if s.get(Employee, employee_id) is not None:
        return True, ""

    # mentor 的 id **不在 employees 表里**。那张表只存新人，
    # mentor 是以 `employees.mentor_id` 这个字段存在的（m_chenhao 带着
    # e_linxy 和 e_zhouyu，但他自己没有一行记录）。
    #
    # 这条分支是我这次校验自己的 bug 逼出来的：第一版只查 Employee 主键，
    # 于是把**每一个 mentor 账号都判成"查无档案"**——管理员点 chenhao
    # 那行的保存会被直接拒掉。**校验写得太紧和写得太松一样是 bug**，
    # 区别只在于前者会当场炸给你看。
    if s.query(Employee).filter(Employee.mentor_id == employee_id).first():
        return True, ""

    return False, (f"没有 {employee_id} 这个 id——"
                   "既不是员工档案，也没有任何新人挂在它名下。"
                   "绑上去这个账号会一直打不开工作台")


def employee_brief(employee_id: str | None) -> dict[str, Any] | None:
    """档案摘要，给管理后台显示"你正在绑的是谁"。"""
    if not employee_id:
        return None
    from .db import Employee

    s = get_session()
    try:
        e = s.get(Employee, employee_id)
        if e is not None:
            return {"id": e.id, "kind": "newbie", "name": e.name, "team": e.team,
                    "role": e.role, "mentor_name": e.mentor_name}
        led = s.query(Employee).filter(Employee.mentor_id == employee_id).all()
        if led:
            return {"id": employee_id, "kind": "mentor",
                    "name": led[0].mentor_name or employee_id,
                    "mentees": [x.name for x in led]}
        return {"id": employee_id, "missing": True}
    finally:
        s.close()


def update_user(username: str, *, role: str | None = None,
                active: bool | None = None, employee_id: str | None = ...,
                new_password: str | None = None) -> tuple[bool, str]:
    s = get_session()
    try:
        u = s.get(User, username)
        if u is None:
            return False, "用户不存在"
        if role is not None:
            if role not in ROLES:
                return False, f"角色非法：{role}"
            u.role = role
        if active is not None:
            u.active = active
            if not active:
                # 停用要**同时踢掉在线会话**，否则那个人还能用到过期为止
                s.query(Session_).filter(Session_.username == username).delete()
        if employee_id is not ...:
            ok, msg = _check_binding(s, username, employee_id or None)
            if not ok:
                return False, msg
            u.employee_id = employee_id or None
        if new_password:
            if len(new_password) < 8:
                return False, "密码至少 8 位"
            u.salt, u.pwd_hash = hash_password(new_password)
            s.query(Session_).filter(Session_.username == username).delete()
        s.commit()
        return True, "已更新"
    finally:
        s.close()


def delete_user(username: str) -> tuple[bool, str]:
    s = get_session()
    try:
        u = s.get(User, username)
        if u is None:
            return False, "用户不存在"
        if u.role == "admin" and s.query(User).filter(User.role == "admin",
                                                      User.active.is_(True)).count() <= 1:
            return False, "不能删除最后一个管理员——那会把自己锁在门外"
        s.query(Session_).filter(Session_.username == username).delete()
        s.delete(u)
        s.commit()
        return True, "已删除"
    finally:
        s.close()


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
DEMO_PASSWORD = "ramp2026"

DEMO_USERS = [
    ("linxy",   "林小雨", "newbie", "e_linxy"),
    ("chenhao", "陈昊",   "mentor", "m_chenhao"),
    ("wangqian", "王倩",  "hr",     None),
    ("ops",     "运营",   "ops",    None),
    ("admin",   "管理员", "admin",  None),
]


def seed_users(password: str = DEMO_PASSWORD) -> int:
    """种演示账号。已存在的跳过，不覆盖已改过的密码。"""
    s = get_session()
    try:
        n = 0
        for username, name, role, emp in DEMO_USERS:
            if s.get(User, username):
                continue
            salt, h = hash_password(password)
            s.add(User(username=username, display_name=name, role=role,
                       employee_id=emp, salt=salt, pwd_hash=h))
            n += 1
        s.commit()
        return n
    finally:
        s.close()
