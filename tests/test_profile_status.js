const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../static/profile.js'), 'utf8');

function mountProfile({ license, session, permissions, travelDays } = {}) {
  const elements = new Map();
  const makeElement = () => ({
    value: '', textContent: '', hidden: false, disabled: false, open: false, focused: false, selected: false, dataset: {}, listeners: {}, children: [],
    attributes: new Map(),
    classList: { toggle() { return false; } },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    focus() { this.focused = true; },
    select() { this.selected = true; },
    scrollIntoView() {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute(name, value) { this.attributes.set(name, String(value)); },
    removeAttribute(name) { this.attributes.delete(name); },
    hasAttribute(name) { return this.attributes.has(name); },
  });
  const element = id => {
    if (!elements.has(id)) {
      const node = makeElement();
      if (id === 'travel-chart') node.setAttribute('hidden', '');
      elements.set(id, node);
    }
    return elements.get(id);
  };
  const requests = [];
  let access = permissions || { travel_trend: false };
  let currentTravelDays = travelDays;
  const apiFetch = (url, options) => {
    requests.push({ url, options });
    if (url === '/api/session') return session || Promise.resolve({ user: {
      username: 'alice', real_name: '测试用户', department: '财务部', status: 'active'
    } });
    if (url === '/api/license') return Promise.resolve(license);
    if (url === '/api/license/import') {
      access = { travel_trend: true };
      return Promise.resolve({ tier: 'pro', serial: 'a'.repeat(32) });
    }
    if (url === '/api/permissions') return Promise.resolve(access);
    if (url === '/api/travel-days') return Promise.resolve(currentTravelDays || { months: [], excluded_records: 0 });
    throw new Error(`Unexpected request: ${url}`);
  };
  const document = {
    visibilityState: 'visible', listeners: {},
    addEventListener(name, handler) { this.listeners[name] = handler; },
    getElementById: element, createElement: makeElement, createElementNS: makeElement,
  };
  const window = { location: { assign() {} } };
  vm.runInNewContext(script, {
    document, window, apiFetch, Intl, Date, Math, Set, Number, Array, Object,
  });
  return {
    element, requests,
    setPermissions(next) { access = next; },
    setTravelDays(next) { currentTravelDays = next; },
    refreshAccess() { document.listeners.visibilitychange(); },
  };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('account badge follows license tier even when profile loads later', async () => {
  let finishSession;
  const session = new Promise(resolve => { finishSession = resolve; });
  const page = mountProfile({ license: { tier: 'basic', serial: 'a'.repeat(32) }, session });
  await settle();
  assert.equal(page.element('profile-status').textContent, '普通用户');
  assert.equal(page.element('profile-status').dataset.tier, 'basic');
  finishSession({ user: { username: 'alice', real_name: '测试用户', department: '财务部', status: 'active' } });
  await settle();
  assert.equal(page.element('profile-status').textContent, '普通用户');
});

test('account badge shows Pro tier and updates immediately after import', async () => {
  const pro = mountProfile({ license: { tier: 'pro', serial: 'a'.repeat(32) } });
  await settle();
  assert.equal(pro.element('profile-status').textContent, 'Pro用户');
  assert.equal(pro.element('profile-status').dataset.tier, 'pro');

  const basic = mountProfile({ license: { tier: 'basic', serial: 'a'.repeat(32) } });
  await settle();
  basic.element('license-code').value = 'signed-code';
  await basic.element('license-import-form').listeners.submit({ preventDefault() {} });
  assert.equal(basic.element('profile-status').textContent, 'Pro用户');
  assert.equal(basic.element('profile-status').dataset.tier, 'pro');
  assert.equal(basic.element('travel-section').dataset.trendLocked, 'false');
  assert.equal(basic.requests.some(request => request.url === '/api/travel-days'), true);
});

test('account badge reports when authorization state cannot be loaded', async () => {
  const page = mountProfile({ license: Promise.reject(new Error('network unavailable')) });
  await settle();
  assert.equal(page.element('profile-status').textContent, '授权状态加载失败');
});

test('buy button opens plan comparison and close button dismisses it', async () => {
  const page = mountProfile({ license: { tier: 'basic', serial: 'a'.repeat(32) } });
  const dialog = page.element('license-plans-dialog');
  page.element('license-buy').listeners.click();
  assert.equal(dialog.open, true);
  page.element('license-plans-close').listeners.click();
  assert.equal(dialog.open, false);
});

test('upgrade action reveals the payment code inside the still-open comparison dialog', async () => {
  const page = mountProfile({ license: { tier: 'basic', serial: 'a'.repeat(32) } });
  await settle();
  const dialog = page.element('license-plans-dialog');
  page.element('license-buy').listeners.click();
  page.element('license-plans-upgrade').listeners.click();
  assert.equal(dialog.open, true);
  assert.equal(dialog.dataset.view, 'payment');
  assert.equal(page.element('license-plans-grid').inert, true);
  assert.equal(page.element('license-payment').inert, false);
  assert.equal(page.element('license-payment').focused, true);
  assert.equal(page.element('license-serial').focused, false);
});

test('payment view returns to comparison and resets after closing', async () => {
  const page = mountProfile({ license: { tier: 'basic', serial: 'a'.repeat(32) } });
  const dialog = page.element('license-plans-dialog');
  page.element('license-buy').listeners.click();
  page.element('license-plans-upgrade').listeners.click();
  page.element('license-payment-back').listeners.click();
  assert.equal(dialog.dataset.view, 'compare');
  assert.equal(page.element('license-plans-grid').inert, false);
  assert.equal(page.element('license-payment').inert, true);
  page.element('license-plans-upgrade').listeners.click();
  page.element('license-plans-close').listeners.click();
  dialog.listeners.close();
  assert.equal(dialog.open, false);
  assert.equal(dialog.dataset.view, 'compare');
  page.element('license-buy').listeners.click();
  assert.equal(dialog.dataset.view, 'compare');
});

test('payment view can return to the serial for obtaining a license code', async () => {
  const page = mountProfile({ license: { tier: 'basic', serial: 'a'.repeat(32) } });
  await settle();
  page.element('license-buy').listeners.click();
  page.element('license-plans-upgrade').listeners.click();
  page.element('license-payment-serial').listeners.click();
  assert.equal(page.element('license-plans-dialog').open, false);
  assert.equal(page.element('license-serial').focused, true);
  assert.equal(page.element('license-serial').selected, true);
  assert.match(page.element('license-message').textContent, /序列号.*授权码/);
});

test('Free sees a locked travel trend and never requests trend data', async () => {
  const page = mountProfile({ license: { tier: 'basic', serial: 'a'.repeat(32) }, permissions: { travel_trend: false } });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendLocked, 'true');
  assert.equal(page.element('travel-pro-overlay').hidden, false);
  assert.equal(page.element('travel-chart').hasAttribute('hidden'), true);
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), false);
  assert.equal(page.element('travel-summary-value').textContent, '•••');
  assert.equal(page.requests.some(request => request.url === '/api/travel-days'), false);
});

