// End-to-end for the compact Services panel (Spec #492): the REAL aggregator + pages against fake Prometheus /
// Cloudflare / Flint / Bedrock / journalctl, a fixture transcript tree and a scripted opener for Claude (all in the
// one in-process aggregator of tests/support/fake_claude.py). Chromium via helm's Playwright at 380 px, dsf 2.
// The whole-column height gate runs at the real pinned-window height. Run via `make e2e-services`.
const { spawn, spawnSync } = require('child_process');
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

// The pinned window is area.height - 2 * MARGIN (extension.js, MARGIN = 12): the 3840x2160 monitor at scale 1.25 is
// 1728 logical px, minus the 32 px top bar = 1696 work area, minus 24 = 1672. DOD-17 pins the two extension lines.
const WINDOW_H = 1672;
const fits = (h) => h <= WINDOW_H;
// Pages are measured in a viewport SHORTER than the column, so documentElement.scrollHeight is the content height
// (in a taller viewport it floors at the viewport and every headroom reads 0). gate() then re-checks at WINDOW_H.
const MEASURE_H = 1000;

const children = [];
const failures = [];
const allRequests = [];
let consoleErrors = 0;
const hygiene = { overflow: 0, emoji: 0, pages: 0 };

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function py(args, env) {
  const c = spawn('python3', args, { cwd: ROOT, stdio: 'ignore', env: Object.assign({}, ENV, { PYTHONPATH: ROOT }, env || {}) });
  children.push(c);
  return c;
}
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
const getJson = async (url) => JSON.parse((await getRaw(url)).body);

