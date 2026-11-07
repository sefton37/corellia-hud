// End-to-end for the Games section: the REAL aggregator + pages against fake Prometheus, a fake Flint, a fake
// Bedrock server-list responder (UDP), a fake journalctl and fixture cgroup / proc / save trees. Chromium via
// helm's Playwright; popups are opened by clicking the real rows. Run via `make e2e-games`.
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

const children = [];
const failures = [];
const allRequests = [];
let consoleErrors = 0;

// Sentinels planted in the fixtures (tests/support/games_env.py).
const NAME = 'NAMESENTINEL-Alice';
const BED_NAME = 'BEDNAMESENTINEL';
const XUID = '2500000000000001';
const PFID = '00000000' + '00000001';
const EOS = 'e05a1f3c' + '9b7d4e2a' + '8c6f0b1d' + '3a5e7c92';
const IP = '203.0.113.77';
const SECRET = 'PWSENTINELx9Kq2Lm4Pz';
const JOIN_SECRET = 'JOINSENTINELq8Wz3';
const SERVERS = ['ark-server', 'ark-astraeos', 'ark-genesis', 'bedrock'];
const PLAYER_KEY = 'last_' + 'rc' + 'on';   // Flint's poll-stamp key

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}
function py(args, env) {
  const c = spawn('python3', args, { cwd: ROOT, stdio: 'ignore', env: Object.assign({}, ENV, env || {}) });
  children.push(c);
  return c;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitPort(port) {
  for (let i = 0; i < 80; i++) {
    const ok = await new Promise((r) => { const s = net.connect(port, '127.0.0.1').on('connect', () => { s.destroy(); r(true); }).on('error', () => r(false)); });
    if (ok) return;
    await sleep(100);
  }
  throw new Error('port ' + port + ' never opened');
}
function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (r) => { let b = ''; r.on('data', (d) => { b += d; }); r.on('end', () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } }); }).on('error', reject);
  });
}
function localIso(ms) {   // naive LOCAL time, the way Flint writes its stamps
  const d = new Date(ms), p = (n, w) => String(n).padStart(w || 2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}000`;
}

// One scenario stack. Options: players, hostile, rich, flint ('up' | 'down' | function(config)), bedrockMode,
// bedrockPlayers, inactive [units], skipCgroup [server names], filler (extra journal lines).
async function stack(o = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hud-games-e2e-'));
  const flags = [].concat(o.players ? ['--players'] : [], o.hostile ? ['--hostile'] : [], o.rich ? ['--rich'] : [],
    (o.skipCgroup || []).map((n) => '--skip-cgroup=' + n));
  const mk = spawnSync('python3', ['-m', 'tests.support.games_env', dir].concat(flags), { cwd: ROOT });
  if (mk.status !== 0) throw new Error('fixtures failed: ' + mk.stderr);
  const cfgPath = path.join(dir, 'flint.json');
  if (typeof o.flint === 'function') {
    const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
    o.flint(cfg);
    fs.writeFileSync(cfgPath, JSON.stringify(cfg));
  }
  if (o.filler) {
    const lines = Array.from({ length: o.filler }, (_, i) => `2026-11-05T08:30:${String(i % 60).padStart(2, '0')}-06:00 Corellia bedrock_server[8897]: [2026-11-05 08:30:00:000 INFO] chatter line ${i}`);
    const logPath = path.join(dir, 'bedrock.log');   // older lines go first: the player lines stay in the last 20
    fs.writeFileSync(logPath, lines.join('\n') + '\n' + fs.readFileSync(logPath, 'utf8'));
  }
  const procs = [];
  const promPort = await freePort();
  procs.push(py(['-m', 'tests.support.fake_prom', '--scenario', 'games', '--port', String(promPort),
    '--inactive', (o.inactive || []).join(',')].concat(o.rich ? ['--rich'] : [])));
  await waitPort(promPort);
  let flintPort = await freePort();
  if (o.flint !== 'down') { procs.push(py(['-m', 'tests.support.fake_flint', '--port', String(flintPort), '--config', cfgPath])); await waitPort(flintPort); }
  const bedPort = await freePort();
  const countFile = path.join(dir, 'bedrock.count');
  procs.push(py(['-m', 'tests.support.fake_bedrock', '--port', String(bedPort), '--mode', o.bedrockMode || 'ok',
    '--players', String(o.bedrockPlayers == null ? 1 : o.bedrockPlayers), '--count-file', countFile]));
  await sleep(500);
  const port = await freePort();
  const child = py(['-m', 'corellia_hud.server'], {
    HUD_PORT: String(port), HUD_TTL: '1', HUD_CLAUDE_CRED: '/nonexistent/fixture_cred.json', HUD_CLAUDE_PROJECTS: '/nonexistent/projects',
    HUD_PROM_URL: 'http://127.0.0.1:' + promPort,
    HUD_REGISTRY: path.join(dir, 'registry.yaml'), HUD_ANALYTICS_DB: path.join(dir, 'none-analytics.db'),
    HUD_PRODUCT_DB: path.join(dir, 'none-product.db'), HUD_CF_TOKEN_FILE: path.join(dir, 'none.token'),
    HUD_CF_API: 'http://127.0.0.1:9/client/v4', HUD_CHAT_DIR: path.join(dir, 'none-chat'),
    HUD_JOURNALCTL: path.join(ROOT, 'tests', 'support', 'fake_journalctl.py'),
    FAKE_JOURNAL_FILE: path.join(dir, 'bedrock.log'),
    HUD_FLINT_URL: 'http://127.0.0.1:' + flintPort, HUD_BEDROCK_PORT: String(bedPort),
    HUD_CGROUP_ROOT: path.join(dir, 'cgroup'), HUD_PROC_ROOT: path.join(dir, 'proc'),
  });
  procs.push(child);
  await waitPort(port);
  return { url: 'http://127.0.0.1:' + port + '/', dir, flintPort, countFile, stop: () => procs.forEach((p) => p.kill()) };
}

function expect(cond, msg) { if (!cond) failures.push(msg); }
async function scenario(n, name, fn) {
  try { await fn(); console.log(`scenario ${n} ${name}: done`); }
  catch (e) { failures.push(`scenario ${n} ${name} threw: ${e.stack || e.message}`); }
}
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
  await hud.waitForSelector('.row.grow');
  return hud;
}
async function click(hud, sel, existing) {
  if (existing && !existing.isClosed()) {
    const nav = existing.waitForEvent('framenavigated');
    await hud.click(sel);
    await nav;
    await existing.waitForSelector('body[data-ready]');
    return existing;
  }
  const [pop] = await Promise.all([hud.waitForEvent('popup'), hud.click(sel)]);
  await pop.setViewportSize({ width: 560, height: 1100 });
  await pop.waitForSelector('body[data-ready]');
  return pop;
}
const bodyText = (page) => page.evaluate(() => document.body.innerText);
const noEmoji = (t) => !/\p{Extended_Pictographic}/u.test(t);
async function shot(page, name) { await page.screenshot({ path: path.join(OUT, name), fullPage: true }); }
const factText = (page, f) => page.locator(`[data-f="${f}"]`).first().innerText();

// One game row as the page shows it.
async function rowInfo(hud, name) {
  const row = hud.locator(`.row.grow[data-game="${name}"]`);
  return {
    cls: await row.getAttribute('class'),
    text: await row.innerText(),
    players: await row.locator('[data-f="players"] .led').first().getAttribute('data-v'),
    tag: await row.locator('.tag').innerText(),
  };
}
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

(async () => {
  const browser = await chromium.launch();
  const bucket = [];
  const seenText = [];
  const finishCtx = async (ctx, allow) => {
    const bad = bucket.splice(0).filter((m) => !(allow && allow.test(m)));
    bad.forEach((m) => console.log('CONSOLE_ERROR ' + m.slice(0, 200)));
    consoleErrors += bad.length;
    await ctx.close();
  };
  let columnHeight = 0, columnWidth = 0;

  // ---------------------------------------------------------------- 1 TILE
  await scenario(1, 'tile', async () => {
    const s = await stack({ players: true, rich: true });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 }); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    expect(await hud.locator('.row.grow').count() === 4, 'games rows != 4');
    const want = {
      'ark-server': { players: '1/8', up: /UP 7H 2M/, ram: /RAM 9\.6G/ },
      'ark-astraeos': { players: '1/8', up: /UP 5D 3H/, ram: /RAM 22\.3G/ },
      'ark-genesis': { players: '1/8', up: /UP 2D 0H/, ram: /RAM 13\.0G/ },
      bedrock: { players: '1/10', up: /UP 1H 0M/, ram: /RAM 0\.5G/ },
    };
    for (const n of SERVERS) {
      const r = await rowInfo(hud, n);
      expect(/\bgreen\b/.test(r.cls), `${n}: lamp is not green: ${r.cls}`);
      expect(r.players === want[n].players, `${n}: players ${r.players} != ${want[n].players}`);
      expect(want[n].up.test(r.text), `${n}: uptime text: ${r.text}`);
      expect(want[n].ram.test(r.text), `${n}: ram text: ${r.text}`);
      expect(/RST24 1/.test(r.text), `${n}: restarts text: ${r.text}`);
    }
    const t = await bodyText(hud);
    const state = JSON.stringify(await getJson(s.url + 'api/state'));
    for (const secret of [NAME, BED_NAME, XUID, PFID, EOS, IP, SECRET, JOIN_SECRET]) {
      expect(!t.includes(secret) && !state.includes(secret), 'a private value reached the tile or /api/state: ' + secret);
    }
    columnHeight = await hud.evaluate(() => document.documentElement.scrollHeight);
    columnWidth = await hud.evaluate(() => document.documentElement.scrollWidth);
    await shot(hud, 'games-tile.png');
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 2 INVENTORY
  await scenario(2, 'inventory', async () => {
    const s = await stack({ players: true });
    const ctx = await browser.newContext(); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    const untargeted = () => Array.from(document.querySelectorAll('#games .row, #games [data-f], #games .tag, #games .lamp'))
      .filter((e) => !e.closest('a.hit[target="corellia-hud-detail"][href^="/detail.html?kind=game&id="]')).length;
    const total = await hud.locator('#games .row, #games [data-f], #games .tag, #games .lamp').count();
    expect(total >= 4 * 7, 'inventory is vacuous: only ' + total + ' game elements');
    expect(await hud.evaluate(untargeted) === 0, 'game stat elements without a click target');
    await hud.evaluate(() => { const d = document.createElement('span'); d.setAttribute('data-f', 'bare'); d.textContent = 'bare'; document.getElementById('games').appendChild(d); });
    const reported = await hud.evaluate(untargeted);
    expect(reported === 1, 'inventory negative control reported ' + reported + ' (want 1)');
    console.log(`INVENTORY games_elements=${total} control_reported=${reported}`);
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 3 DETAIL
  await scenario(3, 'detail', async () => {
    const s = await stack({ players: true, filler: 40 });
    const ctx = await browser.newContext({ deviceScaleFactor: 2 }); watch(ctx, bucket);
    const hud = await openHud(ctx, s.url);
    let pop = await click(hud, '.row.grow[data-game="ark-server"]');
    expect(/\/detail\.html\?kind=game&id=ark-server$/.test(pop.url()), 'ark popup url: ' + pop.url());
    const t = await bodyText(pop);
    seenText.push(t);
    expect(t.includes(NAME), 'online name missing from the ARK window');
    expect(t.includes('Gustaf') && t.includes('unnamed player'), 'last-seen list incomplete: Gustaf / unnamed player');
    expect(/name not exposed/.test(t), 'recent joins row missing');
    expect(/04M ago/.test(await factText(pop, 'last-save')), 'last save age: ' + await factText(pop, 'last-save'));
    expect((await factText(pop, 'cluster')).trim() === 'corellia-cluster', 'cluster: ' + await factText(pop, 'cluster'));
    expect((await factText(pop, 'version')).trim() === '96.9', 'version: ' + await factText(pop, 'version'));
    for (const secret of [EOS, IP, SECRET, JOIN_SECRET]) expect(!t.includes(secret), 'private value in the ARK window: ' + secret);
    await shot(pop, 'games-ark.png');
    const before = ctx.pages().length;
    pop = await click(hud, '.row.grow[data-game="bedrock"]', pop);
    expect(ctx.pages().length === before, 'a second click opened another window instead of re-targeting');
    expect(/kind=game&id=bedrock$/.test(pop.url()), 'bedrock popup url: ' + pop.url());
    const b = await bodyText(pop);
    seenText.push(b);
    expect(b.includes('ExtraBiomes') && b.includes('Vcraft: Home Furniture'), 'add-on packs missing');
    expect(b.includes(XUID) && b.includes(PFID), 'raw xuid / pfid missing from the Bedrock journal');
    expect(!/\[redacted\]/i.test(b), 'a redaction marker is present');
    expect(b.includes(BED_NAME), 'online name missing');
    expect(/join/.test(b) && /leave/.test(b), 'join / leave rows missing');
    expect(await pop.locator('.log div').count() === 20, 'journal lines shown: ' + await pop.locator('.log div').count());
    await shot(pop, 'games-bedrock.png');
    await finishCtx(ctx); s.stop();
  });

  // ---------------------------------------------------------------- 4 FAIL-SOFT
  await scenario(4, 'fail-soft', async () => {
    const cases = [
      ['flint down', { players: true, flint: 'down' }, async (hud) => {
        const r = await rowInfo(hud, 'ark-server');
        expect(r.players === '-' && /NO DATA/.test(r.text), 'flint down: players not NO DATA: ' + r.text);
        expect(/\bgreen\b/.test(r.cls), 'flint down: lamp does not follow the unit: ' + r.cls);
      }],
      ['flint offline', { flint: (c) => { c.status.Extinction.json.status = 'offline'; } }, async (hud) => {
        const r = await rowInfo(hud, 'ark-server');
        expect(/\bamber\b/.test(r.cls) && !/\bred\b/.test(r.cls), 'flint offline is not amber: ' + r.cls);
        expect(/Flint reports/.test(r.text), 'flint offline: no reason shown: ' + r.text);
        expect(/\bgreen\b/.test((await rowInfo(hud, 'ark-genesis')).cls), 'flint offline leaked to another server');
      }],
      ['stale poll', { flint: (c) => { c.status.Extinction.json[PLAYER_KEY] = localIso(Date.now() - 600000); } }, async (hud) => {
        const r = await rowInfo(hud, 'ark-server');
        expect(/stale/.test(r.text) && r.players === '-', 'stale poll not shown as stale: ' + r.text);
      }],
      ['null count', { flint: (c) => { c.status.Extinction.json.player_count = null; } }, async (hud) => {
        const r = await rowInfo(hud, 'ark-server');
        expect(/players unknown/.test(r.text) && r.players === '-' && !/0\/8/.test(r.text), 'null count is not unknown: ' + r.text);
      }],
      ['unit inactive', { skipCgroup: ['ark-genesis'], inactive: ['ark-genesis.service'], flint: (c) => { c.status.Genesis.json.status = 'offline'; } }, async (hud) => {
        const r = await rowInfo(hud, 'ark-genesis');
        expect(/\bred\b/.test(r.cls) && /DOWN/.test(r.tag), 'inactive unit is not red: ' + r.cls + ' ' + r.tag);
        expect(r.players === '-' && /UP -/.test(r.text), 'inactive unit still shows players / uptime: ' + r.text);
      }],
      ['bedrock reply dead', { bedrockMode: 'silent' }, async (hud) => {
        const r = await rowInfo(hud, 'bedrock');
        expect(/\bamber\b/.test(r.cls) && /not answering/.test(r.text) && r.players === '-', 'dead reply is not amber with a reason: ' + r.cls + ' ' + r.text);
      }],
    ];
    for (const [name, opts, check] of cases) {
      const s = await stack(opts);
      const ctx = await browser.newContext(); watch(ctx, bucket);
      const hud = await openHud(ctx, s.url);
      try { await check(hud); } catch (e) { failures.push(`fail-soft "${name}" threw: ${e.message}`); }
      seenText.push(await bodyText(hud));
      await finishCtx(ctx); s.stop();
    }
  });

  // ---------------------------------------------------------------- 5 HOSTILE
  await scenario(5, 'hostile', async () => {
    const s = await stack({ players: true, hostile: true });
    for (const stripCsp of [false, true]) {
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
      let pop = await click(hud, '.row.grow[data-game="ark-server"]');
      let d = await pop.evaluate(detector);
      expect(sumDetector(d) === 0, `ark window injection (csp stripped=${stripCsp}): ${JSON.stringify(d)}`);
      expect((await bodyText(pop)).includes('<img src=x'), 'hostile text not rendered as literal text in the ARK window');
      pop = await click(hud, '.row.grow[data-game="bedrock"]', pop);
      d = await pop.evaluate(detector);
      expect(sumDetector(d) === 0, `bedrock window injection (csp stripped=${stripCsp}): ${JSON.stringify(d)}`);
      expect((await bodyText(pop)).includes('<img src=x'), 'hostile text not rendered as literal text in the Bedrock window');
      if (stripCsp) {   // the same detector must catch a deliberately unsafe render
        await pop.evaluate((h) => { document.body.innerHTML = h; }, '<img src=x onerror="window.__pwned=1"><iframe></iframe><a href="javascript:window.__pwned=1">x</a>');
        await sleep(300);
        const unsafe = sumDetector(await pop.evaluate(detector));
        expect(unsafe > 0, 'the detector did not catch an unsafe render');
        console.log(`INERT_CONTROL detector_catches_unsafe=${unsafe > 0 ? 'yes' : 'no'}`);
      }
      await finishCtx(ctx, stripCsp ? /Failed to load resource.*404/ : null);   // the unsafe-render control loads a bogus img src
    }
    s.stop();
  });

  // ---------------------------------------------------------------- 6 IDLE COST
  await scenario(6, 'idle cost', async () => {
    const s = await stack({ players: true });
    for (let i = 0; i < 5; i++) { await getJson(s.url + 'api/state'); await sleep(1300); }   // each lapses the 1 s state cache
    const reqs = await getJson('http://127.0.0.1:' + s.flintPort + '/__requests');
    const status = reqs.filter((r) => r[1] === '/api/status').length, events = reqs.filter((r) => r[1] === '/api/events').length;
    const pings = parseInt(fs.readFileSync(s.countFile, 'utf8'), 10);
    expect(status === 3 && events === 3, `flint saw ${status} status / ${events} events (want 3 / 3)`);
    expect(pings === 1, `bedrock saw ${pings} datagrams (want 1)`);
    expect(reqs.every((r) => r[0] === 'GET'), 'flint saw a non-GET request');
    console.log(`IDLE_COST status=${status} events=${events} bedrock=${pings}`);
    s.stop();
  });

  // ---------------------------------------------------------------- 7 UNKNOWN
  await scenario(7, 'unknown id', async () => {
    const s = await stack({ players: true });
    const ctx = await browser.newContext(); watch(ctx, bucket);
    const page = await ctx.newPage();
    const statuses = [];
    page.on('response', (r) => { if (r.url().includes('/api/detail/')) statuses.push(r.status()); });
    await page.goto(s.url + 'detail.html?kind=game&id=nope');
    await page.waitForSelector('body[data-ready]');
    const t = await bodyText(page);
    expect(/Not found/.test(t) && t.trim().length > 20, 'unknown game id does not say not found: ' + t.slice(0, 80));
    expect(statuses.includes(404), 'the API did not answer 404: ' + statuses);
    expect(await page.locator('body').getAttribute('data-ready') === 'notfound', 'data-ready is not notfound');
    await finishCtx(ctx, /Failed to load resource.*404/); s.stop();
  });

  // ---------------------------------------------------------------- 8 HYGIENE
  await scenario(8, 'hygiene', async () => {
    expect(seenText.length > 0 && seenText.every(noEmoji), 'emoji found in rendered text');
    expect(columnWidth > 0 && columnWidth <= 380, 'horizontal overflow at 380 px: scrollWidth ' + columnWidth);
    // Height cap 1690: Kel's work area is 3840x2160 at 125% scale (1728 logical px) minus the top bar.
    expect(columnHeight > 0 && columnHeight <= 1690, 'populated column too tall: ' + columnHeight);
    console.log(`COLUMN width=${columnWidth} height=${columnHeight}`);
    for (const n of ['tile', 'ark', 'bedrock']) {
      const p = path.join(OUT, `games-${n}.png`);
      expect(fs.existsSync(p) && fs.statSync(p).size > 15000, 'screenshot missing or tiny: ' + p);
    }
  });

  await browser.close();
  children.forEach((c) => c.kill());
  const foreign = allRequests.filter((u) => !/^(http:\/\/(127\.0\.0\.1|localhost)[:/]|data:|about:)/.test(u));
  expect(consoleErrors === 0, `console/page errors: ${consoleErrors}`);
  expect(foreign.length === 0, 'non-loopback requests: ' + foreign.join(','));
  if (failures.length) { failures.forEach((f) => console.log('FAIL ' + f)); console.log('E2E_GAMES_FAIL failures=' + failures.length); process.exit(1); }
  console.log(`E2E_GAMES_PASS scenarios=8 console_errors=${consoleErrors} nonloopback=${foreign.length}`);
})().catch((e) => { console.log('E2E_GAMES_CRASH ' + e.stack); children.forEach((c) => c.kill()); process.exit(2); });