test('unknown access starts with a loading card instead of a Pro upsell', async () => {
  let finishAccess;
  const access = new Promise(resolve => { finishAccess = resolve; });
  const page = mountProfile({
    license: { tier: 'pro', serial: 'a'.repeat(32) },
    permissions: access,
  });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendState, 'loading');
  assert.equal(page.element('travel-pro-overlay').hidden, true);
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), false);
  finishAccess({ travel_trend: false });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendState, 'locked');
  assert.equal(page.element('travel-pro-overlay').hidden, false);
});

test('permission request failure keeps the card and offers a working retry', async () => {
  const page = mountProfile({
    license: { tier: 'pro', serial: 'a'.repeat(32) },
    permissions: Promise.reject(new Error('offline')),
    travelDays: { months: [{ year: 2026, month: 9, days: 4 }], excluded_records: 0 },
  });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendState, 'error');
  assert.equal(page.element('travel-pro-overlay').hidden, true);
  assert.equal(page.element('travel-error').hidden, false);
  assert.equal(page.element('travel-retry').hidden, false);
  page.setPermissions({ travel_trend: true });
  await page.element('travel-retry').listeners.click();
  assert.equal(page.element('travel-section').dataset.trendState, 'ready');
  assert.equal(page.element('travel-retry').hidden, true);
});