// One full-column stack: fixtures, fake Prometheus, the games world, fake Cloudflare and the aggregator with the
// populated Claude panel. Resolves once the Claude limits source has settled, so the first page load is populated.
async function stack({ registry, prom, promDead = false, noRegistry = false }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hud-svc-e2e-'));
  const mk = spawnSync('python3', ['-m', 'tests.support.make_fixtures', dir, registry, '--token'], { cwd: ROOT });
  if (mk.status !== 0) throw new Error('fixtures failed: ' + mk.stderr);
  const procs = [];
  const promPort = await freePort();
  if (!promDead) { procs.push(py(['-m', 'tests.support.fake_prom', '--scenario', prom, '--port', String(promPort)])); await waitPort(promPort); }
  const gdir = path.join(dir, 'games');
  const gk = spawnSync('python3', ['-m', 'tests.support.games_env', gdir], { cwd: ROOT });
  if (gk.status !== 0) throw new Error('games fixtures failed: ' + gk.stderr);
  const flintPort = await freePort();
  procs.push(py(['-m', 'tests.support.fake_flint', '--port', String(flintPort), '--config', path.join(gdir, 'flint.json')])); await waitPort(flintPort);
  const bedPort = await freePort();
  procs.push(py(['-m', 'tests.support.fake_bedrock', '--port', String(bedPort), '--mode', 'ok', '--players', '0']));
  await sleep(500);
  const cfPort = await freePort();
  procs.push(py(['-m', 'tests.support.fake_cf', '--port', String(cfPort)])); await waitPort(cfPort);
  const port = await freePort();
  const gameEnv = {
    HUD_FLINT_URL: 'http://127.0.0.1:' + flintPort, HUD_BEDROCK_PORT: String(bedPort),
    HUD_CGROUP_ROOT: path.join(gdir, 'cgroup'), HUD_PROC_ROOT: path.join(gdir, 'proc'),
    HUD_JOURNALCTL: path.join(ROOT, 'tests', 'support', 'fake_journalctl.py'), FAKE_JOURNAL_FILE: path.join(gdir, 'bedrock.log'),
  };
  const args = ['-m', 'tests.support.fake_claude', 'serve', dir, String(port), '--limits=ok', '--tree=standard', '--ttl=1',
    '--prom=http://127.0.0.1:' + promPort, '--registry=' + (noRegistry ? path.join(dir, 'no-such-registry.yaml') : path.join(dir, 'registry.yaml')),
    '--analytics=' + path.join(dir, 'analytics.db'), '--product=' + path.join(dir, 'product.db'),
    '--cf-token=' + path.join(dir, 'cloudflare.token'), '--cf-api=http://127.0.0.1:' + cfPort + '/client/v4'];
  procs.push(py(args, gameEnv));
  await waitPort(port);
  const base = 'http://127.0.0.1:' + port;
  for (let i = 0; i < 40; i++) {
    const st = (await getJson(base + '/api/state')).claude.limits.state;
    if (st !== 'pending') break;
    await sleep(300);
  }
  await sleep(1100);   // past the 1 s state cache
  return { dir, base, url: base + '/', stop: () => procs.forEach((p) => p.kill()) };
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

function expect(cond, msg) { if (!cond) failures.push(msg); }
async function scenario(n, name, fn) {
  try { await fn(); console.log(`scenario ${n} ${name}: done`); }
  catch (e) { failures.push(`scenario ${n} ${name} threw: ${e.stack || e.message}`); }
}

// A context per stack so every page it opens (popups too) is watched: console errors, page errors, requests.
async function context(browser, bucket) {
  const ctx = await browser.newContext({ viewport: { width: 380, height: MEASURE_H }, deviceScaleFactor: 2 });
  ctx.on('page', (p) => {
    p.on('console', (m) => { if (m.type() === 'error') bucket.push(m.text()); });
    p.on('pageerror', (e) => bucket.push('pageerror: ' + e));
  });
  ctx.on('request', (r) => allRequests.push(r.url()));
  return ctx;
}
async function openHud(ctx, url) {
  const page = await ctx.newPage();
  await page.goto(url);
  await page.waitForSelector('body[data-ready="live"]');
  return page;
}
// Hygiene of one HUD page: no horizontal overflow, no emoji in the rendered text.
async function checkHygiene(page, label) {
  const d = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, text: document.body.innerText }));
  hygiene.pages += 1;
  if (d.sw > d.cw) { hygiene.overflow += 1; failures.push(`${label}: horizontal overflow ${d.sw} > ${d.cw}`); }
  if (/\p{Extended_Pictographic}/u.test(d.text)) { hygiene.emoji += 1; failures.push(`${label}: emoji in rendered text`); }
}
async function finishCtx(ctx, bucket, allow) {
  consoleErrors += bucket.filter((m) => !(allow && allow.test(m))).length;
  bucket.length = 0;
  await ctx.close();
}
async function shot(page, name) {
  await page.evaluate(() => document.documentElement.classList.add('preview'));
  await page.screenshot({ path: path.join(OUT, name), fullPage: true });
}

