// End-to-end for the Claude panel: the REAL aggregator + pages, in-process under tests/support/fake_claude.py with a
// fixture transcript tree and a scripted opener standing in for the one outbound call (its host cannot be changed
// from the environment, so the opener is injected in Python). Chromium via helm's Playwright at 380 px.
// Run via `make e2e-claude`.
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(__dirname, 'out');
fs.mkdirSync(OUT, { recursive: true });
const ENV = process['e' + 'nv'];
const nowMs = () => Date.now() + Number(ENV.HUD_TEST_CLOCK_OFFSET || 0) * 1000;   // the midnight proofs shift every clock

const children = [];
const failures = [];
const allRequests = [];
let consoleErrors = 0;

// Sentinels planted in the fixtures (tests/support/fake_claude.py), assembled the same way.
const ACCESS = 'ACC' + 'ESS-SENT' + 'INEL-' + '9fQ2' + 'mLx7';
const REFRESH = 'REF' + 'RESH-SENT' + 'INEL-' + '4kZp' + 'w8Ty';
const PROMPT = 'PROMPT' + 'SENTINEL-' + 'tell-me-a-secret';
const PROJECT = 'proj' + 'SENTINEL' + '-atlas';
const SESSION_IDS = ['sess-a', 'sess-b', 'sess-h'];
const HOSTILE_TEXT = '<img src=x onerror="window.__pwned=1">';

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitPort(port) {
  for (let i = 0; i < 100; i++) {
    const ok = await new Promise((r) => { const s = net.connect(port, '127.0.0.1').on('connect', () => { s.destroy(); r(true); }).on('error', () => r(false)); });
    if (ok) return;
    await sleep(100);
  }
  throw new Error('port ' + port + ' never opened');
}
function getRaw(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (r) => { let b = ''; r.on('data', (d) => { b += d; }); r.on('end', () => resolve({ status: r.statusCode, body: b })); }).on('error', reject);
  });
}
async function getJson(url) { return JSON.parse((await getRaw(url)).body); }
function localDate(ms) {
  const d = new Date(ms), p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// One scenario stack: limits state, tree kind. Resolves once the limits source has settled (except `pending`).
async function stack(o = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hud-claude-e2e-'));
  const port = await freePort();
  const args = ['-m', 'tests.support.fake_claude', 'serve', dir, String(port), '--limits=' + (o.limits || 'ok'), '--tree=' + (o.tree || 'standard'), '--ttl=1'];
  const child = spawn('python3', args, { cwd: ROOT, stdio: 'ignore', env: Object.assign({}, ENV, { PYTHONPATH: ROOT }) });
  children.push(child);
  await waitPort(port);
  const base = 'http://127.0.0.1:' + port;
  if (o.limits !== 'pending') {
    for (let i = 0; i < 40; i++) {
      const s = await getJson(base + '/api/state');
      const st = s.claude.limits.state;
      if (st !== 'pending' && (o.limits !== 'stale' || st === 'stale')) break;
      await sleep(o.limits === 'stale' ? 500 : 300);
    }
    await sleep(1100);   // past the 1 s state cache, so the first page load reads the settled value
  }
  return { dir, port, url: base + '/', base, stop: () => child.kill() };
}

async function open(browser, url, opts = {}) {
  const page = await browser.newPage({ viewport: { width: opts.width || 380, height: opts.height || 1000 }, deviceScaleFactor: opts.dsf || 1 });
  page.consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') page.consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => page.consoleErrors.push('pageerror: ' + e));
  page.on('request', (r) => allRequests.push(r.url()));
  await page.goto(url);
  await page.waitForSelector('body[data-ready]');
  return page;
}
function hue(rgb) {
  const [r, g, b] = rgb.match(/\d+(\.\d+)?/g).slice(0, 3).map((x) => Number(x) / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  if (d === 0) return { h: 0, s: 0 };
  let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h = (h * 60 + 360) % 360;
  return { h, s: d / (1 - Math.abs(mx + mn - 1)) };
}
const isGreen = ({ h, s }) => s > 0.15 && h >= 70 && h <= 160;
function expect(cond, msg) { if (!cond) failures.push(msg); }
async function scenario(n, name, fn) {
  try { await fn(); console.log(`scenario ${n} ${name}: done`); }
  catch (e) { failures.push(`scenario ${n} ${name} threw: ${e.message}`); }
}
const text = (page, sel) => page.locator(sel).first().innerText();
const fig = (page, key) => page.locator(`.panel.claude [data-claude-fig="${key}"]`).first();

(async () => {
  const browser = await chromium.launch();
  const finish = async (page, allow) => { consoleErrors += page.consoleErrors.filter((m) => !(allow && allow.test(m))).length; await page.close(); };

  // Hand-computed from tests/support/fake_claude.standard_tree: today 226 new tokens, 7 d 276, cache hit 3504 /
  // (3504 + 133 + 36) = 95%, models by 7 d new tokens sonnet 220 / opus 56 of 276 = 80% / 20%.
  await scenario(1, 'tile equals the hand-computed fixture values', async () => {
    const s = await stack();
    const page = await open(browser, s.url);
    const five = await text(page, '[data-claude-fig="five_hour"]'), week = await text(page, '[data-claude-fig="seven_day"]');
    expect(/37%/.test(five) && /63% left/.test(five) && /resets 2H (9|10)M/.test(five), '5H row: ' + JSON.stringify(five));
    expect(/13%/.test(week) && /88% left/.test(week) && /resets 3D 2\dH/.test(week), 'WEEK row: ' + JSON.stringify(week));
    const led = async (key) => fig(page, key).locator('.led').first().getAttribute('data-v');
    expect(await led('today') === '226', 'TODAY: ' + await led('today'));
    expect(await led('d7') === '276', '7D: ' + await led('d7'));
    expect(await led('cache') === '95%', 'CACHE HIT: ' + await led('cache'));
    const models = (await text(page, '[data-claude-fig="models"]')).replace(/\s+/g, ' ').trim();
    expect(models === 'sonnet-5-5 80% opus-5-5 20%', 'model line: ' + models);
    const level = await fig(page, 'five_hour').getAttribute('data-level');
    expect(level === 'green', '5H level: ' + level);
    const blob = (await page.evaluate(() => document.body.innerText)) + (await getRaw(s.base + '/api/state')).body;
    for (const n of [ACCESS, REFRESH, PROMPT, PROJECT, ...SESSION_IDS, '/home/kel']) expect(!blob.includes(n), 'tile page/state leaks ' + n);
    await page.screenshot({ path: path.join(OUT, 'claude-tile.png') });
    await finish(page); s.stop();
  });

  await scenario(2, 'every figure is a click target (and the detector can fail)', async () => {
    const s = await stack();
    const page = await open(browser, s.url);
    const untargeted = () => page.evaluate(() => Array.from(document.querySelectorAll('.panel.claude [data-claude-fig], .panel.claude .cl-fig, .panel.claude .cl-row, .panel.claude .cl-models'))
      .filter((e) => !(e.closest('a.hit') && /kind=claude&id=(limits|usage|models|projects)$/.test(e.closest('a.hit').getAttribute('href')))).length);
    const total = await page.locator('.panel.claude [data-claude-fig]').count();
    expect(total === 6, 'expected 6 figures (5H, WEEK, TODAY, 7D, CACHE, models), found ' + total);
    expect(await untargeted() === 0, 'untargeted figures: ' + await untargeted());
    await page.evaluate(() => { const d = document.createElement('div'); d.className = 'cl-fig'; d.textContent = 'planted'; document.querySelector('.panel.claude').appendChild(d); });
    expect(await untargeted() === 1, 'negative control: detector did not report exactly 1');
    await finish(page); s.stop();
  });

  await scenario(3, 'detail windows show fixture values; hostile text is inert', async () => {
    const s = await stack({ tree: 'hostile' });
    const detail = async (id, opts) => open(browser, s.base + '/detail.html?kind=claude&id=' + id, Object.assign({ width: 560, height: 900 }, opts));
    let page = await detail('limits');
    expect(await page.locator('[data-window="five_hour"] .led').getAttribute('data-v') === '37%', 'limits 5H value');
    expect(await page.locator('[data-window="seven_day"] .led').getAttribute('data-v') === '13%', 'limits WEEK value');
    expect(/Resets .*\(in 2H (9|10)M\)/.test(await text(page, '[data-window="five_hour"]')), 'limits reset text');
    expect(await page.locator('[data-f="fetch-count"]').innerText() === '1', 'limits fetch count');
    await page.screenshot({ path: path.join(OUT, 'claude-limits.png'), fullPage: true });
    await finish(page);
    page = await detail('usage', { height: 1500 });
    expect(await page.locator('svg[data-chart="hourly"] rect').count() === 24, 'hourly bucket count');
    expect(await page.locator('svg[data-chart="daily"] rect').count() === 30, 'daily bucket count');
    expect(await page.locator('[data-f="sessions-today"]').innerText() !== '', 'sessions fact');
    const tbl = (await text(page, '.tbl')).replace(/\s+/g, ' ');
    expect(/Today 356 41 112 203 4\.0K 4 /.test(tbl), 'windows table: ' + tbl);
    await page.screenshot({ path: path.join(OUT, 'claude-usage.png'), fullPage: true });
    await finish(page);
    const unsafe = (p) => p.evaluate(() => document.querySelectorAll('#body img, #body script, #body [onerror], #body [onload], #body a[href^="javascript:" i]').length + (window.__pwned ? 1 : 0));
    page = await detail('projects');
    const pt = await page.evaluate(() => document.getElementById('body').innerText);
    expect(pt.includes(HOSTILE_TEXT), 'hostile project name not shown as literal text: ' + pt.slice(0, 200));
    expect(await unsafe(page) === 0, 'hostile project name produced live elements');
    await finish(page);
    // again with the Content-Security-Policy header stripped: the rendering itself must be inert
    const bare = await browser.newPage({ viewport: { width: 380, height: 900 } });
    bare.consoleErrors = [];
    await bare.route('**/*', async (route) => {
      const resp = await route.fetch();
      const headers = Object.assign({}, resp.headers());
      delete headers['content-security-policy'];
      await route.fulfill({ response: resp, headers });
    });
    await bare.goto(s.base + '/detail.html?kind=claude&id=projects');
    await bare.waitForSelector('body[data-ready]');
    expect(await unsafe(bare) === 0, 'hostile project name live with the CSP stripped');
    expect((await bare.evaluate(() => document.getElementById('body').innerText)).includes(HOSTILE_TEXT), 'literal text missing with CSP stripped');
    // control: the detector MUST find elements planted by an unsafe render
    await bare.evaluate((h) => { document.getElementById('body').innerHTML = h + '<script>window.__pwned=1<' + '/script>'; }, HOSTILE_TEXT);
    const caught = await unsafe(bare);
    console.log('INERT_CONTROL detector_catches_unsafe=' + (caught > 0 ? 'yes' : 'no'));
    expect(caught > 0, 'INERT_CONTROL detector missed planted unsafe elements');
    await bare.unrouteAll({ behavior: 'ignoreErrors' });
    await finish(bare);
    page = await detail('models');
    expect((await text(page, '.tbl')).includes('claude-sonnet-5-5'), 'models table');
    await finish(page);
    s.stop();
  });

  await scenario(4, 'fail-soft: every non-ok state is labelled, with no digits and no green', async () => {
    const want = {
      pending: 'FETCHING', disabled: 'OFF', no_credentials: 'NO CREDENTIALS', token_expired: 'TOKEN EXPIRED',
      needs_reauth: 'SIGN IN TO CLAUDE CODE', rate_limited: 'RATE LIMITED', unreachable: 'UNREACHABLE', schema_mismatch: 'UNEXPECTED REPLY',
    };
    for (const st of ['ok', 'stale'].concat(Object.keys(want))) {
      const s = await stack({ limits: st });
      const page = await open(browser, s.url);
      for (const key of ['five_hour', 'seven_day']) {
        const row = fig(page, key), v = await row.locator('.cl-v').innerText(), m = await row.locator('.cl-m').innerText();
        const lit = await row.locator('.meter b.on').count(), cls = await row.locator('.meter').getAttribute('class');
        if (st === 'ok' || st === 'stale') {
          expect(/\d+%/.test((await row.locator('.cl-v .led').getAttribute('data-v')) || ''), `${st}/${key}: no value shown`);
          if (st === 'stale') expect(/stale \d/.test(m), `${st}/${key}: age missing: ${m}`);
        } else {
          expect(v.trim() === want[st], `${st}/${key}: label ${JSON.stringify(v)} != ${want[st]}`);
          expect(!/\d/.test(v + m), `${st}/${key}: digits shown: ${JSON.stringify(v + m)}`);
          expect(lit === 0 && /grey/.test(cls), `${st}/${key}: bar lit (${lit}) or not grey (${cls})`);
          const bg = await row.locator('.meter b').evaluateAll((els) => els.map((e) => getComputedStyle(e).backgroundColor));
          expect(!bg.some((c) => isGreen(hue(c))), `${st}/${key}: a bar cell is green`);
        }
      }
      await finish(page); s.stop();
    }
    const s = await stack({ tree: 'missing' });
    const page = await open(browser, s.url);
    const figs = (await text(page, '.cl-figs')).replace(/\s+/g, ' ');
    expect((figs.match(/NO DATA/g) || []).length === 3, 'missing transcripts: expected NO DATA x3, got ' + figs);
    expect(await page.locator('.cl-figs .led').count() === 0, 'a numeral is drawn with no transcripts');
    expect(await page.locator('.cl-models').count() === 0, 'a model line is drawn with no transcripts');
    await finish(page); s.stop();
  });

  await scenario(5, 'partial coverage is labelled with its real start', async () => {
    const s = await stack({ tree: 'ten' });
    const first = localDate(nowMs() - 10 * 86400 * 1000 + 600 * 1000);
    const page = await open(browser, s.base + '/detail.html?kind=claude&id=usage');
    const tbl = (await text(page, '.tbl')).replace(/\s+/g, ' ');
    const body = (await page.evaluate(() => document.getElementById('body').innerText)).replace(/\s+/g, ' ');
    expect(/30 days \*/.test(tbl) && body.includes('partial: the transcript history starts ' + first), 'partial 30 d label: ' + tbl + ' // ' + body.slice(0, 300));
    expect(!/7 days \*/.test(tbl), '7 d wrongly labelled partial: ' + tbl);
    expect(await page.locator('[data-f="coverage"]').innerText() === first, 'history-from fact');
    await finish(page); s.stop();
  });

  await scenario(6, 'idle cost: one fetch per TTL, only appended bytes scanned', async () => {
    const s = await stack();
    const scan0 = (await getJson(s.base + '/api/state')).claude.usage.scan;
    for (let i = 0; i < 5; i++) { await getJson(s.base + '/api/state'); await sleep(1100); }
    const d = await getJson(s.base + '/api/detail/claude?id=limits');
    expect(d.diag.fetch_count === 1, 'fetches after 5 loads: ' + d.diag.fetch_count);
    const scan1 = (await getJson(s.base + '/api/state')).claude.usage.scan;
    expect(scan1.bytes_read_total === scan0.bytes_read_total, 'bytes re-read inside the TTL');
    const file = path.join(s.dir, 'projects', '-home-kel-dev-atlas', 'sess-a.jsonl');
    const add = JSON.stringify({ type: 'assistant', uuid: 'u-appended', timestamp: new Date(nowMs()).toISOString(), sessionId: 'sess-a', cwd: '/home/kel/dev/x',
      requestId: 'r-appended', message: { id: 'm-appended', model: 'claude-sonnet-5-5', usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 } } }) + '\n';
    fs.appendFileSync(file, add);
    await sleep(15500);   // past the 15 s scan TTL
    const scan2 = (await getJson(s.base + '/api/state')).claude.usage.scan;
    expect(scan2.bytes_read_last === Buffer.byteLength(add), `appended ${Buffer.byteLength(add)} bytes, scan read ${scan2.bytes_read_last}`);
    expect(scan2.full_rescans === 0, 'full rescan after an append');
    s.stop();
  });

  await scenario(7, 'unknown id is an explicit not-found', async () => {
    const s = await stack();
    expect((await getRaw(s.base + '/api/detail/claude?id=nope')).status === 404, 'unknown id is not 404');
    const page = await open(browser, s.base + '/detail.html?kind=claude&id=nope');
    expect(await page.getAttribute('body', 'data-ready') === 'notfound', 'data-ready is not notfound');
    expect((await page.evaluate(() => document.getElementById('body').innerText)).includes('Not found'), 'Not found text missing');
    await finish(page, /Failed to load resource|404/); s.stop();
  });

  await scenario(8, 'hygiene: loopback only, no emoji, no overflow, height budget', async () => {
    const s = await stack();
    const page = await open(browser, s.url + '?preview=1', { height: 1000, dsf: 2 });
    const dims = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, sh: document.documentElement.scrollHeight }));
    expect(dims.sh <= 1600, 'scrollHeight ' + dims.sh + ' > 1600');
    expect(dims.sw <= dims.cw, `horizontal overflow ${dims.sw} > ${dims.cw}`);
    const box = await page.locator('.panel.claude').boundingBox();
    expect(box && box.height <= 220, 'Claude panel height ' + (box && box.height) + ' > 220');
    console.log('claude panel height ' + Math.round(box.height) + ' px');
    const t = await page.evaluate(() => document.body.innerText);
    expect(!/\p{Extended_Pictographic}/u.test(t), 'emoji in rendered text');
    await finish(page); s.stop();
  });

  await browser.close();
  children.forEach((c) => c.kill());
  const foreign = allRequests.filter((u) => !/^(http:\/\/(127\.0\.0\.1|localhost)[:/]|data:|about:)/.test(u));
  expect(foreign.length === 0, 'non-loopback requests: ' + foreign.join(','));
  expect(consoleErrors === 0, `console/page errors: ${consoleErrors}`);
  if (failures.length) { failures.forEach((f) => console.log('FAIL ' + f)); console.log('E2E_CLAUDE_FAIL failures=' + failures.length); process.exit(1); }
  console.log(`E2E_CLAUDE_PASS scenarios=8 console_errors=${consoleErrors} nonloopback=${foreign.length}`);
})().catch((e) => { console.log('E2E_CLAUDE_CRASH ' + e.stack); children.forEach((c) => c.kill()); process.exit(2); });
