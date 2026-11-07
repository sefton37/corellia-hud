// Spec #492: the whole-column height gate against the REAL installed aggregator on 127.0.0.1:8670 (read-only GETs).
// Chromium at 380 px loads the live page; the column must fit the pinned window, and what the page shows must agree
// with /api/state. The sources must be ok (state all_up or problems), otherwise the real-data gate is unproven and this
// FAILS. Run after `make install` (the installed unit must serve the new code). Run via `make live-column-check`.
const { chromium } = require('playwright-core');

const WINDOW_H = 1672;   // area.height - 2 * MARGIN, see tests/e2e/services.e2e.cjs and extension.js
const MEASURE_H = 1000;  // shorter than the column, so scrollHeight is the content height
const BASE = process.argv[2] || 'http://127.0.0.1:8670/';

(async () => {
  const fail = [];
  const requests = [];
  const errors = [];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 380, height: MEASURE_H }, deviceScaleFactor: 2 });
  page.on('request', (r) => requests.push(r.method() + ' ' + r.url()));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e));
  await page.goto(BASE);
  await page.waitForSelector('body[data-ready="live"]');
  const state = await (await fetch(BASE + 'api/state')).json();
  const sum = state.meta.services_summary || {};
  const d = await page.evaluate(() => {
    const s = document.querySelector('#services .svc-sum');
    const g = Array.from(document.querySelectorAll('#services .chips a.chip')).find((a) => a.querySelector('.lamp.green'));
    return {
      full: document.documentElement.scrollHeight,
      sumText: s ? s.innerText.trim() : null,
      greenChip: g ? Number(g.innerText.trim()) : 0,
      rows: document.querySelectorAll('#services .row.svc').length,
      more: (document.querySelector('#services .svc-more') || { innerText: '' }).innerText,
      secScroll: document.getElementById('services').scrollHeight, secClient: document.getElementById('services').clientHeight,
    };
  });
  await page.setViewportSize({ width: 380, height: WINDOW_H });
  const win = await page.evaluate(() => ({ sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight }));
  await page.screenshot({ path: require('path').join(__dirname, 'out', 'services-live-column.png'), fullPage: true });
  await browser.close();

  const claude = state.claude ? (state.claude.limits || {}).state + '/' + (state.claude.usage || {}).state : 'absent';
  if (!(sum.state === 'all_up' || sum.state === 'problems')) {
    fail.push(`services_summary.state is ${sum.state}, not all_up or problems: rerun when Prometheus, the registry and the collector are ok; ` +
      'with the sources down the real-data gate is unproven');
  }
  if (!(d.full > MEASURE_H && d.full <= WINDOW_H)) fail.push(`live column ${d.full} px does not fit ${WINDOW_H}`);
  if (!(win.ch === WINDOW_H && win.sh === win.ch)) fail.push(`at the window height the page still scrolls: ${win.sh} > ${win.ch}`);
  const upShown = d.sumText ? Number((/(\d+) up/.exec(d.sumText) || [])[1]) : NaN;
  if (upShown !== state.meta.counts.green) fail.push(`rendered ${upShown} up != counts.green ${state.meta.counts.green}`);
  if (d.greenChip !== state.meta.counts.green) fail.push(`green chip ${d.greenChip} != counts.green ${state.meta.counts.green}`);
  const wantRows = Math.min(sum.cap, sum.not_up);
  if (d.rows !== wantRows) fail.push(`listed rows ${d.rows} != min(cap ${sum.cap}, not_up ${sum.not_up}) = ${wantRows}`);
  if (d.secScroll !== d.secClient) fail.push('the services section is clipped');
  if (errors.length) fail.push('console errors: ' + errors.join(' | '));
  const bad = requests.filter((r) => !r.startsWith('GET ') || !/^GET (http:\/\/127\.0\.0\.1[:/]|data:)/.test(r));
  if (bad.length) fail.push('non-GET or non-loopback requests: ' + bad.join(', '));
  if (fail.length) { fail.forEach((f) => console.log('FAIL ' + f)); console.log('LIVE_COLUMN_FAIL failures=' + fail.length); process.exit(1); }
  console.log(`LIVE_COLUMN_OK full=${d.full} limit=${WINDOW_H} state=${sum.state} up=${upShown} listed=${d.rows} more=${sum.more} claude=${claude}`);
})().catch((e) => { console.log('LIVE_COLUMN_CRASH ' + e.stack); process.exit(2); });
