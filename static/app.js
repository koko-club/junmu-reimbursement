(function () {
  'use strict';
  const form = document.getElementById('reimbursement-form');
  const result = document.getElementById('result');
  const errorBox = document.getElementById('error');
  const submit = document.getElementById('submit');
  const mileageDialog = document.getElementById('mileage-development-dialog');
  const mileageTrigger = document.getElementById('mileage-development-trigger');
  const mileageClose = document.getElementById('mileage-development-close');
  const monthCount = document.getElementById('month-count');
  const yearCount = document.getElementById('year-count');
  const yearAmount = document.getElementById('year-amount');
  let files = [];
  const EXPECTED_ROW_COUNT = 11;
  const numericFields = ['public_amount', 'mileage', 'toll', 'lodging', 'receipts'];
  const detailDateFields = Array.from(document.querySelectorAll('#detail-rows input[data-field="date"]'));

  function syncDetailDateVisibility(field) {
    field.classList.toggle('has-value', field.value.trim() !== '');
  }

  detailDateFields.forEach(function (field) {
    field.value = '';
    field.setAttribute('autocomplete', 'off');
    syncDetailDateVisibility(field);
    field.addEventListener('input', function () { syncDetailDateVisibility(field); });
    field.addEventListener('change', function () { syncDetailDateVisibility(field); });
  });

  async function loadSession() {
    const data = await apiFetch('/api/session');
    document.getElementById('traveler').value = data.user.real_name;
    document.getElementById('department').value = data.user.department;
    document.body.dataset.csrf = data.csrf_token;
  }

  const amountFormatter = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function renderStats(data) {
    const month = Number.isInteger(data.month_count) && data.month_count >= 0 ? data.month_count : 0;
    const year = Number.isInteger(data.year_count) && data.year_count >= 0 ? data.year_count : 0;
    const amount = Number(data.year_amount);
    monthCount.textContent = month + ' 笔';
    yearCount.textContent = year + ' 笔';
    yearAmount.textContent = Number.isFinite(amount) ? '¥' + amountFormatter.format(amount) : '¥0.00';
  }
  async function loadStats() {
    try {
      renderStats(await apiFetch('/api/reimbursements/stats'));
    } catch (_) {
      // Keep the dashboard informative while a transient request is retried.
      monthCount.textContent = '0 笔'; yearCount.textContent = '0 笔'; yearAmount.textContent = '¥0.00';
    }
  }

  function normalizeDateInput(value) {
    return String(value || '').trim().replace(/\//g, '-');
  }

  function message(text, kind) {
    errorBox.className = 'result ' + (kind || 'error');
    errorBox.textContent = text;
  }

  function collectPayload() {
    const payload = {};
    ['date', 'department', 'traveler', 'reason', 'days', 'allowance'].forEach(function (id) {
      const field = document.getElementById(id);
      payload[id] = id === 'date' ? normalizeDateInput(field.value) : field.value.trim();
    });
    payload.days = Number(payload.days);
    payload.allowance = Number(payload.allowance);
    const rows = Array.from(document.querySelectorAll('#detail-rows .detail-row'));
    if (rows.length !== EXPECTED_ROW_COUNT) throw new Error('明细行必须为 11 行');
    payload.rows = rows.map(function (row) {
      const data = {};
      row.querySelectorAll('[data-field]').forEach(function (field) {
        if (field.value.trim() !== '') data[field.dataset.field] = field.dataset.field === 'date' ? normalizeDateInput(field.value) : field.value.trim();
      });
      return data;
    });
    return payload;
  }

  function validate(payload) {
    ['department', 'traveler', 'reason'].forEach(function (id) {
      if (!payload[id]) throw new Error(id + ' 为必填项');
    });
    ['days', 'allowance'].forEach(function (id) {
      const raw = document.getElementById(id).value.trim();
      if (!raw) throw new Error(id + ' 为必填项');
      if (!Number.isFinite(payload[id]) || payload[id] < 0) throw new Error(id + ' 必须是非负数字');
    });
    payload.rows.forEach(function (row, index) {
      const hasValues = Object.keys(row).length > 0;
      if (!hasValues) return;
      if (!row.origin || !row.destination) throw new Error('第 ' + (index + 1) + ' 行需填写起点和终点');
      numericFields.forEach(function (field) {
        if (row[field] === undefined) return;
        const value = Number(row[field]);
        if (!Number.isFinite(value) || value < 0 || (field === 'receipts' && !Number.isInteger(value))) {
          throw new Error('第 ' + (index + 1) + ' 行 ' + field + ' 必须是非负数字');
        }
        row[field] = value;
      });
    });
  }

  /* Original mileage screenshot selection is intentionally kept for reactivation.
  const fileInput = document.getElementById('screenshots');
  const previewList = document.getElementById('preview-list');
  const MAX_FILE_BYTES = 5 * 1024 * 1024;
  const MAX_TOTAL_BYTES = 10 * 1024 * 1024;

  function renderPreviews() {
    previewList.querySelectorAll('img[data-object-url]').forEach(function (image) { URL.revokeObjectURL(image.dataset.objectUrl); });
    previewList.textContent = '';
    files.forEach(function (file, index) {
      const item = document.createElement('div');
      item.className = 'preview-item';
      const image = document.createElement('img');
      image.alt = file.name;
      image.dataset.objectUrl = URL.createObjectURL(file);
      image.src = image.dataset.objectUrl;
      const label = document.createElement('span');
      label.textContent = (index + 1) + '. ' + file.name;
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'remove-file'; remove.textContent = '移除';
      remove.addEventListener('click', function () { files.splice(index, 1); renderPreviews(); syncInput(); });
      item.append(image, label, remove); previewList.appendChild(item);
    });
  }

  function syncInput() {
    const transfer = new DataTransfer(); files.forEach(function (file) { transfer.items.add(file); }); fileInput.files = transfer.files;
  }

  fileInput.addEventListener('change', function () {
    const selected = Array.from(fileInput.files);
    for (const file of selected) {
      if (!/\.(jpe?g|png)$/i.test(file.name)) { message('仅支持 JPG、JPEG 或 PNG 文件'); continue; }
      if (file.size > MAX_FILE_BYTES) { message('文件 ' + file.name + ' 超过单文件 5 MB 限制'); continue; }
      if (files.reduce((sum, entry) => sum + entry.size, 0) + file.size > MAX_TOTAL_BYTES) { message('截图总大小不能超过 10 MB'); continue; }
      files.push(file);
    }
    syncInput(); renderPreviews();
  });
  */

  mileageTrigger.addEventListener('click', function () {
    mileageDialog.showModal();
  });
  mileageClose.addEventListener('click', function () {
    mileageDialog.close();
  });
  mileageDialog.addEventListener('click', function (event) {
    if (event.target === mileageDialog) mileageDialog.close();
  });

  form.addEventListener('reset', function () {
    detailDateFields.forEach(function (field) { field.value = ''; syncDetailDateVisibility(field); });
    files = []; result.className = 'result'; result.textContent = ''; errorBox.className = 'result error'; errorBox.textContent = '';
  });
  form.addEventListener('submit', async function (event) {
    event.preventDefault(); result.textContent = ''; result.className = 'result'; errorBox.textContent = ''; errorBox.className = 'result error';
    let payload;
    try { payload = collectPayload(); validate(payload); } catch (error) { message(error.message); return; }
    const body = new FormData(); body.append('payload', JSON.stringify(payload)); files.forEach(function (file) { body.append('screenshots', file, file.name); });
    submit.disabled = true; submit.dataset.loading = 'true'; submit.textContent = '生成中…';
    try {
      const data = await apiFetch('/api/reimbursements/generate', { method: 'POST', body: body });
      result.className = 'result success';
      const successStatus = document.createElement('span');
      successStatus.className = 'result-success-status';
      successStatus.setAttribute('role', 'img');
      successStatus.setAttribute('aria-label', '生成成功');
      const successIcon = document.createElement('svg');
      successIcon.className = 'ui-icon result-success-icon';
      successIcon.setAttribute('aria-hidden', 'true');
      const successUse = document.createElement('use');
      successUse.setAttribute('href', '/static/icons.svg#check');
      successIcon.appendChild(successUse);
      successStatus.appendChild(successIcon);
      result.appendChild(successStatus);
      [['xlsx_url', '下载 Excel'], ['pdf_url', '下载 PDF']].forEach(function (entry) {
        const link = document.createElement('a');
        link.className = 'table-action-button';
        link.href = data[entry[0]];
        link.textContent = entry[1];
        if (entry[0] === 'pdf_url') {
          link.target = '_blank';
          link.rel = 'noopener';
        }
        result.appendChild(link);
      });
      await loadStats();
    } catch (error) { message(error.message || '生成失败'); }
    finally { submit.disabled = false; delete submit.dataset.loading; submit.textContent = '生成 Excel + PDF'; }
  });
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('app-sidebar');
  toggle.addEventListener('click', function () { const open = sidebar.classList.toggle('open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.getElementById('logout').addEventListener('click', async function () {
    try { await apiFetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); window.location.assign('/login'); } catch (_) {}
  });
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') loadStats(); });
  window.addEventListener('storage', function (event) { if (event.key === 'reimbursement-history-updated') loadStats(); });
  loadSession().catch(function () {});
  loadStats();
})();
