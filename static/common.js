(function () {
  'use strict';

  window.reimbursementApi = async function (form, payload) {
    const headers = { 'Content-Type': 'application/json' };
    const csrf = document.body.dataset.csrf;
    if (csrf) headers['X-CSRF-Token'] = csrf;
    const response = await fetch(form.dataset.endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      headers: headers,
      body: JSON.stringify(payload)
    });
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (response.status === 401) {
      window.location.assign('/login');
      throw new Error(data.error || '登录已失效');
    }
    if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试');
    return data;
  };

  window.showFormMessage = function (form, text, kind) {
    const message = form.querySelector('[data-message]');
    if (!message) return;
    message.textContent = text || '';
    message.className = 'form-message' + (kind ? ' ' + kind : '');
  };
}());
