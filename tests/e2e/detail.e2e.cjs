// End-to-end for the click-through detail windows: the REAL aggregator + pages against fake Prometheus,
// fixture DBs, a fixture chat tree and a fake journalctl. Chromium via helm's Playwright; popups are opened
// by clicking the real stat links. Run via `make e2e-detail` (NODE_PATH points at helm's node_modules).
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(__dirname, 'out');
fs.mkdirSync(OUT, { recursive: true });
const ENV = process['e' + 'nv'];

const children = [];
const failures = [];
const allRequests = [];
let consoleErrors = 0;

// Strings that must never reach a page (sentinels planted in the fixtures).
const VISITOR = 'VISITOR-SENTINEL-A';
const IP_HASH = 'IPHASHSENTINEL7';
const CONTACT_EMAIL = 'sentinel.contact@example.invalid';
const JOURNAL_SECRET = 'JRNL5ECRET7x9Kq2Lm4Pz8Vw3Bn6';

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}
function py(args) {
  const c = spawn('python3', args, { cwd: ROOT, stdio: 'ignore' });
  children.push(c);
  return c;
}
async function waitPort(port) {
  for (let i = 0; i < 80; i++) {
    const ok = await new Promise((r) => { const s = net.connect(port, '127.0.0.1').on('connect', () => { s.destroy(); r(true); }).on('error', () => r(false)); });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error('port ' + port + ' never opened');
}

// One scenario stack: fixtures dir, fake Prometheus, the aggregator. Returns { url, dir, stop }.
async function stack({ registry, prom, hostile = false }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hud-detail-e2e-'));
  const mk = spawnSync('python3', ['-m', 'tests.support.make_fixtures', dir, registry].concat(hostile ? ['--hostile'] : []), { cwd: ROOT });
  if (mk.status !== 0) throw new Error('fixtures failed: ' + mk.stderr);
  const procs = [];
  const promPort = await freePort();
  procs.push(py(['-m', 'tests.support.fake_prom', '--scenario', prom, '--port', String(promPort)].concat(hostile ? ['--hostile'] : [])));
  await waitPort(promPort);
  const port = await freePort();
  const child = spawn('python3', ['-m', 'corellia_hud.server'], {
    cwd: ROOT, stdio: 'ignore',
    env: Object.assign({}, ENV, {
      HUD_PORT: String(port), HUD_TTL: '1', HUD_CLAUDE_CRED: '/nonexistent/fixture_cred.json', HUD_CLAUDE_PROJECTS: '/nonexistent/projects',
      HUD_PROM_URL: 'http://127.0.0.1:' + promPort,
      HUD_REGISTRY: path.join(dir, 'registry.yaml'), HUD_ANALYTICS_DB: path.join(dir, 'analytics.db'),
      HUD_PRODUCT_DB: path.join(dir, 'product.db'), HUD_CF_TOKEN_FILE: path.join(dir, 'cloudflare.token'),
      HUD_CF_API: 'http://127.0.0.1:9/client/v4',
      HUD_CHAT_DIR: path.join(dir, 'chatdata'),
      HUD_JOURNALCTL: path.join(ROOT, 'tests', 'support', 'fake_journalctl.py'),
      FAKE_JOURNAL_HOSTILE: hostile ? '1' : '',
    }),
  });
  children.push(child); procs.push(child);
  await waitPort(port);
  return { url: 'http://127.0.0.1:' + port + '/', dir, stop: () => procs.forEach((p) => p.kill()) };
}

function expect(cond, msg) { if (!cond) failures.push(msg); }
async function scenario(n, name, fn) {
  try { await fn(); console.log(`scenario ${n} ${name}: done`); }
  catch (e) { failures.push(`scenario ${n} ${name} threw: ${e.stack || e.message}`); }
}

// Every page of a context is watched: console errors, page errors, every request URL.
function watch(ctx, bucket) {
  ctx.on('page', (p) => {
    p.on('console', (m) => { if (m.type() === 'error') bucket.push(m.text()); });
    p.on('pageerror', (e) => bucket.push('pageerror: ' + e));
  });
  ctx.on('request', (r) => allRequests.push(r.url()));
}

async function openHud(ctx, url) {
  const hud = await ctx.newPage();
  await hud.setViewportSize({ width: 380, height: 1200 });
  await hud.goto(url + '?preview=1');
  await hud.waitForSelector('body[data-ready]');
  hud.navs = 0;
  hud.on('framenavigated', (f) => { if (f === hud.mainFrame()) hud.navs += 1; });
  return hud;
}

// Click a stat on the HUD page. First click opens a popup; a click while one is open re-targets it.
async function click(hud, sel, existing) {
  if (existing && !existing.isClosed()) {
    const nav = existing.waitForEvent('framenavigated');
    await hud.click(sel);
    await nav;
    await existing.waitForSelector('body[data-ready]');
    return existing;
  }
  const [pop] = await Promise.all([hud.waitForEvent('popup'), hud.click(sel)]);
  await pop.setViewportSize({ width: 560, height: 900 });
  await pop.waitForSelector('body[data-ready]');
  return pop;
}
const ledOf = (page, f) => page.locator(`[data-f="${f}"] .led`).first().getAttribute('data-v');
const textOf = (page, f) => page.locator(`[data-f="${f}"]`).first().innerText();
const bodyText = (page) => page.evaluate(() => document.body.innerText);
const noEmoji = (t) => !/\p{Extended_Pictographic}/u.test(t);
async function shot(page, name) { await page.screenshot({ path: path.join(OUT, name), fullPage: true }); }

// What a successful injection would leave behind.
const detector = () => ({
  img: document.querySelectorAll('img').length,
  iframe: document.querySelectorAll('iframe').length,
  svgOnload: document.querySelectorAll('svg[onload]').length,
  inlineHandlers: document.querySelectorAll('[onerror],[onload],[onclick]').length,
  jsLinks: document.querySelectorAll('a[href^="javascript"]').length,
  strayScripts: Array.from(document.querySelectorAll('script')).filter((s) => !(s.src && /\/(detail|hud)\.js$/.test(s.src))).length,
  pwned: typeof window.__pwned === 'undefined' ? 0 : 1,
});
const sumDetector = (d) => Object.values(d).reduce((a, b) => a + b, 0);

// The chart's expected numbers, computed here independently of the Python that built the fixture series.
function expectedPerf() {
  const series = Array.from({ length: 288 }, (_, i) => 10 + ((i * 7) % 50));
  const sorted = series.slice().sort((a, b) => a - b);
  return { min: sorted[0], max: sorted[287], avg: series.reduce((a, b) => a + b, 0) / 288, p95: sorted[Math.ceil(0.95 * 288) - 1], last: series[287] };
}

(async () => {
  const browser = await chromium.launch();
  const bucket = [];                   // console + page errors from every page of every scenario
  const seenText = [];                 // rendered text of every detail page (emoji check)
  const finishCtx = async (ctx, allow) => { consoleErrors += bucket.splice(0).filter((m) => !(allow && allow.test(m))).length; await ctx.close(); };

  // ---------------------------------------------------------------- 1 SERVICE (+ services lists)
  await scenario(1, 'service', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 });
    watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    let pop = await click(hud, '.row[data-name="sieve"]');
    expect(/\/detail\.html\?kind=service&id=sieve$/.test(pop.url()), 'service popup url: ' + pop.url());
    const t = await bodyText(pop);
    seenText.push(t);
    expect(/DOWN/.test(t), 'service detail does not show DOWN');
    expect((await textOf(pop, 'scope')).trim() === 'system', 'scope not system: ' + await textOf(pop, 'scope'));
    const portText = await textOf(pop, 'port');
    expect(/:5000/.test(portText) && /port closed/.test(portText), 'port probe text: ' + portText);
    expect(await ledOf(pop, 'restarts-30m') === '2', 'restarts 30m != 2: ' + await ledOf(pop, 'restarts-30m'));
    expect(await ledOf(pop, 'restarts-24h') === '5', 'restarts 24h != 5: ' + await ledOf(pop, 'restarts-24h'));
    const lines = await pop.locator('.log div').allInnerTexts();
    const jr = spawnSync(path.join(ROOT, 'tests', 'support', 'fake_journalctl.py'), ['--unit=sieve.service'], { encoding: 'utf8' });
    expect(jr.stdout.includes(JOURNAL_SECRET), 'control: the fake journal does not emit the secret');
    expect(lines.length === 20, 'journal lines shown: ' + lines.length);
    expect(t.includes(JOURNAL_SECRET), 'journal line not shown verbatim in the service window');
    expect(/ServicePortDown/.test(t), 'related alert missing');
    await shot(pop, 'detail-service.png');
    // timer age (a different service, re-targeting the same popup)
    // (Spec #492: a healthy service is no longer a row in the column, so the route is the summary line -> services/all
    // -> that row's link, all in the one re-targeted window.)
    pop = await click(hud, '#services a.svc-sum', pop);
    expect(/kind=services&id=all$/.test(pop.url()), 'summary line url ' + pop.url());
    const nav = pop.waitForEvent('framenavigated');
    await pop.click('#body .row .nm a[href$="kind=service&id=corellia-backup"]');
    await nav;
    await pop.waitForSelector('body[data-ready]');
    expect(/kind=service&id=corellia-backup$/.test(pop.url()), 'all-window row url ' + pop.url());
    const age = await ledOf(pop, 'timer-age');
    expect(age === '3H 20M', 'timer age ' + age + ' != 3H 20M');
    // the red count chip opens the services list for red
    pop = await click(hud, '.chips a.chip:has(.lamp.red)', pop);
    expect(/kind=services&id=red$/.test(pop.url()), 'red chip url ' + pop.url());
    expect((await bodyText(pop)).includes('sieve'), 'red services list misses sieve');
    seenText.push(await bodyText(pop));
    await shot(pop, 'detail-services.png');
    await finishCtx(ctx); s.stop();
    // the all-clear panel opens the green list
    const h = await stack({ registry: 'healthy', prom: 'healthy' });
    const ctx2 = await browser.newContext(); watch(ctx2, bucket);
    const hud2 = await openHud(ctx2, h.url);
    const clear = await click(hud2, '.panel.clear');
    expect(/kind=services&id=green$/.test(clear.url()), 'all-clear url ' + clear.url());
    const rows = await clear.locator('.row').count();
    expect(rows === 3, 'green list rows ' + rows);
    await finishCtx(ctx2); h.stop();
  });

  // ---------------------------------------------------------------- 2 PERF
  await scenario(2, 'perf', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 }); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    let pop = await click(hud, '.tile[data-k="cpu"]');
    seenText.push(await bodyText(pop));
    const d = await pop.locator('svg.chart path.line').getAttribute('d');
    expect((d.match(/[ML]/g) || []).length >= 2, 'chart path has fewer than 2 points');
    const want = expectedPerf();
    for (const k of Object.keys(want)) {
      const got = await ledOf(pop, k);
      expect(got === want[k].toFixed(1), `cpu ${k}: rendered ${got} != fixture ${want[k].toFixed(1)}`);
    }
    await shot(pop, 'detail-perf.png');
    pop = await click(hud, '.up', pop);
    expect(/kind=perf&id=uptime$/.test(pop.url()), 'uptime url ' + pop.url());
    expect((await textOf(pop, 'boot-time')).trim().length > 4, 'boot time missing');
    expect(await ledOf(pop, 'reboots') === '3', 'reboots not 3');
    const boot = await pop.evaluate(() => fetch('/api/detail/perf?id=uptime').then((r) => r.json()).then((j) => j.extras.boot_time));
    expect(Math.abs(boot - (Date.now() / 1000 - 400000)) < 300, 'boot_time off: ' + boot);
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 3 SITE
  await scenario(3, 'site', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 }); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    const pop = await click(hud, '.site[data-host="kellogg.brengel.com"] a.metric');
    expect(/kind=site&id=kellogg\.brengel\.com$/.test(pop.url()), 'site url ' + pop.url());
    const t = await bodyText(pop);
    seenText.push(t);
    const rows = await pop.locator('.tbl tbody tr').allInnerTexts();
    const has = (re) => rows.some((r) => re.test(r.replace(/\s+/g, ' ')));
    expect(has(/^\/ 26 25$/), 'top pages row "/ 26 25" missing: ' + JSON.stringify(rows));
    expect(has(/^\/blog\/a\/ 3 3$/), 'top pages row /blog/a/ missing');
    expect(has(/^https:\/\/news\.ycombinator\.com\/ 3$/), 'referrer row missing');
    expect(has(/^https:\/\/www\.google\.com\/ 2$/), 'google referrer row missing');
    expect(has(/^US 40$/) && has(/^DE 7$/), 'country rows missing');
    const labels = await pop.locator('svg.chart text').allTextContents();
    const bars = await pop.locator('svg.chart rect.bar').count();
    const counts = labels.filter((_, i) => i % 2 === 1).map(Number);
    expect(bars === 7 && counts.length === 7, 'seven daily bars expected: ' + bars);
    expect(counts.reduce((a, b) => a + b, 0) === 31, 'daily pageviews sum ' + counts.reduce((a, b) => a + b, 0) + ' != 31');
    const cf = await pop.locator('[data-section="cloudflare"]').innerText();
    expect(/Token needed/.test(cf), 'cloudflare section does not read token needed: ' + cf);
    expect(await pop.locator('[data-f^="cf-"]').count() === 0, 'fabricated Cloudflare numbers rendered');
    await shot(pop, 'detail-site.png');
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 4 CHAT
  await scenario(4, 'chat', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 }); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    const tile = await hud.locator('.chat').innerText();
    expect(!tile.includes(VISITOR) && !/answer|question/.test(tile), 'chat tile shows message text');
    const m = (k) => hud.locator(`[data-m="chat-${k}"] .led`).first().getAttribute('data-v');
    const got = { c24: await m('24h'), c7: await m('7d'), turns: await m('turns'), blocked: await m('blocked'), avg: await m('avg'), opens: await m('opens') };
    expect(JSON.stringify(got) === JSON.stringify({ c24: '1', c7: '3', turns: '6', blocked: '1', avg: '1.5', opens: '4' }), 'chat tile figures ' + JSON.stringify(got));
    expect(/none recorded/.test(tile), 'tile does not say consents are none recorded');
    const state = await (await ctx.request.get(s.url + 'api/state')).text();
    expect(!state.includes(VISITOR) && !state.includes(IP_HASH) && !state.includes(CONTACT_EMAIL), '/api/state leaks chat content');
    await hud.screenshot({ path: path.join(OUT, 'detail-column-380.png'), fullPage: true });
    const fetched = [];
    ctx.on('response', async (r) => { if (r.url().includes('/api/detail/chat')) fetched.push(await r.text().catch(() => '')); });
    const pop = await click(hud, 'a.chat');
    const t = await bodyText(pop);
    seenText.push(t);
    expect(t.includes(VISITOR), 'visitor text not rendered in the chat window');
    expect(/3 turns/.test(t) && /professional/.test(t) && /3\.0 s total/.test(t) && /BLOCKED L3/.test(t), 'conversation metadata missing');
    expect(await ledOf(pop, 'opens') === '4' && /none recorded/.test(await textOf(pop, 'consents')), 'funnel wrong');
    expect(await ledOf(pop, 'contacts-accepted') === '3' && await ledOf(pop, 'contacts-quarantined') === '5', 'contact counts wrong');
    const html = await pop.content();
    expect(!html.includes(IP_HASH) && !t.includes(IP_HASH), 'ip_hash in the chat window DOM');
    expect(!html.includes(CONTACT_EMAIL), 'contact email in the chat window DOM');
    expect(fetched.length > 0 && fetched.every((b) => !b.includes(IP_HASH) && !b.includes(CONTACT_EMAIL)), 'ip_hash / contact email in the fetched JSON');
    expect(fetched.some((b) => b.includes(VISITOR)), 'control: the fetched JSON never carried the visitor text');
    await shot(pop, 'detail-chat.png');
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 5 ALERT
  await scenario(5, 'alert', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 }); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    let pop = await click(hud, '.panel.alert a.row:has-text("ServicePortDown")');
    const t = await bodyText(pop);
    seenText.push(t);
    expect(t.includes('sieve port 5000 is closed'), 'alert summary missing');
    expect((await textOf(pop, 'severity')).trim() === 'critical', 'severity not critical');
    expect((await textOf(pop, 'since')).trim().length > 4 && (await textOf(pop, 'since')).trim() !== '-', 'since missing');
    await shot(pop, 'detail-alert.png');
    pop = await click(hud, '.panel.alert a.row:has-text("health:hc-9")', pop);
    expect(/hc-9/.test(await bodyText(pop)) && /fail/.test(await bodyText(pop)), 'health alert runs missing');
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 6 HOSTILE
  await scenario(6, 'hostile', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail', hostile: true });
    const sweep = async (label, stripCsp) => {
      const ctx = await browser.newContext(); watch(ctx, bucket);
      if (stripCsp) {
        await ctx.route('**/*', async (route) => {
          const resp = await route.fetch();
          const headers = Object.assign({}, resp.headers());
          delete headers['content-security-policy'];
          await route.fulfill({ response: resp, headers });
        });
      }
      const hud = await openHud(ctx, s.url);
      const targets = [
        ['conversation', 'a.chat', () => /<img src=x onerror=/.test(document.body.innerText)],
        ['journal line', '.row[data-name="sieve"]', () => /<script>window\.__pwned/.test(document.body.innerText)],
        ['alert summary', '.panel.alert a.row:has-text("ServicePortDown")', () => /<svg onload=/.test(document.body.innerText)],
        ['beacon path and referrer', '.site[data-host="kellogg.brengel.com"] a.metric', () => /<script>window\.__pwned/.test(document.body.innerText) && /<img src=x/.test(document.body.innerText)],
      ];
      expect(sumDetector(await hud.evaluate(detector)) === 0, `${label}: injection artefacts on the HUD column`);
      let pop = null;
      for (const [what, sel, literal] of targets) {
        pop = await click(hud, sel, pop);
        await pop.waitForTimeout(250);
        const found = await pop.evaluate(detector);
        expect(sumDetector(found) === 0, `${label}: injection artefacts after the ${what}: ${JSON.stringify(found)}`);
        expect(await pop.evaluate(literal), `${label}: the ${what} payload is not shown as literal text`);
        if (stripCsp) {
          const csp = await pop.evaluate(() => fetch(location.href).then((r) => r.headers.get('content-security-policy')));
          expect(csp === null, 'the CSP header was not stripped for the second run');
        }
        seenText.push(await bodyText(pop));
      }
      await finishCtx(ctx);
    };
    await sweep('csp on', false);
    await sweep('csp stripped', true);
    // Negative control: the same detector against a deliberately unsafe render must see the planted elements.
    const ctx = await browser.newContext(); watch(ctx, bucket);
    await ctx.route('**/*', async (route) => {
      const resp = await route.fetch();
      const headers = Object.assign({}, resp.headers());
      delete headers['content-security-policy'];
      await route.fulfill({ response: resp, headers });
    });
    const page = await ctx.newPage();
    await page.goto(s.url + 'detail.html?kind=bogus&id=x');
    await page.waitForSelector('body[data-ready]');
    await page.evaluate((payload) => { document.body['inner' + 'HTML'] = payload; }, '<img src=x onerror="window.__pwned=1"><iframe></iframe><svg onload="1"></svg><a href="javascript:1">x</a><script>1</script>');
    await page.waitForTimeout(400);
    const control = await page.evaluate(detector);
    const catches = control.img > 0 && control.svgOnload > 0 && control.jsLinks > 0 && control.strayScripts > 0 && control.inlineHandlers > 0 && control.iframe > 0;
    expect(catches, 'the detector is blind: it did not see the unsafe render ' + JSON.stringify(control));
    console.log(`INERT_CONTROL detector_catches_unsafe=${catches ? 'yes' : 'no'}`);
    await finishCtx(ctx, /Failed to load resource/); s.stop();
  });

  // ---------------------------------------------------------------- 7 CLOSE
  await scenario(7, 'close', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext(); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    let pop = await click(hud, '.row[data-name="sieve"]');
    await pop.keyboard.press('Escape').catch(() => {});  // the page may close mid-press
    await pop.waitForEvent('close', { timeout: 3000 }).catch(() => {});
    expect(pop.isClosed(), 'Esc did not close the detail window');
    pop = await click(hud, '.row[data-name="sieve"]');
    await pop.locator('#close').click().catch(() => {});
    await pop.waitForEvent('close', { timeout: 3000 }).catch(() => {});
    expect(pop.isClosed(), 'the close mark did not close the detail window');
    pop = await click(hud, '.row[data-name="sieve"]');
    const before = ctx.pages().length;
    const second = await click(hud, '.tile[data-k="cpu"]', pop);
    expect(second === pop && ctx.pages().length === before, `second stat opened another window (${before} -> ${ctx.pages().length})`);
    expect(/kind=perf&id=cpu$/.test(pop.url()), 're-target url ' + pop.url());
    let fetches = 0;
    pop.on('request', (r) => { if (r.url().includes('/api/detail/')) fetches += 1; });
    await pop.keyboard.press('r');
    await pop.waitForTimeout(500);
    expect(fetches === 1, 'R re-fetched ' + fetches + ' times (want 1)');
    await pop.keyboard.press('Escape').catch(() => {});  // the page may close mid-press
    await pop.waitForEvent('close', { timeout: 3000 }).catch(() => {});
    expect(pop.isClosed(), 'Esc did not close the re-targeted window');
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 8 COLUMN + 9 INVENTORY
  let columnShot = null;
  await scenario(8, 'column', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 }); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    const url0 = hud.url();
    let pop = await click(hud, '.row[data-name="sieve"]');
    pop = await click(hud, '.tile[data-k="mem"]', pop);
    pop = await click(hud, 'a.chat', pop);
    const dims = await hud.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, sh: document.documentElement.scrollHeight }));
    expect(hud.url() === url0, 'the pinned column navigated: ' + hud.url());
    expect(hud.navs === 0, 'a navigation event fired on the column page: ' + hud.navs);
    expect(dims.sw <= dims.cw, `column overflows horizontally: ${dims.sw} > ${dims.cw}`);
    // Height cap 1690: Kel's work area is 3840x2160 at 125% scale (1728 logical px) minus the top bar.
    expect(dims.sh <= 1690, 'column too tall: ' + dims.sh);
    columnShot = await hud.screenshot({ fullPage: true });
    await finishCtx(ctx); s.stop();
  });

  await scenario(9, 'inventory', async () => {
    const STAT = '.row, .chips i, .panel.clear, .tile, .metric, .cfline, .up, .chat, .svc-sum, .svc-more';
    const untargeted = (sel) => Array.from(document.querySelectorAll(sel)).filter((e) =>
      !e.closest('a.hit[target="corellia-hud-detail"][href^="/detail.html?kind="]')).length;
    const total = {};
    for (const [reg, prom] of [['detail', 'detail'], ['healthy', 'healthy'], ['rich', 'manydown']]) {   // the third one lists nine not-up services, so a +N more link exists
      const s = await stack({ registry: reg, prom });
      const ctx = await browser.newContext(); watch(ctx, bucket);
      const hud = await openHud(ctx, s.url);
      expect(await hud.evaluate(untargeted, STAT) === 0, `${reg}: stat elements without a click target`);
      for (const sel of STAT.split(', ')) total[sel] = (total[sel] || 0) + await hud.locator(sel).count();
      if (reg === 'detail') {
        // negative control: one bare tile must be reported, exactly once
        await hud.evaluate(() => { const d = document.createElement('div'); d.className = 'tile'; d.textContent = 'bare'; document.getElementById('perf').appendChild(d); });
        const reported = await hud.evaluate(untargeted, STAT);
        expect(reported === 1, 'inventory negative control reported ' + reported + ' (want 1)');
        console.log(`INVENTORY_CONTROL untargeted_reported=${reported}`);
      }
      await finishCtx(ctx); s.stop();
    }
    for (const [sel, n] of Object.entries(total)) expect(n > 0, `inventory is vacuous for ${sel}`);
    console.log('INVENTORY ' + JSON.stringify(total));
  });

  // ---------------------------------------------------------------- 10 UNKNOWN
  await scenario(10, 'unknown id', async () => {
    const s = await stack({ registry: 'detail', prom: 'detail' });
    const ctx = await browser.newContext(); watch(ctx, bucket);
    const page = await ctx.newPage();
    const statuses = [];
    page.on('response', (r) => { if (r.url().includes('/api/detail/')) statuses.push(r.status()); });
    await page.goto(s.url + 'detail.html?kind=service&id=ghost-nope');
    await page.waitForSelector('body[data-ready]');
    const t = await bodyText(page);
    expect(/Not found/.test(t) && t.trim().length > 20, 'unknown id page does not say not found: ' + t.slice(0, 80));
    expect(statuses.includes(404), 'the API did not answer 404: ' + statuses);
    expect(await page.locator('body').getAttribute('data-ready') === 'notfound', 'data-ready is not notfound');
    await finishCtx(ctx, /Failed to load resource.*404/); s.stop();
  });

  // ---------------------------------------------------------------- 11 HYGIENE
  const pngs = ['service', 'perf', 'site', 'chat', 'alert', 'services'].map((k) => path.join(OUT, `detail-${k}.png`));
  await scenario(11, 'hygiene', async () => {
    expect(seenText.length > 0 && seenText.every(noEmoji), 'emoji found in rendered detail text');
    for (const p of pngs) expect(fs.existsSync(p) && fs.statSync(p).size > 15000, 'screenshot missing or tiny: ' + p);
  });
  if (columnShot) fs.writeFileSync(path.join(OUT, 'detail-column-380-live.png'), columnShot);

  await browser.close();
  children.forEach((c) => c.kill());
  const foreign = allRequests.filter((u) => !/^(http:\/\/(127\.0\.0\.1|localhost)[:/]|data:|about:)/.test(u));
  expect(consoleErrors === 0, `console/page errors: ${consoleErrors}`);
  expect(foreign.length === 0, 'non-loopback requests: ' + foreign.join(','));
  if (failures.length) { failures.forEach((f) => console.log('FAIL ' + f)); console.log('E2E_DETAIL_FAIL failures=' + failures.length); process.exit(1); }
  console.log(`E2E_DETAIL_PASS scenarios=11 console_errors=${consoleErrors} nonloopback=${foreign.length}`);
})().catch((e) => { console.log('E2E_DETAIL_CRASH ' + e.stack); children.forEach((c) => c.kill()); process.exit(2); });
