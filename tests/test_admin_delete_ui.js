const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class Element {
  constructor() {
    this.dataset = {};
    this.children = [];
    this.listeners = {};
    this.hidden = false;
    this._textContent = '';
  }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }
  get textContent() { return this.children.length ? this.children.map(child => child.textContent).join('') : this._textContent; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  closest(selector) {
    if (selector === 'button[data-action]') return this.dataset.action ? this : null;
    if (selector === 'tr') return this.parent && this.parent.parent;
    return null;
  }
  showModal() { this.open = true; }
  close() { this.open = false; if (this.listeners.close) this.listeners.close(); }
  focus() { this.focused = true; }
}

function mount(apiFetch) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const profileForm = new Element();
  profileForm.elements = { user_id: { value: '' }, real_name: { value: '' }, department: { value: '' } };
  profileForm.querySelector = () => new Element();
  const verifyForm = get('delete-verify-form');
  verifyForm.elements = { password: new Element() };
  verifyForm.reset = () => { verifyForm.elements.password.value = ''; };
  const submit = new Element();
  verifyForm.querySelector = () => submit;
  const selectorElements = new Map([
    ['[data-profile-form]', profileForm],
    ['[data-copy-password]', new Element()],
    ['[data-delete-cancel]', new Element()],
    ['[data-delete-continue]', new Element()],
    ['[data-delete-verify-cancel]', new Element()],
  ]);
  const document = {
    getElementById: get,
    querySelector: selector => selectorElements.get(selector),
    createElement: () => new Element(),
  };
  get('app-sidebar').classList = { toggle: () => false };
  get('sidebar-toggle').setAttribute = () => {};
  const source = fs.readFileSync(path.join(__dirname, '../static/admin.js'), 'utf8');
  vm.runInNewContext(source, {
    document, apiFetch, navigator: {},
    window: { confirm: () => true, location: { assign() {} } },
  });
  return { get, selectorElements, verifyForm, submit };
}

test('cancel stops deletion; confirmation requests current admin password before API call', async () => {
  const requests = [];
  const ui = mount(async (path, options) => {
    if (options) requests.push({ path, body: JSON.parse(options.body) });
    if (path === '/api/admin/users') return { users: [{ id: 7, username: 'alice', real_name: '张三', department: '技术部', status: 'active' }] };
    if (path === '/api/admin/registrations') return { users: [] };
    return { message: '已删除' };
  });
  await new Promise(resolve => setImmediate(resolve));
  const rows = ui.get('users-rows');
  const deleteButton = rows.children[0].children[4].children.find(button => button.dataset.action === 'delete');
  rows.listeners.click({ target: deleteButton });
  assert.equal(ui.get('delete-warning-dialog').open, true);
  assert.equal(ui.get('delete-target-name').textContent, 'alice');
  ui.selectorElements.get('[data-delete-cancel]').listeners.click();
  assert.equal(requests.length, 0);
  rows.listeners.click({ target: deleteButton });
  ui.selectorElements.get('[data-delete-continue]').listeners.click();
  assert.equal(ui.get('delete-verify-dialog').open, true);
  assert.equal(requests.length, 0);
  ui.verifyForm.elements.password.value = 'administrator1';
  await ui.verifyForm.listeners.submit({ preventDefault() {} });
  assert.deepEqual(requests, [{ path: '/api/admin/users/7/delete', body: { password: 'administrator1' } }]);
  assert.equal(ui.get('delete-verify-dialog').open, false);
  assert.equal(ui.verifyForm.elements.password.value, '');
});
