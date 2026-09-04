/* 每一页都要用的东西。
 *
 * 拆成多页之后，"当前是谁"不再是内存里的一个变量——每次换页都会重新
 * 问一次 /api/me。这看起来多了一次请求，但换来的是：**页面本身就是权限边界**。
 * 服务端在 /newbie /mentor /hr /ops /admin 上各自做角色检查，
 * 进不去的人在拿到 HTML 之前就被 302 走了。
 */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const el = h => { const d = document.createElement('div'); d.innerHTML = h.trim(); return d.firstElementChild; };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const money = v => '¥' + Number(v || 0).toFixed(4);
const j = (m, b) => ({ method: m, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });

async function api(p, o) {
  const r = await fetch('/api' + p, o);
  if (r.status === 401) { location.href = '/login'; throw new Error('会话已过期'); }
  if (!r.ok) {
    let m = r.statusText;
    try { m = (await r.json()).detail || m; } catch (e) {}
    throw new Error(m);
  }
  return r.status === 204 ? null : r.json();
}

function toast(t) {
  let e = $('#toast');
  if (!e) { e = el('<div class="toast" id="toast"></div>'); document.body.appendChild(e); }
  e.textContent = t; e.classList.add('on');
  clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove('on'), 2600);
}

/* 顶栏。导航只列**当前角色能进的页面**——这份清单来自服务端返回的 views，
   前端不自己猜。 */
const PAGES = [
  ['newbie', '/newbie', '新人工作台'],
  ['mentor', '/mentor', 'Mentor 带教'],
  ['hr', '/hr', 'HR 看板'],
  ['ops', '/ops', '运营与质量'],
  ['admin', '/admin', '管理后台'],
];

async function mount(current) {
  let me;
  try { me = await api('/me'); } catch (e) { return null; }
  const nav = PAGES.filter(([v]) => (me.views || []).includes(v))
    .map(([v, href, label]) =>
      `<a href="${href}"${href === current ? ' aria-current="page"' : ''}>${label}</a>`).join('');
  const hd = el(`<header class="hd">
    <div class="logo"><div class="dot"></div><div><b>爬坡 Ramp</b><em>ONBOARDING AGENT</em></div></div>
    <nav class="nav">${nav}</nav>
    <span class="st" id="hs" style="margin-left:auto"><span class="spin"></span> 连接中…</span>
    <span class="who"><b>${esc(me.display_name)}</b> · ${esc(me.role_label)}
      <button id="lo">登出</button></span>
  </header>`);
  document.body.insertBefore(hd, document.body.firstChild);

  // 演示数据横幅。**不标出来，看的人有理由以为这是真接了企业系统。**
  // 知识库来自虚构公司「云启科技」，员工档案、社保、工单全是造的；
  // 而调用链、成本、检索分数是真的 —— 这两件事必须分清楚，
  // 一个讲隐私和诚实的产品，不该在自己的数据来源上含糊。
  if (me.demo_mode) {
    const b = el('<div class="demobar">' +
      '<b>知识库为虚构</b>' +
      '<span>知识库来自虚构公司「云启科技」—— 这是刻意的：只有自建才能精确控制 ' +
      'L1/L2/L3 分级与有效期，用来验证分级降权是否真的生效。' +
      '<b>除此之外没有虚构</b>：成员、入职信息由管理员录入，' +
      '外部系统（HR / 组织架构 / IT 权限）未接入时工具会明说查不到，不会编。</span>' +
      '</div>');
    document.body.insertBefore(b, hd);
  }
  $('#lo').onclick = async () => {
    try { await api('/logout', { method: 'POST' }); } catch (e) {}
    location.href = '/login';
  };
  api('/health').then(h => {
    $('#hs').innerHTML = `MySQL ${esc(String(h.db[1]).replace('MySQL ', ''))} · 知识 ${h.knowledge} 条 · 工具 ${h.tools} 个`;
  }).catch(e => { $('#hs').innerHTML = `<span class="err">后端异常：${esc(e.message)}</span>`; });
  return me;
}
