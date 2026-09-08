(function () {
  'use strict';

  window.apiFetch = async function (path, options) {
    const request = Object.assign({ credentials: 'same-origin' }, options || {});
    request.method = request.method || 'GET';
    request.credentials = 'same-origin';
    const headers = new Headers(request.headers || {});
    const csrf = document.body.dataset.csrf;
    if (csrf && request.method !== 'GET') headers.set('X-CSRF-Token', csrf);
    request.headers = headers;
    const response = await fetch(path, request);
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (response.status === 401) {
      window.location.assign('/login');
      throw new Error(data.error || '登录已失效');
    }
    if (response.status === 403 && data.next) {
      window.location.assign(data.next);
      throw new Error(data.error || '请先完成必要操作');
    }
    if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试');
    return data;
  };

  window.reimbursementApi = async function (form, payload) {
    return window.apiFetch(form.dataset.endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  };

  window.showFormMessage = function (form, text, kind) {
    const message = form.querySelector('[data-message]');
    if (!message) return;
    message.textContent = text || '';
    message.className = 'form-message' + (kind ? ' ' + kind : '');
  };
}());
