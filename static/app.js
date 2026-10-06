(function () {
  'use strict';
  const form = document.getElementById('reimbursement-form');
  const result = document.getElementById('result');
  const errorBox = document.getElementById('error');
  const submit = document.getElementById('submit');
  const travelFields = document.getElementById('travel-fields');
  const expenseFields = document.getElementById('expense-fields');
  const expenseRows = Array.from(document.querySelectorAll('#expense-rows .expense-row'));
  const expenseTotal = document.getElementById('expense-total');
  const expenseTotalUpper = document.getElementById('expense-total-upper');
  const invoiceInput = document.getElementById('invoice-input');
  const invoiceSelect = document.getElementById('invoice-select');
  const invoiceDropzone = document.getElementById('invoice-dropzone');
  const previewList = document.getElementById('preview-list');
  const invoiceUsage = document.getElementById('invoice-usage');
  const invoiceUpgrade = document.getElementById('invoice-upgrade');
  const travelTrigger = document.getElementById('travel-reimbursement-trigger');
  const expenseTrigger = document.getElementById('expense-reimbursement-trigger');
  const monthCount = document.getElementById('month-count');
  const yearCount = document.getElementById('year-count');
  const yearAmount = document.getElementById('year-amount');
  const editId = new URLSearchParams(window.location.search).get('edit');
  const confirmDialog = document.getElementById('regenerate-confirm-dialog');
  let files = [];
  let keptInvoices = [];
  let invoiceLimit = null;
  let invoiceAddQueue = Promise.resolve();
  let invoiceSelectionVersion = 0;
  let sessionProfile = null;
  let form_type = 'travel';
  let statsRequestId = 0;
  let licenseExpiryTimer = null;
  const EXPECTED_ROW_COUNT = 11;
  const numericFields = ['public_amount', 'mileage', 'toll', 'lodging', 'receipts'];
  const detailDateFields = Array.from(document.querySelectorAll('#detail-rows input[data-field="date"]'));

  function syncDetailDateVisibility(field) {
    field.classList.toggle('has-value', field.value.trim() !== '');
  }

  detailDateFields.forEach(function (field) {
    field.setAttribute('autocomplete', 'off');
    syncDetailDateVisibility(field);
    field.addEventListener('input', function () { syncDetailDateVisibility(field); });
    field.addEventListener('change', function () { syncDetailDateVisibility(field); });
  });

  function restoreProfileFields() {
    if (!sessionProfile) return;
    document.getElementById('traveler').value = sessionProfile.traveler;
    document.getElementById('department').value = sessionProfile.department;
    document.getElementById('expense-traveler').value = sessionProfile.traveler;
    document.getElementById('expense-department').value = sessionProfile.department;
  }

  async function loadSession() {
    const data = await apiFetch('/api/session');
    const profile = {
      department: data.user.department,
      traveler: data.user.real_name
    };
    sessionProfile = profile;
    restoreProfileFields();
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
    const selectedType = form_type;
    const requestId = ++statsRequestId;
    try {
      const data = await apiFetch('/api/reimbursements/stats?form_type=' + selectedType);
      if (selectedType !== form_type || requestId !== statsRequestId) return;
      renderStats(data);
    } catch (_) {
      if (selectedType !== form_type || requestId !== statsRequestId) return;
      monthCount.textContent = '—'; yearCount.textContent = '—'; yearAmount.textContent = '—';
    }
  }

  function normalizeDateInput(value) {
    return String(value || '').trim().replace(/\//g, '-');
  }

  function message(text, kind) {
    errorBox.className = 'result ' + (kind || 'error');
    errorBox.textContent = text;
    invoiceUpgrade.hidden = true;
  }

  function showInvoiceLimit() {
    message('普通用户每张报销单最多可导入 6 张发票，升级 Pro 后可解除限制。');
    invoiceUpgrade.hidden = false;
  }

  async function refreshPermissions() {
    const access = await apiFetch('/api/permissions');
    invoiceLimit = access.invoice_limit;
    if (licenseExpiryTimer) clearTimeout(licenseExpiryTimer);
    if (access.license_expires_at) {
      const remaining = Date.parse(access.license_expires_at) - Date.parse(access.server_now);
      if (Number.isFinite(remaining) && remaining > 0) {
        licenseExpiryTimer = setTimeout(function () {
          loadStats();
          refreshPermissions().catch(function () {});
        }, Math.min(Math.max(remaining + 100, 1000), 2147483647));
      }
    }
    renderPreviews();
  }

  function selectedInvoiceCount() {
    return keptInvoices.reduce(function (sum, invoice) { return sum + invoice.count; }, 0) +
      files.reduce(function (sum, entry) { return sum + entry.count; }, 0);
  }

  function collectPayload() {
    const payload = {};
    const profile = sessionProfile;
    if (!profile) throw new Error('账户信息尚未加载，请稍后重试');
    if (form_type === 'expense') {
      if (expenseRows.length !== EXPECTED_ROW_COUNT) throw new Error('费用明细必须为 11 行');
      return {
        form_type: 'expense',
        date: normalizeDateInput(document.getElementById('expense-date').value),
        department: profile.department,
        traveler: profile.traveler,
        rows: expenseRows.map(function (row) {
          const data = {};
          row.querySelectorAll('[data-field]').forEach(function (field) {
            if (field.value.trim() !== '') data[field.dataset.field] = field.value.trim();
          });
          return data;
        })
      };
    }
    ['date', 'reason', 'days', 'allowance'].forEach(function (id) {
      const field = document.getElementById(id);
      payload[id] = id === 'date' ? normalizeDateInput(field.value) : field.value.trim();
    });
    payload.department = profile.department;
    payload.traveler = profile.traveler;
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
    if (payload.form_type === 'expense') {
      if (!payload.date) throw new Error('请填写报销日期');
      if (!payload.rows.some(function (row) { return Object.keys(row).length > 0; })) {
        throw new Error('请至少填写一行费用明细');
      }
      payload.rows.forEach(function (row, index) {
        if (!Object.keys(row).length) return;
        if (!row.project || !row.summary || row.amount === undefined) {
          throw new Error('第 ' + (index + 1) + ' 行需填写报销项目、摘要和金额');
        }
        if (!/^\d+(?:\.\d{1,2})?$/.test(row.amount)) {
          throw new Error('第 ' + (index + 1) + ' 行金额须为非负数，最多两位小数');
        }
      });
      return;
    }
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

  function invoiceFileIcon(name) {
    const extension = name.split('.').pop().toLowerCase();
    const kind = extension === 'jpeg' ? 'jpg' : extension;
    if (kind === 'pdf' || kind === 'jpg' || kind === 'png') {
      const icon = document.createElement('img');
      icon.className = 'invoice-file-icon';
      icon.src = '/static/invoice-' + kind + '.svg';
      icon.alt = '';
      icon.setAttribute('aria-hidden', 'true');
      return icon;
    }
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.classList.add('invoice-file-icon', 'ui-icon');
    icon.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '/static/icons.svg#file-text');
    icon.appendChild(use);
    return icon;
  }

  function renderPreviews() {
    previewList.textContent = '';
    keptInvoices.forEach(function (invoice, index) {
      const item = document.createElement('li'); item.className = 'preview-item'; item.tabIndex = 0;
      const number = document.createElement('span'); number.className = 'invoice-preview-number'; number.textContent = (index + 1) + '.';
      const label = document.createElement('span'); label.className = 'invoice-preview-name'; label.textContent = invoice.name + '（' + invoice.count + ' 张）'; label.title = invoice.name;
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'remove-file secondary'; remove.textContent = '×';
      remove.setAttribute('aria-label', '移除 ' + invoice.name);
      remove.addEventListener('click', function () { keptInvoices.splice(index, 1); renderPreviews(); });
      item.append(number, invoiceFileIcon(invoice.name), label, remove); previewList.appendChild(item);
    });
    files.forEach(function (entry, index) {
      const item = document.createElement('li');
      item.className = 'preview-item'; item.tabIndex = 0;
      const number = document.createElement('span'); number.className = 'invoice-preview-number'; number.textContent = (keptInvoices.length + index + 1) + '.';
      const icon = invoiceFileIcon(entry.file.name);
      const label = document.createElement('span');
      label.className = 'invoice-preview-name'; label.textContent = entry.file.name + '（' + entry.count + ' 张）'; label.title = entry.file.name;
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'remove-file secondary'; remove.textContent = '×';
      remove.setAttribute('aria-label', '移除 ' + entry.file.name);
      remove.addEventListener('click', function () { files.splice(index, 1); renderPreviews(); });
      item.append(number, icon, label, remove); previewList.appendChild(item);
    });
    invoiceUsage.textContent = invoiceLimit == null ? '已选择 ' + selectedInvoiceCount() + ' 张发票' :
      '已选择 ' + selectedInvoiceCount() + ' / ' + invoiceLimit + ' 张发票';
    if (!invoiceUpgrade.hidden && (invoiceLimit == null || selectedInvoiceCount() <= invoiceLimit)) message('');
  }

  async function addInvoices(selected, version) {
    const accepted = selected.filter(function (file) { return /\.(pdf|jpe?g|png)$/i.test(file.name); });
    if (accepted.length !== selected.length) message('仅支持 PDF、JPG 或 PNG 发票文件');
    if (!accepted.length) return;
    await refreshPermissions();
    const body = new FormData();
    accepted.forEach(function (file) { body.append('invoices', file, file.name); });
    const inspected = await apiFetch('/api/invoices/inspect', { method: 'POST', body: body });
    if (version !== invoiceSelectionVersion) return;
    if (!Array.isArray(inspected.counts) || inspected.counts.length !== accepted.length ||
        inspected.counts.some(function (count) { return !Number.isInteger(count) || count < 1; })) {
      throw new Error('发票数量识别失败，请重试');
    }
    const newCount = inspected.counts.reduce(function (sum, count) { return sum + count; }, 0);
    if (invoiceLimit != null && selectedInvoiceCount() + newCount > invoiceLimit) {
      showInvoiceLimit();
      return;
    }
    accepted.forEach(function (file, index) { files.push({ file: file, count: inspected.counts[index] }); });
    message('');
    renderPreviews();
  }

  function enqueueInvoices(selected) {
    const version = invoiceSelectionVersion;
    invoiceAddQueue = invoiceAddQueue.then(function () { return addInvoices(selected, version); }).catch(function (error) {
      message(error.message || '发票读取失败');
    });
  }

  invoiceSelect.addEventListener('click', function (event) {
    event.stopPropagation(); invoiceInput.click();
  });
  invoiceDropzone.addEventListener('click', function () { invoiceInput.click(); });
  invoiceDropzone.addEventListener('keydown', function (event) {
    if (event.target === invoiceDropzone && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault(); invoiceInput.click();
    }
  });
  invoiceInput.addEventListener('change', function () {
    enqueueInvoices(Array.from(invoiceInput.files));
    invoiceInput.value = '';
  });
  invoiceDropzone.addEventListener('dragover', function (event) {
    event.preventDefault(); invoiceDropzone.classList.add('drag-over');
  });
  invoiceDropzone.addEventListener('dragleave', function (event) {
    if (!invoiceDropzone.contains(event.relatedTarget)) invoiceDropzone.classList.remove('drag-over');
  });
  invoiceDropzone.addEventListener('drop', function (event) {
    event.preventDefault(); invoiceDropzone.classList.remove('drag-over');
    enqueueInvoices(Array.from(event.dataTransfer.files));
  });
  function selectType(type) {
    const changed = form_type !== type;
    form_type = type;
    const expense = type === 'expense';
    travelFields.hidden = expense;
    expenseFields.hidden = !expense;
    travelTrigger.setAttribute('aria-selected', String(!expense));
    expenseTrigger.setAttribute('aria-selected', String(expense));
    if (changed) {
      monthCount.textContent = '0 笔'; yearCount.textContent = '0 笔'; yearAmount.textContent = '¥0.00';
      loadStats();
    }
  }
  travelTrigger.addEventListener('click', function () { selectType('travel'); });
  expenseTrigger.addEventListener('click', function () { selectType('expense'); });

  function centsFromInput(value) {
    if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return null;
    const pieces = value.split('.');
    return BigInt(pieces[0]) * 100n + BigInt((pieces[1] || '').padEnd(2, '0'));
  }
  function upperInteger(value) {
    const digits = '零壹贰叁肆伍陆柒捌玖';
    const smallUnits = ['', '拾', '佰', '仟'];
    const bigUnits = ['', '万', '亿', '兆', '京'];
    if (value === 0n) return '零';
    const groups = [];
    while (value > 0n) { groups.push(Number(value % 10000n)); value /= 10000n; }
    let result = '', zeroPending = false;
    for (let index = groups.length - 1; index >= 0; index--) {
      const group = groups[index];
      if (!group) { if (result) zeroPending = true; continue; }
      if (result && (zeroPending || group < 1000)) result += '零';
      let part = '', insideZero = false;
      for (let position = 3; position >= 0; position--) {
        const digit = Math.floor(group / (10 ** position)) % 10;
        if (digit) {
          if (insideZero && part) part += '零';
          part += digits[digit] + smallUnits[position];
          insideZero = false;
        } else if (part) insideZero = true;
      }
      result += part + (bigUnits[index] || '');
      zeroPending = false;
    }
    return result;
  }
  function upperAmount(cents) {
    const digits = '零壹贰叁肆伍陆柒捌玖';
    const integer = cents / 100n;
    const jiao = Number(cents % 100n / 10n);
    const fen = Number(cents % 10n);
    let result = upperInteger(integer) + '元';
    if (!jiao && !fen) return result + '整';
    if (!jiao) return result + (integer ? '零' : '') + digits[fen] + '分';
    result += digits[jiao] + '角';
    return result + (fen ? digits[fen] + '分' : '');
  }
  function updateExpenseTotal() {
    let total = 0n;
    for (const row of expenseRows) {
      const input = row.querySelector('[data-field="amount"]');
      const value = input.value.trim();
      if (!value) continue;
      const cents = centsFromInput(value);
      if (cents === null) {
        expenseTotal.textContent = '请检查金额';
        expenseTotalUpper.textContent = '请检查金额';
        return;
      }
      total += cents;
    }
    const whole = (total / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    expenseTotal.textContent = '¥' + whole + '.' + (total % 100n).toString().padStart(2, '0');
    expenseTotalUpper.textContent = upperAmount(total);
  }
  expenseRows.forEach(function (row) {
    row.querySelector('[data-field="amount"]').addEventListener('input', updateExpenseTotal);
  });

  form.addEventListener('reset', function () {
    invoiceSelectionVersion += 1;
    detailDateFields.forEach(function (field) { field.value = ''; syncDetailDateVisibility(field); });
    files = []; renderPreviews(); invoiceInput.value = ''; result.className = 'result'; result.textContent = ''; message('');
    setTimeout(function () { restoreProfileFields(); updateExpenseTotal(); }, 0);
  });
  async function loadEditRecord() {
    if (!editId) return;
    const data = await apiFetch('/api/reimbursements/' + encodeURIComponent(editId) + '/edit');
    const payload = data.payload;
    selectType(payload.form_type === 'expense' ? 'expense' : 'travel');
    if (form_type === 'expense') {
      document.getElementById('expense-date').value = payload.date || '';
      expenseRows.forEach(function (row, index) {
        const values = payload.rows[index] || {};
        row.querySelectorAll('[data-field]').forEach(function (field) {
          field.value = values[field.dataset.field] == null ? '' : String(values[field.dataset.field]);
        });
      });
      updateExpenseTotal();
    } else {
      ['date', 'reason', 'days', 'allowance'].forEach(function (id) {
        document.getElementById(id).value = payload[id] == null ? '' : String(payload[id]);
      });
      document.querySelectorAll('#detail-rows .detail-row').forEach(function (row, index) {
        const values = payload.rows[index] || {};
        row.querySelectorAll('[data-field]').forEach(function (field) {
          field.value = values[field.dataset.field] == null ? '' : String(values[field.dataset.field]);
          field.dispatchEvent(new Event('change', { bubbles: true }));
        });
      });
    }
    const layout = form.querySelector('input[name="invoice-layout"][value="' + data.invoice_layout + '"]');
    if (layout) layout.checked = true;
    keptInvoices = (data.invoices || []).map(function (name, index) {
      return { name: name, index: index, count: (data.invoice_counts || [])[index] || 1 };
    });
    renderPreviews();
    if (data.legacy_invoices_unavailable) {
      const note = document.createElement('p'); note.className = 'invoice-legacy-note';
      note.textContent = '这条旧记录没有保存原始发票文件；重新生成时请重新上传需要保留的发票。';
      document.getElementById('preview-list').before(note);
    }
    submit.textContent = '重新生成PDF';
  }
  async function confirmRegeneration() {
    confirmDialog.returnValue = '';
    return new Promise(function (resolve) {
      confirmDialog.addEventListener('close', function () { resolve(confirmDialog.returnValue === 'confirm'); }, { once: true });
      confirmDialog.showModal();
    });
  }
  form.addEventListener('submit', async function (event) {
    event.preventDefault(); result.textContent = ''; result.className = 'result'; errorBox.textContent = ''; errorBox.className = 'result error';
    let payload;
    try { payload = collectPayload(); validate(payload); } catch (error) { message(error.message); return; }
    await invoiceAddQueue;
    try { await refreshPermissions(); } catch (error) { message(error.message || '权限状态读取失败'); return; }
    if (invoiceLimit != null && selectedInvoiceCount() > invoiceLimit) { showInvoiceLimit(); return; }
    if (editId) {
      if (!await confirmRegeneration()) return;
      payload.keep_invoices = keptInvoices.map(function (invoice) { return invoice.index; });
    }
    const body = new FormData();
    body.append('payload', JSON.stringify(payload));
    body.append('invoice_layout', form.querySelector('input[name="invoice-layout"]:checked').value);
    files.forEach(function (entry) { body.append('invoices', entry.file, entry.file.name); });
    submit.disabled = true; submit.dataset.loading = 'true'; submit.textContent = '生成中…';
    try {
      const endpoint = editId ? '/api/reimbursements/' + encodeURIComponent(editId) + '/regenerate' : '/api/reimbursements/generate';
      const data = await apiFetch(endpoint, { method: 'POST', body: body });
      if (editId && window.parent !== window) {
        window.parent.postMessage({ type: 'reimbursement-regenerated', id: editId }, window.location.origin);
        return;
      }
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
      const link = document.createElement('a');
      link.className = 'table-action-button';
      link.href = data.pdf_url;
      link.textContent = '打开 PDF';
      link.target = '_blank';
      link.rel = 'noopener';
      result.appendChild(link);
      await loadStats();
    } catch (error) {
      if (error.upgradeUrl) showInvoiceLimit();
      else message(error.message || '生成失败');
    }
    finally { submit.disabled = false; delete submit.dataset.loading; submit.textContent = editId ? '重新生成PDF' : '生成 PDF'; }
  });
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('app-sidebar');
  toggle.addEventListener('click', function () { const open = sidebar.classList.toggle('open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.getElementById('logout').addEventListener('click', async function () {
    try { await apiFetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); window.location.assign('/login'); } catch (_) {}
  });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      setStatsLocked(true);
      loadStats();
      refreshPermissions().catch(function () {});
    }
  });
  window.addEventListener('storage', function (event) { if (event.key === 'reimbursement-history-updated') loadStats(); });
  loadSession().then(loadEditRecord).catch(function (error) {
    message(error.message || '账户信息加载失败，请刷新页面重试');
    if (error.upgradeUrl) invoiceUpgrade.hidden = false;
  });
  refreshPermissions().catch(function () {});
  loadStats();
})();
