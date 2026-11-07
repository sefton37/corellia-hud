// Node tests for the pin extension's window matching (match.js is a pure ES module) and for the
// structure of extension.js (gi:// imports cannot load under node, so those checks read the text).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT_DIR = path.join(HERE, '..', 'extension', 'corellia-hud-pin@corellia.local');
const match = await import(path.join(EXT_DIR, 'match.js'));
const {classifyWindow} = match;
const extText = fs.readFileSync(path.join(EXT_DIR, 'extension.js'), 'utf8');

// Body of `class <name> {` up to its matching closing brace (brace counting; no braces in strings here).
function classBody(name) {
  const start = extText.indexOf(`class ${name} {`);
  assert.ok(start >= 0, `class ${name} not found in extension.js`);
  let depth = 0, i = extText.indexOf('{', start);
  const from = i;
  for (; i < extText.length; i++) {
    if (extText[i] === '{') depth++;
    if (extText[i] === '}' && --depth === 0) break;
  }
  return extText.slice(from, i + 1);
}

test('the column window is classified as column', () => {
  assert.equal(classifyWindow('corellia-hud', 'Corellia HUD'), 'column');
});

test('the detail window is classified as detail', () => {
  assert.equal(classifyWindow('corellia-hud-detail', 'Corellia detail'), 'detail');
});

test('class wins: detail class with the column title is detail, never column', () => {
  assert.equal(classifyWindow('corellia-hud-detail', 'Corellia HUD'), 'detail');
});

test('title-only detail match works before the class is known', () => {
  assert.equal(classifyWindow('', 'Corellia detail'), 'detail');
});

test('title-only column match is preserved for the not-yet-set class path', () => {
  assert.equal(classifyWindow('', 'Corellia HUD'), 'column');
});

test('lookalikes match nothing', () => {
  assert.equal(classifyWindow('corellia-hud-detail-x', 'x'), null);
  assert.equal(classifyWindow('corellia-hud-', 'Corellia HUD detail'), null);
  assert.equal(classifyWindow('Corellia-HUD', ''), null);
  assert.equal(classifyWindow('corellia-hud-detail ', 'x'), null);
  assert.equal(classifyWindow('x', 'corellia detail'), null);
});

test('null and undefined arguments match nothing and never throw', () => {
  assert.equal(classifyWindow(null, null), null);
  assert.equal(classifyWindow(undefined, undefined), null);
  assert.equal(classifyWindow(), null);
});

test('unrelated windows match nothing', () => {
  assert.equal(classifyWindow('org.gnome.Nautilus', 'Files'), null);
  assert.equal(classifyWindow('firefox', 'Corellia - Mozilla Firefox'), null);
});

test('the exported constant pairs differ and are the documented values', () => {
  assert.notEqual(match.WM_CLASS, match.DETAIL_WM_CLASS);
  assert.notEqual(match.TITLE, match.DETAIL_TITLE);
  assert.equal(match.DETAIL_WM_CLASS, 'corellia-hud-detail');
  assert.equal(match.DETAIL_TITLE, 'Corellia detail');
});

test('extension.js declares the same constants as match.js', () => {
  const grab = (name) => new RegExp(`const ${name} = '([^']+)';`).exec(extText)?.[1];
  assert.equal(grab('WM_CLASS'), match.WM_CLASS);
  assert.equal(grab('TITLE'), match.TITLE);
  assert.equal(grab('DETAIL_WM_CLASS'), match.DETAIL_WM_CLASS);
  assert.equal(grab('DETAIL_TITLE'), match.DETAIL_TITLE);
});

test('extension.js imports match.js and routes through classifyWindow', () => {
  assert.match(extText, /^import .* from '\.\/match\.js';$/m);
  assert.match(extText, /classifyWindow\(/);
});

test('DetailWindow raises and positions the window and never lowers or sticks it', () => {
  const body = classBody('DetailWindow');
  assert.ok(body.includes('make_above('), 'make_above( missing');
  assert.ok(body.includes('move_resize_frame('), 'move_resize_frame( missing');
  for (const banned of ['.lower(', '.stick(', 'unmake_above'])
    assert.ok(!body.includes(banned), `DetailWindow must not contain ${banned}`);
});

test('DetailWindow is hidden from the window list and placed left of the column', () => {
  const body = classBody('DetailWindow');
  assert.ok(body.includes('hide_from_window_list'));
  assert.match(body, /DETAIL_WIDTH/);
  assert.match(body, /DETAIL_GAP/);
});

test('PinnedWindow still lowers and sticks the column', () => {
  const body = classBody('PinnedWindow');
  assert.ok(body.includes('.lower('));
  assert.ok(body.includes('.stick('));
});
