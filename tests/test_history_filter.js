const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle: name => { if (classes.has(name)) { classes.delete(name); return false; } classes.add(name); return true; },
    };
    this.style = { setProperty(name, value) { this[name] = value; } };
  }
  append(...children) { children.forEach(child => { child.parentElement = this; this.children.push(child); }); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); }
  set textContent(value) { this._textContent = value; if (value === '') this.children = []; }
  get textContent() { return this._textContent; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  dispatch(type, event = {}) { return this.listeners[type]?.(event); }
  close() {}
  showModal() { this.open = true; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute() {}
  querySelector(selector) {
    if (selector.startsWith('.')) return this.children.find(child => child.className?.split(' ').includes(selector.slice(1))) || null;
    return null;
  }
  get options() { return this.children; }
}

function makePage(scope = 'active', initialStatsAllowed = true, fetchOverride = null) {
  const ids = [
    'history-rows', 'history-empty', 'history-error', 'record-count',
    'history-pagination', 'history-previous-page', 'history-next-page',
    'history-page-status', 'history-filters', 'history-year', 'history-month',
    'history-stat-count', 'history-stat-amount',
    'history-type-switcher', 'travel-history-trigger', 'expense-history-trigger',
    'records-heading', 'history-record-panel', 'history-retention-hint',
    'purge-dialog', 'purge-name', 'edit-dialog', 'edit-frame', 'edit-close',
    'sidebar-toggle', 'app-sidebar', 'logout',
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const statWrappers = { count: new Element(), amount: new Element() };
  statWrappers.count.append(elements['history-stat-count']);
  statWrappers.amount.append(elements['history-stat-amount']);
  elements['history-filters'].hidden = true;
  elements['history-year'].value = '';
  elements['history-month'].value = '';
  const script = fs.readFileSync(path.join(__dirname, '../static/history.js'), 'utf8');
  const calls = [];
  let response = [];
  let statsAllowed = initialStatsAllowed;
  const statsGrid = new Element();
  const statsOverlay = new Element();
  statsGrid.querySelector = () => statsOverlay;
  const window = {
    location: { origin: 'http://test.local' },
    listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };
  const document = {
    body: { dataset: { scope } },
    visibilityState: 'visible',
    listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
    getElementById: id => elements[id],
    querySelectorAll: () => [],
    querySelector: selector => selector === '.history-stats' ? statsGrid : new Element(),
    createElement: tag => new Element(tag),
  };
  const context = { document, window, Intl, Date, encodeURIComponent,
    apiFetch: async url => { calls.push(url); if (fetchOverride) return fetchOverride(url); return url === '/api/permissions'
      ? { reimbursement_stats: statsAllowed, history_retention_months: statsAllowed ? null : 6 }
      : { reimbursements: response }; },
    localStorage: { setItem() {} },
  };
  vm.runInNewContext(script, context);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  return {
    elements, calls, flush, statsGrid, statsOverlay, statWrappers,
    setStatsAllowed(value) { statsAllowed = value; },
    refreshAccess() { document.listeners.visibilitychange(); },
    setResponse(records) { response = records; },
    reload() {
      window.listeners.message({
        origin: window.location.origin,
        source: elements['edit-frame'].contentWindow,
        data: { type: 'reimbursement-regenerated' },
      });
    },
  };
}

const record = (id, created_at, reimbursement_date = '2026-01-01') => ({
  id, created_at, reimbursement_date, reason: `记录 ${id}`,
  reimbursement_amount: '10.00', pdf_url: `/pdf/${id}`, form_type: 'travel',
});

test('history shows two record tabs with the reference icons and selected state', () => {
  const markup = fs.readFileSync(path.join(__dirname, '../templates/history.html'), 'utf8');
  assert.match(markup, /<button id="travel-history-trigger"[^>]*aria-selected="true"[^>]*>[\s\S]*?差旅报销记录<\/span><\/button>/);
  assert.match(markup, /<button id="expense-history-trigger"[^>]*aria-selected="false"[^>]*>[\s\S]*?费用报销记录<\/span><\/button>/);
  assert.match(markup, /icons\.svg#route/);
  assert.match(markup, /icons\.svg#receipt/);
});

test('history places two shared-style statistics above the record table', () => {
  const markup = fs.readFileSync(path.join(__dirname, '../templates/history.html'), 'utf8');
  assert.match(markup, /class="stats-grid history-stats"/);
  assert.match(markup, /id="history-stat-count" class="stat-value"/);
  assert.match(markup, /id="history-stat-amount" class="stat-value"/);
  assert.ok(markup.indexOf('id="history-stat-count"') < markup.indexOf('id="history-record-panel"'));
});

test('history uses one continuous locked statistic area without retention banner', () => {
  const markup = fs.readFileSync(path.join(__dirname, '../templates/history.html'), 'utf8');
  const styles = fs.readFileSync(path.join(__dirname, '../static/ui-components.css'), 'utf8');
  assert.doesNotMatch(markup, /id="history-limit-note"/);
  assert.match(markup, /class="stats-pro-overlay"[^>]*>\s*<a class="table-action-button" href="\/profile">升级 Pro 后查看<\/a>/);
  assert.match(styles, /\.history-stats\[data-stats-locked="true"\]::before\s*\{[^}]*backdrop-filter:\s*blur\(/);
  assert.match(styles, /\.history-stats\[data-stats-locked="true"\] \.stat-card\s*\{[^}]*backdrop-filter:\s*none/);
  assert.match(styles, /\.history-stats\[data-stats-locked="true"\] \.stat-label\s*\{[^}]*z-index:\s*3/);
});

test('recent-record hint sits below the category heading', () => {
  const markup = fs.readFileSync(path.join(__dirname, '../templates/history.html'), 'utf8');
  const styles = fs.readFileSync(path.join(__dirname, '../static/ui-components.css'), 'utf8');
  assert.match(markup, /id="records-heading"[^>]*>差旅报销记录<\/h2>[\s\S]*?<p id="history-retention-hint"[^>]*hidden>当前展示近6个月记录，升级Pro后展示全部。<\/p>/);
  assert.match(styles, /\.history-retention-hint\s*\{[^}]*font-size:\s*0\.78rem/);
});

test('recent-record hint follows Free and Pro access in both record categories', async () => {
  const page = makePage('active', false);
  await page.flush();
  const hint = page.elements['history-retention-hint'];
  assert.equal(hint.hidden, false);
  page.elements['expense-history-trigger'].dispatch('click');
  assert.equal(page.elements['records-heading'].textContent, '费用报销记录');
  assert.equal(hint.hidden, false);
  page.setStatsAllowed(true);
  page.refreshAccess();
  await page.flush();
  assert.equal(hint.hidden, true);
  page.setStatsAllowed(false);
  page.refreshAccess();
  await page.flush();
  assert.equal(hint.hidden, false);
  const trash = makePage('trash', false);
  await trash.flush();
  assert.equal(trash.elements['history-retention-hint'].hidden, true);
});

test('statistics follow category and date filters across all pages', async () => {
  const page = makePage();
  page.setResponse([
    ...Array.from({ length: 16 }, (_, index) => ({
      ...record(index + 1, '2026-09-01T00:00:00+00:00'),
      reimbursement_amount: index === 15 ? '0.20' : '0.10',
    })),
    { ...record(17, '2025-01-01T00:00:00+00:00'), reimbursement_amount: '100.00' },
    { ...record(18, '2026-09-01T00:00:00+00:00'), form_type: 'expense', reimbursement_amount: '24.50' },
  ]);
  page.reload();
  await page.flush();
  const { elements } = page;
  assert.equal(elements['history-stat-count'].textContent, '17 笔');
  assert.equal(elements['history-stat-amount'].textContent, '¥101.70');
  elements['history-next-page'].dispatch('click');
  assert.equal(elements['history-stat-count'].textContent, '17 笔');
  assert.equal(elements['history-stat-amount'].textContent, '¥101.70');
  elements['history-year'].value = '2026';
  elements['history-year'].dispatch('change');
  assert.equal(elements['history-stat-count'].textContent, '16 笔');
  assert.equal(elements['history-stat-amount'].textContent, '¥1.70');
  elements['history-month'].value = '2';
  elements['history-month'].dispatch('change');
  assert.equal(elements['history-stat-count'].textContent, '0 笔');
  assert.equal(elements['history-stat-amount'].textContent, '¥0.00');
  elements['expense-history-trigger'].dispatch('click');
  assert.equal(elements['history-stat-count'].textContent, '1 笔');
  assert.equal(elements['history-stat-amount'].textContent, '¥24.50');
});

test('active record refresh rolls digits in both statistics without animating table rows', async () => {
  const page = makePage();
  page.setResponse([record(1, '2026-09-01T00:00:00+00:00')]);
  page.reload();
  await page.flush();
  const { elements } = page;
  assert.equal(elements['history-stat-count'].textContent, '1 笔');
  assert.equal(elements['history-stat-amount'].textContent, '¥10.00');
  const countReel = page.statWrappers.count.querySelector('.history-stat-reel');
  const amountReel = page.statWrappers.amount.querySelector('.history-stat-reel');
  assert.ok(countReel);
  assert.ok(amountReel);
  assert.equal(countReel['aria-hidden'], 'true');
  assert.equal(countReel.children.filter(child => child.className === 'history-reel-digit').length, 1);
  assert.equal(amountReel.children.filter(child => child.className === 'history-reel-digit').length, 4);
  assert.equal(countReel.children[0].children[0].children.at(-1).textContent, '1');
  assert.equal(elements['history-rows'].children[0].classList.contains('history-reel-row'), false);
  elements['history-year'].value = '2026';
  elements['history-year'].dispatch('change');
  assert.notEqual(page.statWrappers.count.querySelector('.history-stat-reel'), countReel);
  elements['history-month'].value = '9';
  elements['history-month'].dispatch('change');
  assert.equal(elements['history-rows'].children[0].classList.contains('history-reel-row'), false);
  elements['expense-history-trigger'].dispatch('click');
  assert.ok(page.statWrappers.amount.querySelector('.history-stat-reel'));
});

test('masked statistics and trash records do not receive reels', async () => {
  const free = makePage('active', false);
  free.setResponse([record(1, '2026-09-01T00:00:00+00:00')]);
  free.reload();
  await free.flush();
  assert.equal(free.statWrappers.count.querySelector('.history-stat-reel'), null);
  assert.equal(free.statWrappers.amount.querySelector('.history-stat-reel'), null);
  assert.equal(free.elements['history-rows'].children[0].classList.contains('history-reel-row'), false);
  const trash = makePage('trash');
  trash.setResponse([record(2, '2026-09-01T00:00:00+00:00')]);
  trash.reload();
  await trash.flush();
  assert.equal(trash.elements['history-rows'].children[0].classList.contains('history-reel-row'), false);
  assert.equal(trash.statWrappers.amount.querySelector('.history-stat-reel'), null);
});

test('reel styling is limited to statistics and preserves reduced-motion behavior', () => {
  const styles = fs.readFileSync(path.join(__dirname, '../static/ui-components.css'), 'utf8');
  assert.match(styles, /body\.history-body\[data-scope="active"\] \.history-reel-track/);
  assert.match(styles, /\.history-reel-track\s*\{[^}]*animation:\s*history-digit-roll 2s/);
  assert.doesNotMatch(styles, /\.history-reel-row/);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\.history-reel-track/);
  assert.doesNotMatch(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), /history-reel-track/);
});

test('Free statistics stay masked and respond to Pro upgrades and expiry', async () => {
  const page = makePage('active', false);
  page.setResponse([{ ...record(1, '2026-09-01T00:00:00+00:00'), reimbursement_amount: '99.75' }]);
  page.setStatsAllowed(false);
  page.reload();
  await page.flush();
  assert.equal(page.statsGrid.dataset.statsLocked, 'true');
  assert.equal(page.elements['history-stat-count'].textContent, '•••');
  assert.equal(page.elements['history-stat-amount'].textContent, '•••');
  page.setStatsAllowed(true);
  page.refreshAccess();
  await page.flush();
  assert.equal(page.statsGrid.dataset.statsLocked, 'false');
  assert.equal(page.elements['history-stat-amount'].textContent, '¥99.75');
  page.setStatsAllowed(false);
  page.refreshAccess();
  await page.flush();
  assert.equal(page.elements['history-stat-amount'].textContent, '•••');
});

test('license downgrade removes old history and ignores an earlier Pro list response', async () => {
  let pro = true;
  let listRequests = 0;
  let finishOldRequest;
  let finishFreeRequest;
  const page = makePage('active', true, url => {
    if (url === '/api/permissions') {
      return { reimbursement_stats: pro, history_retention_months: pro ? null : 6 };
    }
    listRequests += 1;
    if (listRequests === 1) return { reimbursements: [record(1, '2025-01-01T00:00:00+00:00')] };
    if (listRequests === 2) return new Promise(resolve => { finishOldRequest = resolve; });
    return new Promise(resolve => { finishFreeRequest = resolve; });
  });
  await page.flush();
  assert.deepEqual(page.elements['history-rows'].children.map(row => row.dataset.recordId), [1]);
  page.reload();
  await page.flush();
  pro = false;
  page.refreshAccess();
  await page.flush();
  assert.deepEqual(page.elements['history-rows'].children.map(row => row.dataset.recordId), []);
  finishFreeRequest({ reimbursements: [record(2, '2026-09-01T00:00:00+00:00')] });
  await page.flush();
  finishOldRequest({ reimbursements: [record(1, '2025-01-01T00:00:00+00:00')] });
  await page.flush();
  assert.deepEqual(page.elements['history-rows'].children.map(row => row.dataset.recordId), [2]);
});

test('switching record type isolates rows, counts, year options and pagination', async () => {
  const page = makePage();
  page.setResponse([
    record(1, '2025-01-01T00:00:00+00:00'),
    ...Array.from({ length: 16 }, (_, index) => ({
      ...record(index + 2, '2026-09-01T00:00:00+00:00'), form_type: 'expense',
    })),
  ]);
  page.reload();
  await page.flush();
  const { elements } = page;
  assert.deepEqual(elements['history-rows'].children.map(row => row.dataset.recordId), [1]);
  assert.equal(elements['record-count'].textContent, '1 条');
  assert.deepEqual(elements['history-year'].options.map(option => option.value), ['', '2025']);

  elements['expense-history-trigger'].dispatch('click');
  assert.equal(elements['record-count'].textContent, '16 条');
  assert.equal(elements['history-rows'].children.length, 15);
  assert.deepEqual(elements['history-year'].options.map(option => option.value), ['', '2026']);
  assert.equal(elements['expense-history-trigger']['aria-selected'], 'true');
  assert.equal(elements['travel-history-trigger']['aria-selected'], 'false');
  assert.equal(elements['records-heading'].textContent, '费用报销记录');
  elements['history-next-page'].dispatch('click');
  assert.equal(elements['history-page-status'].textContent, '2 / 2');
  elements['travel-history-trigger'].dispatch('click');
  assert.equal(elements['history-page-status'].textContent, '');
  assert.deepEqual(elements['history-rows'].children.map(row => row.dataset.recordId), [1]);
});

test('empty expense category has a category-specific empty state', async () => {
  const page = makePage();
  page.setResponse([record(1, '2026-09-01T00:00:00+00:00')]);
  page.reload();
  await page.flush();
  page.elements['expense-history-trigger'].dispatch('click');
  assert.equal(page.elements['history-empty'].hidden, false);
  assert.equal(page.elements['history-empty'].textContent, '暂无费用报销记录');
  assert.equal(page.elements['record-count'].textContent, '0 条');
});

test('history page keeps accessible filter names without visible labels', () => {
  const markup = fs.readFileSync(path.join(__dirname, '../templates/history.html'), 'utf8');
  assert.match(markup, /id="history-filters"[^>]*hidden/);
  assert.match(markup, /id="history-year" aria-label="按生成年份筛选"/);
  assert.match(markup, /id="history-month" aria-label="按生成月份筛选"/);
  assert.doesNotMatch(markup, /<label[^>]*for="history-(?:year|month)"/);
});

test('history defaults to all records and filters by generated time in Shanghai', async () => {
  const page = makePage();
  page.setResponse([
    record(1, '2025-12-31T15:59:00+00:00'),
    record(2, '2025-12-31T16:00:00+00:00'),
    record(3, '2026-08-01T00:00:00+00:00', '2026-09-30'),
  ]);
  page.reload();
  await page.flush();
  const { elements } = page;
  assert.equal(elements['record-count'].textContent, '3 条');
  assert.deepEqual(elements['history-rows'].children.map(row => row.dataset.recordId), [1, 2, 3]);
  assert.equal(elements['history-filters'].hidden, false);
  assert.deepEqual(elements['history-year'].options.map(option => option.value), ['', '2026', '2025']);

  elements['history-year'].value = '2026';
  elements['history-year'].dispatch('change');
  assert.deepEqual(elements['history-rows'].children.map(row => row.dataset.recordId), [2, 3]);
  elements['history-month'].value = '1';
  elements['history-month'].dispatch('change');
  assert.deepEqual(elements['history-rows'].children.map(row => row.dataset.recordId), [2]);
  assert.equal(elements['record-count'].textContent, '1 / 3 条');

  elements['history-year'].value = '';
  elements['history-year'].dispatch('change');
  assert.deepEqual(elements['history-rows'].children.map(row => row.dataset.recordId), [2]);
  elements['history-month'].value = '';
  elements['history-month'].dispatch('change');
  assert.equal(elements['record-count'].textContent, '3 条');
  assert.equal(page.calls.every(url => url === '/api/reimbursements?scope=active' || url === '/api/permissions'), true);
});

test('filtering resets pagination and preserves the selection after reloading records', async () => {
  const page = makePage();
  page.setResponse(Array.from({ length: 16 }, (_, index) =>
    record(index + 1, index === 15 ? '2025-01-01T00:00:00+00:00' : '2026-09-01T00:00:00+00:00')));
  page.reload();
  await page.flush();
  page.elements['history-next-page'].dispatch('click');
  assert.equal(page.elements['history-page-status'].textContent, '2 / 2');
  page.elements['history-year'].value = '2025';
  page.elements['history-year'].dispatch('change');
  assert.equal(page.elements['history-page-status'].textContent, '');
  assert.deepEqual(page.elements['history-rows'].children.map(row => row.dataset.recordId), [16]);
  page.setResponse([record(16, '2025-01-01T00:00:00+00:00'), record(17, '2026-09-01T00:00:00+00:00')]);
  page.reload();
  await page.flush();
  assert.equal(page.elements['history-year'].value, '2025');
  assert.deepEqual(page.elements['history-rows'].children.map(row => row.dataset.recordId), [16]);
});

test('month-only filtering handles empty results without changing record actions', async () => {
  const page = makePage();
  page.setResponse([
    record(1, '2025-09-01T00:00:00+00:00'),
    record(2, '2026-09-01T00:00:00+00:00'),
    record(3, 'invalid-time'),
  ]);
  page.reload();
  await page.flush();
  const { elements } = page;
  elements['history-month'].value = '9';
  elements['history-month'].dispatch('change');
  assert.deepEqual(elements['history-rows'].children.map(row => row.dataset.recordId), [1, 2]);
  const row = elements['history-rows'].children[0];
  const actions = row.children.at(-1).children[0].children;
  assert.deepEqual(actions.map(button => button.dataset.action), ['pdf', 'edit', 'trash']);
  const edit = actions[1];
  edit.closest = () => row;
  await elements['history-rows'].dispatch('click', {
    target: { closest: selector => selector === 'button[data-action]' ? edit : null },
  });
  assert.equal(elements['edit-frame'].src, '/?edit=1');
  assert.equal(elements['edit-dialog'].open, true);
  elements['history-month'].value = '2';
  elements['history-month'].dispatch('change');
  assert.equal(elements['history-empty'].hidden, false);
  assert.equal(elements['history-empty'].textContent, '该时间范围内暂无记录');
  assert.equal(elements['history-pagination'].hidden, true);
  assert.equal(elements['record-count'].textContent, '0 / 3 条');
});

test('trash view keeps its existing unfiltered list', async () => {
  const page = makePage('trash');
  page.setResponse([record(1, '2025-01-01T00:00:00+00:00')]);
  page.reload();
  await page.flush();
  assert.equal(page.elements['history-filters'].hidden, true);
  assert.equal(page.elements['record-count'].textContent, '1 条');
  assert.equal(page.calls.at(-1), '/api/reimbursements?scope=trash');
});
