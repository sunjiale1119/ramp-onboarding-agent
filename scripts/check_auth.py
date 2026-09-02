"""权限边界端到端验证：注册 → 待激活 → 授权 → 停用 → 越权。

这份脚本存在的理由：**权限是最容易"看起来对但其实没生效"的东西。**
界面藏掉一个按钮，和服务端拒绝返回，在截图上长得一模一样。
所以每一条边界都要有一个会失败的断言，不能靠肉眼看页面。

它抓到过两处真问题：
  · 新人页在调 mentor 的接口挑自己那条（横向越权，单页时代看不出来）
  · 我自己的测试脚本真把演示账号 linxy 删了 —— 删除端点确实有效，
    但也说明**破坏性测试必须用一次性账号**，现在建的是 zhaolei，跑完就删。

先起服务：
    uv run python -m ramp.api            # 默认 8000
再跑：
    uv run python scripts/check_auth.py       # 换端口：... check_auth.py 8021
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
PORT = sys.argv[1] if len(sys.argv) > 1 else os.getenv('RAMP_PORT', '8000')
B = f'http://127.0.0.1:{PORT}'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mk():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj), NoRedirect())


def call(op, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={'Content-Type': 'application/json'})
    try:
        with op.open(r) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            if 'json' not in resp.headers.get('Content-Type', ''):
                return resp.status, {'html': len(raw)}
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw[:100], 'loc': e.headers.get('Location')}


def line(t, code, d):
    msg = d.get('message') or d.get('detail') or d.get('loc') or ''
    if d.get('html'):
        msg = f"拿到页面 {d['html']} 字节"
    print(f'  {t:<40} {code}  {str(msg)[:70]}')


adm = mk()
print('admin 登录:', call(adm, 'POST', '/api/login',
                          {'username': 'admin', 'password': 'ramp2026'})[0])

print()
print('=========== 6. 注册 → 待激活 → 管理员授权 ===========')
line('6a 注册 zhaolei / 赵磊', *call(mk(), 'POST', '/api/register',
     {'username': 'zhaolei', 'password': 'ramp2026x', 'display_name': '赵磊'}))
line('6b 重名注册（应拒）', *call(mk(), 'POST', '/api/register',
     {'username': 'zhaolei', 'password': 'ramp2026x', 'display_name': '赵磊'}))
line('6c 密码太短（应拒）', *call(mk(), 'POST', '/api/register',
     {'username': 'weakpw', 'password': '123', 'display_name': '张三'}))
line('6d 用户名非法字符（应拒）', *call(mk(), 'POST', '/api/register',
     {'username': 'a b!', 'password': 'ramp2026x', 'display_name': '李四'}))
line('6e 没填姓名（应拒）', *call(mk(), 'POST', '/api/register',
     {'username': 'noname1', 'password': 'ramp2026x', 'display_name': '  '}))
line('6f 注册后直接登录（应拒·待激活）', *call(mk(), 'POST', '/api/login',
     {'username': 'zhaolei', 'password': 'ramp2026x'}))

c, d = call(adm, 'GET', '/api/admin/users')
z = [u for u in d['users'] if u['username'] == 'zhaolei']
print(f"  {'6g 管理员看到他':<40} {c}  "
      f"role={z[0]['role']} active={z[0]['active']}" if z else '  6g 没找到')

# 绑定的 id 决定这个账号能读**谁的**提问原文，所以它必须真实存在。
# m_zhaolei 没有任何新人挂在名下 —— 绑上去这人打开工作台就是空的。
line('6h 绑一个不存在的 id（应拒）', *call(adm, 'POST', '/api/admin/users/update',
     {'username': 'zhaolei', 'employee_id': 'm_zhaolei'}))
line('6h2 抢 chenhao 已占的 id（应拒）', *call(adm, 'POST', '/api/admin/users/update',
     {'username': 'zhaolei', 'employee_id': 'm_chenhao'}))
line('6h3 激活 + 派 mentor（不绑档案）', *call(adm, 'POST', '/api/admin/users/update',
     {'username': 'zhaolei', 'active': True, 'role': 'mentor'}))

zop = mk()
c, d = call(zop, 'POST', '/api/login',
            {'username': 'zhaolei', 'password': 'ramp2026x'})
me = d.get('me', {})
print(f"  {'6i 现在能登录了':<40} {c}  "
      f"{me.get('display_name')} · {me.get('role_label')} → {me.get('home')}")
line('6j GET / 跳转', *call(zop, 'GET', '/'))
line('6k 他开 /admin（应被赶走）', *call(zop, 'GET', '/admin'))
line('6l 他开 /mentor（应进得去）', *call(zop, 'GET', '/mentor'))
line('6m 他调管理后台接口（应 403）', *call(zop, 'GET', '/api/admin/users'))

print()
print('=========== 7. 停用 = 立刻踢线，不是等会话过期 ===========')
line('7a 管理员停用他', *call(adm, 'POST', '/api/admin/users/update',
     {'username': 'zhaolei', 'active': False}))
line('7b 他手里那张旧 cookie 打 /api/me', *call(zop, 'GET', '/api/me'))
line('7c 他再开 /mentor', *call(zop, 'GET', '/mentor'))

print()
print('=========== 8. 管理员也做不到的事 ===========')
line('8a 删最后一个管理员（应拒）', *call(adm, 'POST', '/api/admin/users/delete',
     {'username': 'admin'}))
line('8b 停用自己（应拒）', *call(adm, 'POST', '/api/admin/users/update',
     {'username': 'admin', 'active': False}))
line('8c 删自己（应拒）', *call(adm, 'POST', '/api/admin/users/delete',
     {'username': 'admin'}))
line('8d 派一个不存在的角色（应拒）', *call(adm, 'POST', '/api/admin/users/update',
     {'username': 'zhaolei', 'role': 'superuser'}))
line('8e 看新人提问原文（应 403）', *call(adm, 'GET', '/api/newbie/e_linxy/memory'))

print()
print('=========== 9. 水平越权：新人只能读自己的 ===========')
lop = mk()
call(lop, 'POST', '/api/login', {'username': 'linxy', 'password': 'ramp2026'})
line('9a linxy 读自己的记忆', *call(lop, 'GET', '/api/newbie/e_linxy/memory'))
line('9b linxy 读别人的记忆（应 403）',
     *call(lop, 'GET', '/api/newbie/e_zhouyu/memory'))
line('9c linxy 读别人的时间线（应 403）',
     *call(lop, 'GET', '/api/newbie/e_zhouyu/timeline'))
line('9d linxy 读别人的档案（应 403）',
     *call(lop, 'GET', '/api/newbie/e_zhouyu/profile'))
line('9e linxy 调 mentor 接口（应 403）',
     *call(lop, 'GET', '/api/mentor/m_chenhao/mentees'))
line('9f linxy 调 HR 看板（应 403）', *call(lop, 'GET', '/api/hr/dashboard'))

print()
print('清理：')
line('删除 zhaolei', *call(adm, 'POST', '/api/admin/users/delete',
                           {'username': 'zhaolei'}))
