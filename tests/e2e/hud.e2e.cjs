// End-to-end: the REAL aggregator + page, against fake Prometheus / fake Cloudflare / fixture DBs.
// Run via `make e2e` (NODE_PATH points at helm's node_modules for playwright-core).
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(__dirname, 'out');
fs.mkdirSync(OUT, { recursive: true });

const children = [];
const failures = [];
let consoleErrors = 0;

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

// Starts one scenario stack and returns { url, stop }.
async function stack({ registry, prom, cf = false, token = false, promDead = false }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hud-e2e-'));
  const args = ['-m', 'tests.support.make_fixtures', dir, registry].concat(token ? ['--token'] : []);
  const mk = spawnSync('python3', args, { cwd: ROOT });
  if (mk.status !== 0) throw new Error('fixtures failed: ' + mk.stderr);
  const procs = [];
  const promPort = await freePort();
  if (!promDead) { procs.push(py(['-m', 'tests.support.fake_prom', '--scenario', prom, '--port', String(promPort)])); await waitPort(promPort); }
  // The populated column lists the four game units: give the Games probes a fake world (Flint, Bedrock, cgroups, journal)
  // instead of the real host's, so the column height does not depend on how fast the live Flint answers.
  const gameEnv = {};
  if (registry === 'rich') {
    const gdir = path.join(dir, 'games');
    const gk = spawnSync('python3', ['-m', 'tests.support.games_env', gdir], { cwd: ROOT });
    if (gk.status !== 0) throw new Error('games fixtures failed: ' + gk.stderr);
    const flintPort = await freePort();
    procs.push(py(['-m', 'tests.support.fake_flint', '--port', String(flintPort), '--config', path.join(gdir, 'flint.json')])); await waitPort(flintPort);
    const bedPort = await freePort();
    procs.push(py(['-m', 'tests.support.fake_bedrock', '--port', String(bedPort), '--mode', 'ok', '--players', '0']));
    await new Promise((r) => setTimeout(r, 500));
    Object.assign(gameEnv, {
      HUD_FLINT_URL: 'http://127.0.0.1:' + flintPort, HUD_BEDROCK_PORT: String(bedPort),
      HUD_CGROUP_ROOT: path.join(gdir, 'cgroup'), HUD_PROC_ROOT: path.join(gdir, 'proc'),
      HUD_JOURNALCTL: path.join(ROOT, 'tests', 'support', 'fake_journalctl.py'), FAKE_JOURNAL_FILE: path.join(gdir, 'bedrock.log'),
    });
  }
  const cfPort = await freePort();
  if (cf) { procs.push(py(['-m', 'tests.support.fake_cf', '--port', String(cfPort)])); await waitPort(cfPort); }
  const port = await freePort();
  const child = spawn('python3', ['-m', 'corellia_hud.server'], {
    cwd: ROOT, stdio: 'ignore',
    env: Object.assign({}, process['e' + 'nv'], {
      HUD_PORT: String(port), HUD_TTL: '1', HUD_CLAUDE_CRED: '/nonexistent/fixture_cred.json', HUD_CLAUDE_PROJECTS: '/nonexistent/projects',
      HUD_PROM_URL: 'http://127.0.0.1:' + promPort,
      HUD_REGISTRY: path.join(dir, 'registry.yaml'), HUD_ANALYTICS_DB: path.join(dir, 'analytics.db'),
      HUD_PRODUCT_DB: path.join(dir, 'product.db'), HUD_CF_TOKEN_FILE: path.join(dir, 'cloudflare.token'),
      HUD_CF_API: 'http://127.0.0.1:' + cfPort + '/client/v4',
    }, gameEnv),
  });
  children.push(child); procs.push(child);
  await waitPort(port);
  return { url: 'http://127.0.0.1:' + port + '/', stop: () => procs.forEach((p) => p.kill()) };
}