// Everything the consistency and gate checks read from a HUD page, in one round trip.
async function facts(page) {
  return page.evaluate(() => {
    const q = (s) => document.querySelector(s);
    const sum = q('#services .svc-sum');
    const chips = {};
    document.querySelectorAll('#services .chips a.chip').forEach((a) => {
      const l = a.querySelector('.lamp');
      const c = ['green', 'amber', 'red', 'grey'].find((x) => l.classList.contains(x));
      chips[c] = Number(a.innerText.trim());
    });
    const clear = q('.panel.clear');
    const sec = q('#services');
    const rows = Array.from(document.querySelectorAll('#services .row.svc')).map((r) => {
      const b = r.getBoundingClientRect();
      return { name: r.getAttribute('data-name'), lamp: getComputedStyle(r.querySelector('.lamp')).backgroundColor,
        cls: r.className, tag: r.querySelector('.tag').innerText.trim(), reason: r.querySelector('.rs').innerText.trim(),
        w: b.width, h: b.height, rsw: r.querySelector('.rs').getBoundingClientRect().width };
    });
    const more = q('#services .svc-more');
    const upM = sum && /(\d+) up/.exec(sum.innerText);
    const notM = sum && /(\d+) not up/.exec(sum.innerText);
    return {
      full: document.documentElement.scrollHeight, client: document.documentElement.clientHeight,
      secScroll: sec.scrollHeight, secClient: sec.clientHeight,
      sum: sum && { text: sum.innerText.trim(), state: sum.getAttribute('data-state'), href: sum.getAttribute('href'),
        lamp: getComputedStyle(sum.querySelector('.lamp')).backgroundColor, up: upM ? Number(upM[1]) : null, notUp: notM ? Number(notM[1]) : null },
      chips, clearText: clear ? clear.innerText.replace(/\s+/g, ' ').trim() : null,
      rows, more: more && { text: more.innerText.trim(), href: more.getAttribute('href') },
      off: q('#services .svc-off') ? q('#services .svc-off').innerText.trim() : null,
      alertPanel: !!q('.panel.alert'),
      pageText: document.body.innerText,
      allLamps: Array.from(document.querySelectorAll('#services .lamp')).map((l) => getComputedStyle(l).backgroundColor),
      games: document.querySelectorAll('#games [data-game]').length, sites: document.querySelectorAll('#web .site').length,
      claudeLive: ['five_hour', 'seven_day'].map((k) => document.querySelectorAll(`.panel.claude [data-claude-fig="${k}"] .led`).length),
    };
  });
}

// GATE line + assertions for the whole populated column: content height against the real window height, then a
// resize to the window itself must show no scroll at all.
async function gate(name, page, f) {
  const clipped = (f.secScroll !== f.secClient ? 1 : 0) + f.rows.filter((r) => !(r.w > 0 && r.h > 0 && r.rsw > 0)).length;
  await page.setViewportSize({ width: 380, height: WINDOW_H });
  const win = await page.evaluate(() => ({ sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight }));
  const pass = f.full > MEASURE_H && fits(f.full) && win.ch === WINDOW_H && win.sh === win.ch;
  console.log(`GATE ${name} scrollHeight=${f.full} headroom=${WINDOW_H - f.full} limit=${WINDOW_H} pass=${pass ? 'yes' : 'no'} clipped=${clipped}`);
  expect(pass, `${name}: column ${f.full} px vs the ${WINDOW_H} px window (window scroll ${win.sh} / client ${win.ch})`);
  expect(clipped === 0, `${name}: the Services section is clipped (${clipped})`);
}

// CONSISTENT line: banner N (the all-clear text when shown, else the count the banner is built from), the
// summary's up, the green chip, and listed + more == red + amber + grey.
async function consistent(name, page, f) {
  const meta = (await getJson(page.url().split('?')[0] + 'api/state')).meta;
  const clearN = f.clearText ? Number((/(\d+) up/.exec(f.clearText) || [])[1]) : null;
  const banner = clearN == null ? meta.counts.green : clearN;
  const up = f.sum ? f.sum.up : null, chipG = f.chips.green == null ? 0 : f.chips.green;
  const notUp = (f.chips.red || 0) + (f.chips.amber || 0) + (f.chips.grey || 0);
  const listed = f.rows.length, more = f.more ? Number((/\+(\d+) more/.exec(f.more.text) || [])[1]) : 0;
  const ok = banner === up && up === chipG && up === meta.counts.green && listed + more === notUp && notUp === meta.services_summary.not_up;
  console.log(`CONSISTENT ${name} banner=${banner} summary=${up} chip=${chipG} listed=${listed} more=${more} not_up=${notUp} ok=${ok ? 'yes' : 'no'}`);
  expect(ok, `${name}: banner ${banner} / summary ${up} / chip ${chipG} / listed ${listed} + more ${more} vs not-up chips ${notUp}`);
}

