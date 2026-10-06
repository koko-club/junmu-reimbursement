const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');

test('login lock response displays a live countdown without redirecting', async () => {
  let now = 0;
  let submitHandler;
  let timer;
  let redirects = 0;
  const message = { textContent: '', className: '' };
  const submit = { disabled: false };
  const username = { value: 'admin', previousElementSibling: { textContent: '账号' } };
  const password = { value: 'wrong', previousElementSibling: { textContent: '密码' } };
  const fields = [username, password];
  const form = {
    dataset: { endpoint: '/api/login' },
    querySelector(selector) {
      if (selector === 'button[type="submit"]') return submit;
      if (selector === '[data-message]') return message;
      if (selector === '[name="confirm_password"]') return null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[required]') return fields;
      if (selector === 'input[type="password"]') return [password];
      return [];
    },
    addEventListener(type, handler) { if (type === 'submit') submitHandler = handler; },
  };
  const context = {
    location: { assign() { redirects += 1; } },
    document: {
      body: { dataset: { csrf: 'csrf' } },
      querySelectorAll() { return [form]; },
      getElementById() { return null; },
    },
    FormData: class { constructor() { return [['username', username.value], ['password', password.value]]; } },
    Headers: class { set() {} },
    Date: { now() { return now; } },
    setInterval(callback) { timer = callback; return 1; },
    clearInterval() { timer = null; },
    fetch: async () => ({
      status: 429,
      ok: false,
      headers: { get(name) { return name === 'Retry-After' ? '60' : null; } },
      json: async () => ({ error: '登录失败 5 次，已锁定 60 秒', lock_seconds: 60 }),
    }),
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/common.js'), 'utf8'), context);
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/auth.js'), 'utf8'), context);

  await submitHandler({ preventDefault() {} });
  assert.equal(redirects, 0);
  assert.equal(message.textContent, '登录失败 5 次，已锁定 60 秒');
  assert.equal(submit.disabled, true);
  now = 1000;
  timer();
  assert.equal(message.textContent, '登录失败 5 次，已锁定 59 秒');
  now = 60000;
  timer();
  assert.equal(submit.disabled, false);
  assert.equal(message.textContent, '');
});

test('wrong login credentials keep the error on the login page', async () => {
  let redirects = 0;
  const context = {
    location: { assign() { redirects += 1; } },
    document: { body: { dataset: { csrf: 'csrf' } }, getElementById() { return null; } },
    Headers: class { set() {} },
    fetch: async () => ({ status: 401, ok: false, json: async () => ({ error: '用户名或密码错误' }) }),
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/common.js'), 'utf8'), context);
  await assert.rejects(context.apiFetch('/api/login', { method: 'POST' }), /用户名或密码错误/);
  assert.equal(redirects, 0);
});

test('IP rate limit displays the Retry-After countdown on the login page', async () => {
  let now = 0;
  let submitHandler;
  let timer;
  const message = { textContent: '', className: '' };
  const submit = { disabled: false };
  const username = { value: '111', name: 'username', previousElementSibling: { textContent: '账号' } };
  const password = { value: 'wrong', name: 'password', previousElementSibling: { textContent: '密码' } };
  const form = {
    dataset: { endpoint: '/api/login' },
    querySelector(selector) {
      if (selector === 'button[type="submit"]') return submit;
      if (selector === '[data-message]') return message;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[required]') return [username, password];
      if (selector === 'input[type="password"]') return [password];
      return [];
    },
    addEventListener(type, handler) { if (type === 'submit') submitHandler = handler; },
  };
  const context = {
    location: { assign() {} },
    document: {
      body: { dataset: { csrf: 'csrf' } },
      querySelectorAll() { return [form]; },
      getElementById() { return null; },
    },
    FormData: class { constructor() { return [['username', username.value], ['password', password.value]]; } },
    Headers: class { set() {} },
    Date: { now() { return now; } },
    setInterval(callback) { timer = callback; return 1; },
    clearInterval() { timer = null; },
    fetch: async () => ({
      status: 429,
      ok: false,
      headers: { get(name) { return name === 'Retry-After' ? '37' : null; } },
      json: async () => ({ error: '请求过于频繁，请稍后重试' }),
    }),
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/common.js'), 'utf8'), context);
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/auth.js'), 'utf8'), context);

  await submitHandler({ preventDefault() {} });
  assert.equal(message.textContent, '登录请求过于频繁，请在 37 秒后重试');
  assert.equal(submit.disabled, true);
  now = 1000;
  timer();
  assert.equal(message.textContent, '登录请求过于频繁，请在 36 秒后重试');
  now = 37000;
  timer();
  assert.equal(message.textContent, '');
  assert.equal(submit.disabled, false);
});