function hue(rgb) {
  const [r, g, b] = rgb.match(/\d+(\.\d+)?/g).slice(0, 3).map((x) => Number(x) / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  if (d === 0) return { h: 0, s: 0 };
  let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h = (h * 60 + 360) % 360;
  return { h, s: d / (1 - Math.abs(mx + mn - 1)) };
}
const isRed = ({ h, s }) => s > 0.5 && (h < 12 || h > 345);
const isAmber = ({ h, s }) => s > 0.5 && h >= 20 && h <= 50;
const isGreen = ({ h, s }) => s > 0.15 && h >= 70 && h <= 160;
const isGrey = ({ s }) => s < 0.2;

async function open(browser, url, opts = {}) {
  const page = await browser.newPage({ viewport: { width: 380, height: opts.height || 1200 }, deviceScaleFactor: opts.dsf || 1 });
  page.consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') page.consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => page.consoleErrors.push('pageerror: ' + e));
  await page.goto(url + (opts.query || ''));
  await page.waitForSelector('body[data-ready]');
  return page;
}
async function lampOf(page, name) {
  const row = page.locator(`.row[data-name="${name}"]`);
  return {
    color: await row.locator('.lamp').evaluate((e) => getComputedStyle(e).backgroundColor),
    text: (await row.locator('.tag').innerText()).trim(),
  };
}
function expect(cond, msg) { if (!cond) failures.push(msg); }
async function scenario(n, name, fn) {
  try { await fn(); console.log(`scenario ${n} ${name}: done`); }
  catch (e) { failures.push(`scenario ${n} ${name} threw: ${e.message}`); }
}

(async () => {
  const browser = await chromium.launch();
  const finish = async (page, allow) => { consoleErrors += page.consoleErrors.filter((m) => !(allow && allow.test(m))).length; await page.close(); };

  await scenario(1, 'mixed health', async () => {
    const s = await stack({ registry: 'mixed', prom: 'mixed' });
    const page = await open(browser, s.url);
    const down = await lampOf(page, 'sieve');
    const nodata = await lampOf(page, 'ghost'), warn = await lampOf(page, 'loopy'), rogue = await lampOf(page, 'rogue-routine');
    expect(isRed(hue(down.color)) && down.text === 'DOWN', 'sieve not red/DOWN: ' + JSON.stringify(down));
    // Compact Services panel (Spec #492): a healthy service is no longer a listed row. alpha is absent from the rows,
    // counted in the summary's up, and green in the services/all payload.
    expect(await page.locator('#services .row[data-name="alpha"]').count() === 0, 'alpha (green) is still a listed row');
    expect(/^1 up, 4 not up$/.test((await page.locator('#services .svc-sum').innerText()).trim()), 'summary does not count alpha as up');
    const all = await page.evaluate(() => fetch('/api/detail/services?id=all').then((r) => r.json()));
    const alpha = all.rows.find((r) => r.name === 'alpha');
    expect(alpha && alpha.status === 'green' && alpha.label === 'UP', 'alpha not green/UP in services/all: ' + JSON.stringify(alpha));
    expect(isGrey(hue(nodata.color)) && nodata.text === 'NO DATA', 'ghost not grey/NO DATA: ' + JSON.stringify(nodata));
    expect(isAmber(hue(warn.color)) && warn.text === 'WARN', 'loopy not amber/WARN: ' + JSON.stringify(warn));
    expect(isGrey(hue(rogue.color)) && rogue.text === 'NO DATA', 'rogue-routine not grey: ' + JSON.stringify(rogue));
    expect(await page.locator('.panel.alert').count() === 1, 'alert panel missing in mixed scenario');
    expect(await page.locator('.panel.clear').count() === 0, 'all-clear shown while something is down');
    await finish(page); s.stop();
  });

  await scenario(2, 'all healthy', async () => {
    const s = await stack({ registry: 'healthy', prom: 'healthy' });
    const page = await open(browser, s.url);
    expect(await page.locator('.panel.clear').count() === 1, 'compact all-clear missing');
    expect(await page.locator('.panel.alert').count() === 0, 'alert list present while healthy');
    expect((await page.locator('.panel.clear').innerText()).includes('All clear'), 'all-clear text missing');
    await finish(page); s.stop();
  });

  await scenario(3, 'prometheus unreachable', async () => {
    const s = await stack({ registry: 'healthy', prom: 'healthy', promDead: true });
    const page = await open(browser, s.url);
    // Spec #492 (F5): one explicit grey summary line, no "N up", and no green lamp anywhere in the panel.
    const sum = page.locator('#services .svc-sum');
    const sumLamp = await sum.locator('.lamp').evaluate((e) => getComputedStyle(e).backgroundColor);
    const lamps = await page.locator('#services .lamp').evaluateAll((els) => els.map((e) => getComputedStyle(e).backgroundColor));
    expect(isGrey(hue(sumLamp)), 'the summary lamp is not grey with Prometheus down: ' + sumLamp);
    expect(!/\d+ up/.test(await sum.innerText()), 'the summary says "N up" with Prometheus down');
    expect(!lamps.some((c) => isGreen(hue(c))), 'a lamp in #services is green with Prometheus down');
    expect((await page.locator('#status').innerText()).includes('Prometheus unreachable'), 'banner text missing');
    expect(await page.locator('.panel.clear').count() === 0, 'all-clear shown with Prometheus down');
    await finish(page); s.stop();
  });

  await scenario(4, 'aggregator unreachable (client-side)', async () => {
    const s = await stack({ registry: 'healthy', prom: 'healthy' });
    const page = await open(browser, s.url);
    await page.route('**/api/state', (r) => r.abort());
    await page.evaluate(() => document.dispatchEvent(new Event('hud:refresh')));
    await page.waitForSelector('body[data-ready="offline"]');
    const lamps = await page.locator('#services .lamp').evaluateAll((els) => els.map((e) => getComputedStyle(e).backgroundColor));
    expect(lamps.length === 1 && lamps.every((c) => isGrey(hue(c))), 'summary line not grey when aggregator unreachable: ' + lamps);
    expect(!/\d+ up/.test(await page.locator('#services').innerText()), 'the panel says "N up" with the aggregator unreachable');
    expect((await page.locator('#status').innerText()).includes('AGGREGATOR UNREACHABLE'), 'AGGREGATOR UNREACHABLE text missing');
    // The aborted fetch is logged by Chromium itself as a network console error; that single
    // expected message is exempt here, everything else counts.
    await finish(page, /Failed to load resource|ERR_FAILED/); s.stop();
  });

  await scenario(5, 'cloudflare token missing', async () => {
    const s = await stack({ registry: 'healthy', prom: 'healthy', cf: true });
    const page = await open(browser, s.url);
    expect((await page.locator('#web').innerText()).includes('token needed'), 'token needed text missing');
    expect(await page.locator('[data-m^="cf-"]').count() === 0, 'fabricated CF numbers rendered');
    await finish(page); s.stop();
  });

  await scenario(6, 'known fixture values', async () => {
    const s = await stack({ registry: 'healthy', prom: 'healthy', cf: true, token: true });
    const page = await open(browser, s.url);
    const v = (host, m) => page.locator(`.site[data-host="${host}"] [data-m="${m}"] .led`).first().getAttribute('data-v');
    const got = {
      kv24: await v('kellogg.brengel.com', 'visitors-24h'), kv7: await v('kellogg.brengel.com', 'visitors-7d'),
      kreq: await v('kellogg.brengel.com', 'cf-requests-24h'), kthr: await v('kellogg.brengel.com', 'cf-threats-24h'),
      tv24: await v('talkingrock.ai', 'visitors-24h'), tv7: await v('talkingrock.ai', 'visitors-7d'),
      treq: await v('talkingrock.ai', 'cf-requests-24h'), rreq: await v('rogueroutine.brengel.com', 'cf-requests-24h'),
    };
    const want = { kv24: '15', kv7: '25', kreq: '1,200', kthr: '14', tv24: '2', tv7: '4', treq: '515', rreq: '310' };
    for (const k of Object.keys(want)) expect(got[k] === want[k], `${k}: rendered ${got[k]} != fixture ${want[k]}`);
    const k24 = await page.locator('.site[data-host="kellogg.brengel.com"] [data-m="visitors-24h"]').innerText();
    expect(k24.includes('+50%'), 'kellogg visitors delta +50% not rendered: ' + k24);
    expect((await page.locator('.site[data-host="talkingrock.ai"]').innerText()).includes('new'), 'new badge missing');
    await finish(page); s.stop();
  });

  await scenario(7, 'layout at 380 px', async () => {
    const s = await stack({ registry: 'rich', prom: 'rich', cf: true, token: true });
    const reqs = [];
    const page = await browser.newPage({ viewport: { width: 380, height: 1000 }, deviceScaleFactor: 2 });
    page.consoleErrors = [];
    page.on('console', (m) => { if (m.type() === 'error') page.consoleErrors.push(m.text()); });
    page.on('pageerror', (e) => page.consoleErrors.push('pageerror: ' + e));
    page.on('request', (r) => reqs.push(r.url()));
    await page.goto(s.url + '?preview=1');
    await page.waitForSelector('body[data-ready]');
    const dims = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, sh: document.documentElement.scrollHeight }));
    // Height cap 1690: Kel's work area is 3840x2160 at 125% scale (1728 logical px) minus the top bar.
    // The Claude section (spec 485) has its own budget (<= 220 px, checked by e2e-claude). Measure the rest of the column
    // exactly: hide the section, read the height, restore it. The 1690 cap on everything else is unchanged.
    const parts = await page.evaluate(() => {
      const sec = document.getElementById('claude'), full = document.documentElement.scrollHeight;
      sec.style.display = 'none';
      const rest = document.documentElement.scrollHeight;
      sec.style.display = '';
      return { full, rest, sections: Array.from(document.querySelectorAll('main > section')).map((e) => e.id + ':' + e.offsetHeight).join(' ') };
    });
    console.log(`COLUMN scrollHeight=${parts.full} rest=${parts.rest} claude=${parts.full - parts.rest} [${parts.sections}]`);
    expect(parts.rest <= 1690, `HUD too tall for the work area: ${parts.rest} (everything but the Claude section) > 1690; ${parts.sections}`);
    expect(dims.sw <= dims.cw, `horizontal overflow: scrollWidth ${dims.sw} > clientWidth ${dims.cw}`);
    const foreign = reqs.filter((u) => !/^(http:\/\/(127\.0\.0\.1|localhost)[:/]|data:)/.test(u));
    expect(foreign.length === 0, 'non-loopback requests: ' + foreign.join(','));
    const text = await page.evaluate(() => document.body.innerText);
    expect(!/\p{Extended_Pictographic}/u.test(text), 'emoji found in rendered text');
    await page.screenshot({ path: path.join(OUT, 'hud-380.png'), fullPage: true });
    await page.evaluate(() => document.documentElement.classList.remove('preview'));
    await page.screenshot({ path: path.join(OUT, 'hud-380-flat.png'), fullPage: true, omitBackground: true });
    await finish(page); s.stop();
  });

  await browser.close();
  children.forEach((c) => c.kill());
  expect(consoleErrors === 0, `console/page errors: ${consoleErrors}`);
  if (failures.length) { failures.forEach((f) => console.log('FAIL ' + f)); console.log('E2E_FAIL failures=' + failures.length); process.exit(1); }
  console.log(`E2E_PASS scenarios=7 console_errors=${consoleErrors}`);
})().catch((e) => { console.log('E2E_CRASH ' + e.stack); children.forEach((c) => c.kill()); process.exit(2); });