test('Pro sees trend data, then expiry locks and clears it', async () => {
  const page = mountProfile({
    license: { tier: 'pro', serial: 'a'.repeat(32) },
    permissions: { travel_trend: true },
    travelDays: { months: [{ year: 2026, month: 9, days: 4 }], excluded_records: 0 },
  });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendLocked, 'false');
  assert.equal(page.element('travel-pro-overlay').hidden, true);
  assert.equal(page.element('travel-chart').hasAttribute('hidden'), false);
  assert.equal(page.requests.some(request => request.url === '/api/travel-days'), true);
  page.element('travel-month').value = '9';
  page.refreshAccess();
  assert.equal(page.element('travel-section').dataset.trendState, 'loading');
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), false);
  await settle();
  assert.equal(page.element('travel-month').value, '9');
  page.setPermissions({ travel_trend: false });
  page.refreshAccess();
  await settle();
  assert.equal(page.element('travel-section').dataset.trendLocked, 'true');
  assert.equal(page.element('travel-chart').hasAttribute('hidden'), true);
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), false);
  assert.equal(page.element('travel-chart').children.length, 0);
  assert.equal(page.element('travel-summary-value').textContent, '•••');
});

test('Pro reveals the real SVG chart and hides the locked placeholder', async () => {
  const page = mountProfile({
    license: { tier: 'pro', serial: 'a'.repeat(32) },
    permissions: { travel_trend: true },
    travelDays: { months: [{ year: 2026, month: 9, days: 137 }], excluded_records: 0 },
  });
  await settle();
  assert.equal(page.element('travel-chart').hasAttribute('hidden'), false);
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), true);
  assert.equal(page.element('travel-chart').children.some(node => node.attributes.get('class') === 'travel-line'), true);
});

test('Pro keeps the full chart placeholder while travel data loads, then reveals data', async () => {
  let finishTrend;
  const trend = new Promise(resolve => { finishTrend = resolve; });
  const page = mountProfile({
    license: { tier: 'pro', serial: 'a'.repeat(32) },
    permissions: { travel_trend: true },
    travelDays: trend,
  });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendState, 'loading');
  assert.equal(page.element('travel-chart').hasAttribute('hidden'), true);
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), false);
  assert.equal(page.element('travel-month-placeholder').hidden, false);
  assert.equal(page.element('travel-pro-overlay').hidden, true);
  assert.equal(page.element('travel-loading').hidden, false);

  finishTrend({ months: [{ year: 2026, month: 9, days: 137 }], excluded_records: 0 });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendState, 'ready');
  assert.equal(page.element('travel-chart').hasAttribute('hidden'), false);
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), true);
  assert.equal(page.element('travel-month-placeholder').hidden, true);
  assert.equal(page.element('travel-loading').hidden, true);
});

test('reloading Pro travel data replaces the old chart with the placeholder', async () => {
  const page = mountProfile({
    license: { tier: 'pro', serial: 'a'.repeat(32) },
    permissions: { travel_trend: true },
    travelDays: { months: [{ year: 2026, month: 9, days: 4 }], excluded_records: 0 },
  });
  await settle();
  let finishTrend;
  page.setTravelDays(new Promise(resolve => { finishTrend = resolve; }));
  page.element('license-code').value = 'renewal-code';
  const renewal = page.element('license-import-form').listeners.submit({ preventDefault() {} });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendState, 'loading');
  assert.equal(page.element('travel-chart').hasAttribute('hidden'), true);
  assert.equal(page.element('travel-trend-placeholder').hasAttribute('hidden'), false);
  assert.equal(page.element('travel-month-list').hidden, true);
  assert.equal(page.element('travel-summary-value').textContent, '•••');
  finishTrend({ months: [{ year: 2026, month: 9, days: 5 }], excluded_records: 0 });
  await renewal;
  assert.equal(page.element('travel-section').dataset.trendState, 'ready');
});

test('late Pro trend response cannot reveal data after access expires', async () => {
  let finishTrend;
  const trend = new Promise(resolve => { finishTrend = resolve; });
  const page = mountProfile({
    license: { tier: 'pro', serial: 'a'.repeat(32) },
    permissions: { travel_trend: true },
    travelDays: trend,
  });
  await settle();
  page.setPermissions({ travel_trend: false });
  page.refreshAccess();
  await settle();
  finishTrend({ months: [{ year: 2026, month: 9, days: 99 }], excluded_records: 0 });
  await settle();
  assert.equal(page.element('travel-section').dataset.trendLocked, 'true');
  assert.equal(page.element('travel-chart').children.length, 0);
  assert.equal(page.element('travel-summary-value').textContent, '•••');
});