// Click a link on the HUD page and return the popup it opens (target="corellia-hud-detail").
async function clickPopup(hud, sel) {
  const [pop] = await Promise.all([hud.waitForEvent('popup'), hud.click(sel)]);
  await pop.setViewportSize({ width: 560, height: 900 });
  await pop.waitForSelector('body[data-ready]');
  return pop;
}
const endsWith = (url, tail) => url.endsWith(tail);

(async () => {
  const browser = await chromium.launch();
  const bucket = [];

  // ---------------------------------------------------------------- 1 ALL-GREEN
  await scenario(1, 'all green', async () => {
    const s = await stack({ registry: 'allgreen', prom: 'allgreen' });
    const ctx = await context(browser, bucket);
    const page = await openHud(ctx, s.url);
    const f = await facts(page);
    expect(f.sum && f.sum.text === '27 up all services', 'summary text: ' + JSON.stringify(f.sum));
    expect(f.sum && f.sum.state === 'all_up', 'summary state: ' + (f.sum && f.sum.state));
    expect(f.sum && isGreen(hue(f.sum.lamp)), 'summary lamp is not green: ' + (f.sum && f.sum.lamp));
    expect(f.rows.length === 0 && !f.more, 'all-green lists rows: ' + f.rows.map((r) => r.name));
    expect(f.chips.green === 27 && !f.chips.amber && !f.chips.red && !f.chips.grey, 'chips: ' + JSON.stringify(f.chips));
    expect(f.clearText && /27 up/.test(f.clearText), 'banner: ' + f.clearText);
    await checkHygiene(page, 'allgreen');
    await gate('allgreen', page, f);
    await consistent('allgreen', page, f);
    const pop = await clickPopup(page, '#services a.svc-sum');
    expect(endsWith(pop.url(), 'kind=services&id=all'), 'summary click url ' + pop.url());
    await shot(page, 'services-allgreen.png');
    await finishCtx(ctx, bucket); s.stop();
  });

  // ---------------------------------------------------------------- 2 ONE DOWN
  await scenario(2, 'one down', async () => {
    const s = await stack({ registry: 'allgreen', prom: 'onedown' });
    const ctx = await context(browser, bucket);
    const page = await openHud(ctx, s.url);
    const f = await facts(page);
    expect(f.rows.length === 1 && f.rows[0].name === 'sieve', 'listed rows: ' + f.rows.map((r) => r.name));
    const r = f.rows[0] || {};
    expect(isRed(hue(r.lamp || 'rgb(0,0,0)')) && r.tag === 'DOWN', 'sieve row lamp/tag: ' + JSON.stringify(r));
    expect(r.reason === 'port closed' && r.rsw > 20, 'reason not visible: ' + JSON.stringify(r));
    expect(f.sum && f.sum.up === 26 && f.sum.notUp === 1 && f.sum.state === 'problems', 'summary: ' + JSON.stringify(f.sum));
    expect(f.chips.red === 1 && f.chips.green === 26, 'chips: ' + JSON.stringify(f.chips));
    expect(f.alertPanel, 'alerts panel missing');
    expect(f.sum && isRed(hue(f.sum.lamp)), 'summary lamp is not red: ' + (f.sum && f.sum.lamp));
    await checkHygiene(page, 'onedown');
    await gate('onedown', page, f);
    await consistent('onedown', page, f);
    await shot(page, 'services-onedown.png');
    const pop = await clickPopup(page, '#services .row.svc[data-name="sieve"]');
    expect(endsWith(pop.url(), 'kind=service&id=sieve'), 'row click url ' + pop.url());
    await finishCtx(ctx, bucket); s.stop();
  });

  // ---------------------------------------------------------------- 3 SEVERAL DOWN (cap / overflow)
  await scenario(3, 'several down', async () => {
    const s = await stack({ registry: 'rich', prom: 'manydown' });
    const ctx = await context(browser, bucket);
    const page = await openHud(ctx, s.url);
    const f = await facts(page);
    const cap = (await getJson(s.base + '/api/state')).meta.services_summary.cap;
    expect(f.rows.length === cap && cap === 4, `listed ${f.rows.length} rows, cap ${cap}`);
    expect(f.rows.map((r) => r.name).join() === 'sieve,forgejo,ollama,stalag', 'order: ' + f.rows.map((r) => r.name));
    expect(f.rows.slice(0, 3).every((r) => isRed(hue(r.lamp))) && f.rows[3] && isAmber(hue(f.rows[3].lamp)), 'three red then amber: ' + f.rows.map((r) => r.cls));
    expect(f.more && f.more.text === '+5 more' && endsWith(f.more.href, 'id=all'), 'more link: ' + JSON.stringify(f.more));
    expect(f.chips.red === 3 && f.chips.amber === 2 && f.chips.grey === 4, 'chips: ' + JSON.stringify(f.chips));
    expect(f.rows.every((r) => r.h > 0 && r.w > 0), 'a listed row has no box');
    await checkHygiene(page, 'manydown');
    await gate('manydown', page, f);
    await consistent('manydown', page, f);
    await shot(page, 'services-manydown.png');
    const pop = await clickPopup(page, '#services a.svc-more');
    expect(endsWith(pop.url(), 'kind=services&id=all'), '+N more click url ' + pop.url());
    await finishCtx(ctx, bucket); s.stop();
  });

  // ---------------------------------------------------------------- 4 SOURCES UNAVAILABLE
  await scenario(4, 'sources unavailable', async () => {
    const noUp = (t) => !/\b\d+ up\b/i.test(t);
    async function unavailable(caseName, cfg, want, after) {
      const s = await stack(cfg);
      const ctx = await context(browser, bucket);
      const page = await openHud(ctx, s.url);
      if (after) { await after(page); }
      const f = await facts(page);
      const greenLamps = f.allLamps.filter((c) => isGreen(hue(c))).length;
      const allclear = await page.locator('.panel.clear').count() > 0 ? 'yes' : 'no';
      const line = f.off || (f.sum && f.sum.text) || '';
      console.log(`UNAVAILABLE case=${caseName} allclear=${allclear} green_lamps=${greenLamps}`);
      expect(allclear === 'no', `${caseName}: all-clear banner shown`);
      expect(greenLamps === 0, `${caseName}: ${greenLamps} green lamp(s) in #services`);
      expect(want.re.test(line), `${caseName}: line ${JSON.stringify(line)} !~ ${want.re}`);
      expect(noUp(f.pageText), `${caseName}: the page says "N up": ${(/\b\d+ up\b/i.exec(f.pageText) || [])[0]}`);
      expect(f.rows.length === 0 && !f.more, `${caseName}: per-service rows rendered`);
      if (want.state) { expect(f.sum && f.sum.state === want.state, `${caseName}: state ${f.sum && f.sum.state}`); }
      if (want.hue) { expect(f.sum && want.hue(hue(f.sum.lamp)), `${caseName}: summary lamp ${f.sum && f.sum.lamp}`); }
      await checkHygiene(page, caseName);
      return { s, ctx, page, f };
    }
    let r = await unavailable('prom_unreachable', { registry: 'allgreen', prom: 'allgreen', promDead: true },
      { re: /Prometheus unreachable, 27 services unknown/, state: 'unavailable', hue: isGrey });
    await shot(r.page, 'services-unavailable.png');
    const pop = await clickPopup(r.page, '#services a.svc-sum');   // the all window says so too, instead of 27 grey rows as fact
    expect((await pop.locator('.dnote.err').first().innerText()).includes('Prometheus is unreachable'), 'detail all window lacks the unavailable note');
    await finishCtx(r.ctx, bucket); r.s.stop();
    r = await unavailable('collector_stale', { registry: 'allgreen', prom: 'stale' },
      { re: /collector silent, \d+ services unconfirmed/, state: 'stale', hue: isAmber });
    await finishCtx(r.ctx, bucket); r.s.stop();
    r = await unavailable('registry_missing', { registry: 'allgreen', prom: 'allgreen', noRegistry: true },
      { re: /registry unavailable/, state: 'no_registry', hue: isGrey });
    await finishCtx(r.ctx, bucket); r.s.stop();
    r = await unavailable('offline', { registry: 'allgreen', prom: 'allgreen' }, { re: /aggregator unreachable, 27 services unknown/ }, async (page) => {
      await page.route('**/api/state', (x) => x.abort());
      await page.evaluate(() => document.dispatchEvent(new Event('hud:refresh')));
      await page.waitForSelector('body[data-ready="offline"]');
    });
    await finishCtx(r.ctx, bucket, /Failed to load resource|ERR_FAILED/); r.s.stop();
  });

  // ---------------------------------------------------------------- 5 DETAIL ALL
  await scenario(5, 'detail all', async () => {
    const s = await stack({ registry: 'allgreen', prom: 'onedown' });
    const ctx = await context(browser, bucket);
    const hud = await openHud(ctx, s.url);
    const pop = await clickPopup(hud, '#services a.svc-sum');
    expect(endsWith(pop.url(), 'kind=services&id=all'), 'url ' + pop.url());
    const d = await pop.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('#body .row'));
      const sieve = rows.find((r) => r.querySelector('.nm a').innerText === 'sieve');
      return {
        rows: rows.length, groups: Array.from(document.querySelectorAll('#body .grp')).map((g) => g.innerText.trim()),
        tags: rows.map((r) => r.querySelector('.tag').innerText.trim()),
        links: rows.filter((r) => /kind=service&id=[\w@:.-]+$/.test(r.querySelector('.nm a').getAttribute('href'))).length,
        sieve: sieve ? sieve.querySelector('.tag').innerText.trim() : '',
        chips: Array.from(document.querySelectorAll('#body .chips a.chip')).map((a) => a.getAttribute('href').replace(/.*id=/, '') + ':' + a.innerText.trim()),
        notes: document.querySelectorAll('#body .dnote').length,
      };
    });
    const ok = d.rows === 27 && d.groups.join() === 'Apps,Platform,Games,Plumbing' && d.links === 27 && d.sieve === 'DOWN' &&
      d.tags.filter((t) => t === 'UP').length === 26 && d.chips.join() === 'green:26,red:1' && d.notes === 0;
    console.log(`DETAIL_ALL rows=${d.rows} groups=${d.groups.length} links=${d.links} down=${d.sieve} chips=${d.chips.join('/')} ok=${ok ? 'yes' : 'no'}`);
    expect(ok, 'detail all: ' + JSON.stringify(d));
    await pop.setViewportSize({ width: 560, height: 1300 });
    await shot(pop, 'services-detail-all.png');
    // an unknown id is a not-found page, never a blank one
    const bad = await ctx.newPage();
    const statuses = [];
    bad.on('response', (r) => { if (r.url().includes('/api/detail/')) statuses.push(r.status()); });
    await bad.goto(s.url + 'detail.html?kind=services&id=everything');
    await bad.waitForSelector('body[data-ready]');
    expect(await bad.locator('body').getAttribute('data-ready') === 'notfound' && /Not found/.test(await bad.evaluate(() => document.body.innerText)), 'everything is not a not-found page');
    expect(statuses.includes(404), 'everything: API status ' + statuses);
    await finishCtx(ctx, bucket, /Failed to load resource.*404/); s.stop();
  });

  // ---------------------------------------------------------------- 6 WHOLE-COLUMN GATE (rich)
  await scenario(6, 'whole-column gate', async () => {
    const s = await stack({ registry: 'rich', prom: 'rich' });
    const ctx = await context(browser, bucket);
    const page = await openHud(ctx, s.url);
    const f = await facts(page);
    const total = (await getJson(s.base + '/api/state')).services.length;
    const live = f.claudeLive[0] > 0 && f.claudeLive[1] > 0 ? 'live' : 'placeholder';
    const populated = live === 'live' && f.games === 4 && f.sites === 3 && total === 27;
    const line = `COLUMN_POPULATED claude=${live} games=${f.games} sites=${f.sites} services=${total}`;
    console.log(line);
    expect(populated, 'the gate refuses an unpopulated column: ' + line);
    expect(f.sum && f.sum.state === 'problems' && f.rows.length === 4 && !f.more, 'rich summary: ' + JSON.stringify(f.sum) + ' rows ' + f.rows.length);
    await checkHygiene(page, 'rich');
    await gate('rich', page, f);
    await consistent('rich', page, f);
    // inert control: the same comparator must FAIL on a column that really overflows. A 150 px filler is planted in
    // #services; when the headroom is larger than that, the filler grows to headroom + 1 so the control stays honest.
    const planted = async (px) => page.evaluate((h) => {
      const d = document.createElement('div'); d.id = 'planted-filler'; d.style.height = h + 'px';
      document.getElementById('services').appendChild(d);
      const full = document.documentElement.scrollHeight; d.remove(); return full;
    }, px);
    let filler = 150, withFiller = await planted(filler);
    if (fits(withFiller)) { filler = WINDOW_H - f.full + 1; withFiller = await planted(filler); }
    const caught = !fits(withFiller);
    console.log(`INERT_CONTROL gate_catches_overflow=${caught ? 'yes' : 'no'}`);
    console.log(`# inert control: filler=${filler}px, column ${f.full} -> ${withFiller}`);
    expect(caught, `inert control: the comparator passed ${withFiller} with a ${filler} px filler`);
    await shot(page, 'services-column.png');
    await finishCtx(ctx, bucket); s.stop();
  });

  // ---------------------------------------------------------------- 7 HYGIENE
  await scenario(7, 'hygiene', async () => {
    const s = await stack({ registry: 'allgreen', prom: 'onedown' });
    const ctx = await context(browser, bucket);
    const page = await openHud(ctx, s.url);
    const STAT = '#services .svc-sum, #services .svc-more, #services .row, #services .chip';
    const untargeted = () => page.evaluate((sel) => Array.from(document.querySelectorAll(sel)).filter((e) =>
      !e.closest('a.hit[target="corellia-hud-detail"][href^="/detail.html?kind="]')).length, STAT);
    expect(await page.locator(STAT).count() >= 3, 'the actionable inventory is vacuous');
    expect(await untargeted() === 0, 'actionable elements without a click target: ' + await untargeted());
    await page.evaluate(() => { const d = document.createElement('div'); d.className = 'svc-sum'; d.textContent = 'bare'; document.getElementById('services').appendChild(d); });
    const reported = await untargeted();
    console.log(`INVENTORY_CONTROL untargeted_reported=${reported}`);
    expect(reported === 1, 'negative control reported ' + reported + ' (want 1)');
    expect(hygiene.pages >= 8 && hygiene.overflow === 0 && hygiene.emoji === 0, 'hygiene tally: ' + JSON.stringify(hygiene));
    await finishCtx(ctx, bucket); s.stop();
  });

  await browser.close();
  children.forEach((c) => c.kill());
  const foreign = allRequests.filter((u) => !/^(http:\/\/(127\.0\.0\.1|localhost)[:/]|data:|about:)/.test(u));
  expect(foreign.length === 0, 'non-loopback requests: ' + foreign.join(','));
  expect(consoleErrors === 0, `console/page errors: ${consoleErrors}`);
  if (failures.length) { failures.forEach((f) => console.log('FAIL ' + f)); console.log('E2E_SERVICES_FAIL failures=' + failures.length); process.exit(1); }
  console.log(`E2E_SERVICES_PASS scenarios=7 console_errors=${consoleErrors} nonloopback=${foreign.length}`);
})().catch((e) => { console.log('E2E_SERVICES_CRASH ' + e.stack); children.forEach((c) => c.kill()); process.exit(2); });
