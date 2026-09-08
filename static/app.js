(function () {
  'use strict';
  const form = document.getElementById('reimbursement-form');
  const fileInput = document.getElementById('screenshots');
  const previewList = document.getElementById('preview-list');
  const result = document.getElementById('result');
  const errorBox = document.getElementById('error');
  const submit = document.getElementById('submit');
  let files = [];
  const MAX_FILE_BYTES = 5 * 1024 * 1024;
  const MAX_TOTAL_BYTES = 10 * 1024 * 1024;
  const EXPECTED_ROW_COUNT = 11;
  const numericFields = ['public_amount', 'mileage', 'toll', 'lodging', 'receipts'];

  async function loadSession() {
    const data = await apiFetch('/api/session');
    document.getElementById('traveler').value = data.user.real_name;
    document.getElementById('department').value = data.user.department;
    document.body.dataset.csrf = data.csrf_token;
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

  form.addEventListener('reset', function () { files = []; renderPreviews(); result.className = 'result'; result.textContent = ''; errorBox.className = 'result error'; errorBox.textContent = ''; });
  form.addEventListener('submit', async function (event) {
    event.preventDefault(); result.textContent = ''; result.className = 'result'; errorBox.textContent = ''; errorBox.className = 'result error';
    let payload;
    try { payload = collectPayload(); validate(payload); } catch (error) { message(error.message); return; }
    const body = new FormData(); body.append('payload', JSON.stringify(payload)); files.forEach(function (file) { body.append('screenshots', file, file.name); });
    submit.disabled = true; submit.dataset.loading = 'true'; submit.textContent = '生成中…';
    try {
      const data = await apiFetch('/api/reimbursements/generate', { method: 'POST', body: body });
      result.className = 'result success';
      const strong = document.createElement('strong'); strong.textContent = '生成成功'; result.appendChild(strong);
      [['xlsx_url', '下载 Excel'], ['pdf_url', '下载 PDF']].forEach(function (entry) {
        const link = document.createElement('a');
        link.href = data[entry[0]];
        link.textContent = entry[1];
        link.style.marginLeft = '12px';
        if (entry[0] === 'pdf_url') {
          link.target = '_blank';
          link.rel = 'noopener';
        }
        result.appendChild(link);
      });
    } catch (error) { message(error.message || '生成失败'); }
    finally { submit.disabled = false; delete submit.dataset.loading; submit.textContent = '生成 Excel + PDF'; }
  });
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('app-sidebar');
  toggle.addEventListener('click', function () { const open = sidebar.classList.toggle('open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.getElementById('logout').addEventListener('click', async function () {
    try { await apiFetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); window.location.assign('/login'); } catch (_) {}
  });
  loadSession().catch(function () {});
})();
