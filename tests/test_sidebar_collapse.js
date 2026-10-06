const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');
const SCRIPT = path.join(ROOT, 'static/sidebar.js');

function mount(initialValue = null, storageFails = false) {
  const classes = new Set();
  const attributes = {};
  const events = {};
  const requests = [];
  const saved = new Map();
  if (initialValue !== null) saved.set('reimbursement-sidebar-collapsed', initialValue);
  const icon = { setAttribute(name, value) { attributes[`icon-${name}`] = value; } };
  const button = {
    addEventListener(name, handler) { events[`button-${name}`] = handler; },
    setAttribute(name, value) { attributes[name] = value; },
    querySelector(selector) { return selector === 'use' ? icon : null; },
  };
  const versionButton = {
    addEventListener(name, handler) { events[`version-${name}`] = handler; },
  };
  const dialog = {
    open: false,
    innerHTML: '',
    setAttribute(name, value) { attributes[`dialog-${name}`] = value; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    querySelector(selector) {
      return selector === '[data-version-close]' ? {
        addEventListener(name, handler) { events[`version-close-${name}`] = handler; },
      } : null;
    },
  };
  const document = {
    documentElement: {
      classList: {
        add(name) { classes.add(name); },
        remove(name) { classes.delete(name); },
        toggle(name, force) {
          if (force === undefined ? !classes.has(name) : force) classes.add(name);
          else classes.delete(name);
          return classes.has(name);
        },
        contains(name) { return classes.has(name); },
      },
    },
    addEventListener(name, handler) { events[name] = handler; },
    getElementById(id) {
      if (id === 'sidebar-collapse') return button;
      if (id === 'version-details-trigger') return versionButton;
      if (id === 'app-version') return { textContent: 'v1.1.0' };
      if (id === 'current-user') return { textContent: '' };
      return null;
    },
    createElement(name) { return name === 'dialog' ? dialog : null; },
    body: { appendChild(node) { assert.equal(node, dialog); } },
  };
  document.body.dataset = { csrf: '' };
  const localStorage = {
    getItem(key) { if (storageFails) throw new Error('unavailable'); return saved.get(key) ?? null; },
    setItem(key, value) { if (storageFails) throw new Error('unavailable'); saved.set(key, value); },
  };
  const source = fs.existsSync(SCRIPT) ? fs.readFileSync(SCRIPT, 'utf8') : '';
  const context = { document, localStorage };
  context.window = context;
  vm.runInNewContext(source, context);
  return {
    classes, attributes, events, saved, dialog, requests,
    ready() { events.DOMContentLoaded?.(); },
    clickControl() { events['button-click']?.(); },
    clickVersion() { events['version-click']?.(); },
    closeVersion() { events['version-close-click']?.(); },
    loadSession(payload) {
      context.Headers = class {
        constructor(initial) { this.values = { ...(initial || {}) }; }
        set(name, value) { this.values[name] = value; }
      };
      context.fetch = async function (path, options) {
        requests.push({ path, options });
        return { status: 200, ok: true, json: async () => path === '/api/session' ? payload : { message: 'ok' } };
      };
      vm.runInNewContext(fs.readFileSync(path.join(ROOT, 'static/common.js'), 'utf8'), context);
    },
  };
}

test('sidebar starts collapsed before the page becomes interactive, even after a previous expansion', () => {
  const page = mount('0');
  assert.equal(page.classes.has('sidebar-collapsed'), true);
  page.ready();
  assert.equal(page.attributes['aria-expanded'], 'false');
  assert.equal(page.attributes['aria-label'], '展开侧边栏');
});

test('only the dedicated control changes collapse state during the current page visit', () => {
  const page = mount('0');
  page.ready();
  assert.equal(page.attributes['aria-expanded'], 'false');
  assert.equal(page.attributes['icon-href'], '/static/icons.svg?v=20261001-double-chevron#chevrons-right');
  page.clickControl();
  assert.equal(page.classes.has('sidebar-collapsed'), false);
  assert.equal(page.saved.get('reimbursement-sidebar-collapsed'), '0');
  assert.equal(page.attributes['aria-label'], '收起侧边栏');
  assert.equal(page.attributes['icon-href'], '/static/icons.svg?v=20261001-double-chevron#chevrons-left');
  page.clickControl();
  assert.equal(page.classes.has('sidebar-collapsed'), true);
  assert.equal(page.saved.get('reimbursement-sidebar-collapsed'), '0');
  assert.equal(page.attributes['aria-expanded'], 'false');
});

test('collapse control works when browser storage is unavailable', () => {
  const page = mount(null, true);
  page.ready();
  assert.equal(page.classes.has('sidebar-collapsed'), true);
  page.clickControl();
  assert.equal(page.classes.has('sidebar-collapsed'), false);
});

test('each workspace page has a separate control and labeled icon links', () => {
  for (const name of ['index', 'history', 'admin', 'profile']) {
    const markup = fs.readFileSync(path.join(ROOT, `templates/${name}.html`), 'utf8');
    assert.match(markup, /id="version-details-trigger"[^>]*aria-haspopup="dialog"[^>]*aria-controls="version-details-dialog"/);
    assert.match(markup, /id="sidebar-collapse"[^>]*aria-controls="app-sidebar"[^>]*aria-expanded="false"[^>]*aria-label="展开侧边栏"/);
    const nav = markup.match(/<nav>([\s\S]*?)<\/nav>/)?.[1];
    assert.ok(nav, `${name} has navigation`);
    for (const anchor of nav.match(/<a\b[^>]*>[\s\S]*?<\/a>/g) || []) {
      assert.match(anchor, /aria-label="[^"]+"/, `${name} link has a tooltip label`);
      assert.match(anchor, /<svg\b/, `${name} link has an icon`);
      assert.match(anchor, /<span>/, `${name} link has visible text when expanded`);
    }
  }
});

test('version button opens the v1.1.0 release notes and closes them', () => {
  const page = mount();
  page.ready();
  page.clickVersion();
  assert.equal(page.dialog.open, true);
  for (const detail of ['v1.1.0', '2026-10-01', '发票上传', '费用报销', '历史记录再次编辑', '个人中心', '用户资料修改', '报销统计看板', '出差天数看板', '侧栏 UI 优化', '登录失败时超时提示异常', '用户注册条件提示不准确']) {
    assert.ok(page.dialog.innerHTML.includes(detail), `release notes include ${detail}`);
  }
  page.closeVersion();
  assert.equal(page.dialog.open, false);
});

test('first-login session opens the release notes and acknowledges them once', async () => {
  const page = mount();
  page.ready();
  page.loadSession({ user: { username: 'alice' }, csrf_token: 'csrf', release_notes_version: 'v1.1.0' });
  await new Promise(setImmediate);
  assert.equal(page.dialog.open, true);
  assert.deepEqual(page.requests.map(({ path }) => path), ['/api/session', '/api/release-notes/seen']);
  assert.equal(JSON.parse(page.requests[1].options.body).version, 'v1.1.0');
  assert.equal(page.requests[1].options.headers.values['X-CSRF-Token'], 'csrf');

  const later = mount();
  later.ready();
  later.loadSession({ user: { username: 'alice' }, csrf_token: 'csrf' });
  await new Promise(setImmediate);
  assert.equal(later.dialog.open, false);
  assert.deepEqual(later.requests.map(({ path }) => path), ['/api/session']);
});
