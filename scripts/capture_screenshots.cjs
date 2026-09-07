// Start preview_screenshots.py first. Requires Playwright (or pass its module path).
const { chromium } = require(process.argv[2] || 'playwright');
const path = require('path');
const fs = require('fs');
const out = path.resolve(__dirname, '../docs/screenshots/latest');
const base = 'http://127.0.0.1:8766';
(async () => {
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [];
  async function shot(page, name) {
    await page.waitForLoadState('networkidle');
    await page.screenshot({path: path.join(out, name + '.png'), fullPage: true});
    console.log(name);
  }
  async function login(username) {
    const ctx = await browser.newContext({viewport: {width: 1440, height: 1000}, deviceScaleFactor: 1});
    const res = await ctx.request.post(base + '/api/login', {data: {username, password:'ramp2026'}});
    if (!res.ok()) throw new Error('Login failed: ' + username);
    const page = await ctx.newPage();
    page.on('pageerror', e => errors.push(e.message));
    return page;
  }
  try {
    const publicPage = await browser.newPage({viewport:{width:1440,height:1000}});
    await publicPage.goto(base + '/login');
    await shot(publicPage, '01-login');
    await publicPage.goto(base + '/register');
    await shot(publicPage, '02-register');
    for (const [user, route, name] of [
      ['demo_newbie','newbie','03-newbie'], ['demo_mentor','mentor','04-mentor'],
      ['demo_hr','hr','05-hr'], ['demo_itdesk','ops','06-ops']]) {
      const p = await login(user);
      await p.goto(base + '/' + route);
      await shot(p, name);
    }
    const p = await login('admin');
    await p.goto(base + '/admin');
    await p.locator('#u-tab [data-act="edit"]').first().waitFor();
    await shot(p, '07-admin-members');
    await p.locator('[data-u="demo_newbie"] [data-act="edit"]').click();
    await shot(p, '08-admin-member-edit');
    await p.locator('#m-cancel').click();
    await p.locator('button[data-pane="x"]').click();
    await p.locator('#x-contacts select').first().waitFor();
    await shot(p, '09-admin-external');
    await p.locator('button[data-pane="k"]').click();
    await shot(p, '10-admin-knowledge');
    if (errors.length) throw new Error('Browser errors: ' + errors.join('; '));
    console.log('10 screenshots captured; no browser JavaScript errors.');
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode=1;});
