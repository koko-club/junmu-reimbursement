const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function registrationForm(submitError) {
  const values = {
    username: 'newuser', password: '12345', confirm_password: '12345',
    real_name: '张三', department: '技术部'
  };
  const fields = Object.fromEntries(Object.entries(values).map(([name, value]) => [name, {
    name, value, previousElementSibling: { textContent: name },
    focus() { this.focused = true; },
    setAttribute(key, value) { this[key] = value; },
    removeAttribute(key) { delete this[key]; }
  }]));
  const errors = Object.fromEntries(Object.keys(values).map((name) => [name, { textContent: '' }]));
  const button = { disabled: false };
  const form = {
    dataset: { endpoint: '/api/register' },
    querySelector(selector) {
      if (selector === 'button[type="submit"]') return button;
      const name = selector.match(/^\[name="([^"]+)"\]$/);
      if (name) return fields[name[1]];
      const error = selector.match(/^\[data-field-error="([^"]+)"\]$/);
      if (error) return errors[error[1]];
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[required]') return Object.values(fields);
      if (selector === '[data-field-error]') return Object.values(errors);
      if (selector === '[aria-invalid="true"]') return Object.values(fields).filter((field) => field['aria-invalid'] === 'true');
      if (selector === 'input[type="password"]') return [fields.password, fields.confirm_password];
      return [];
    },
    addEventListener(name, handler) { if (name === 'submit') this.submit = handler; }
  };
  const context = {
    document: { querySelectorAll: () => [form] },
    FormData: class { constructor() { return Object.entries(values); } },
    window: { reimbursementApi: async () => { throw submitError; } },
    showFormMessage: (_form, message) => { form.message = message; },
    Date, Math, clearInterval, setInterval
  };
  const script = fs.readFileSync(path.join(__dirname, '../static/auth.js'), 'utf8');
  vm.runInNewContext(script, context);
  return { form, fields, errors };
}

test('registration API field error appears beside the matching input', async () => {
  const error = new Error('密码至少需要6位');
  error.field = 'password';
  const { form, fields, errors } = registrationForm(error);
  await form.submit({ preventDefault() {} });
  assert.equal(errors.password.textContent, '密码至少需要6位');
  assert.equal(fields.password.focused, true);
  assert.equal(fields.password['aria-invalid'], 'true');
});
