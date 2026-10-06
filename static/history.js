(function () {
  'use strict';
  const scope = document.body.dataset.scope === 'trash' ? 'trash' : 'active';
  const rows = document.getElementById('history-rows');
  const empty = document.getElementById('history-empty');
  const errorBox = document.getElementById('history-error');
  const count = document.getElementById('record-count');
  const statCount = document.getElementById('history-stat-count');
  const statAmount = document.getElementById('history-stat-amount');
  const statsGrid = document.querySelector('.history-stats');
  const statsOverlay = statsGrid.querySelector('.stats-pro-overlay');
  const filters = document.getElementById('history-filters');
  const yearFilter = document.getElementById('history-year');
  const monthFilter = document.getElementById('history-month');
  const travelTrigger = document.getElementById('travel-history-trigger');
  const expenseTrigger = document.getElementById('expense-history-trigger');
  const recordPanel = document.getElementById('history-record-panel');
  const recordsHeading = document.getElementById('records-heading');
  const retentionHint = document.getElementById('history-retention-hint');
  const pagination = document.getElementById('history-pagination');
  const previousPage = document.getElementById('history-previous-page');
  const nextPage = document.getElementById('history-next-page');
  const pageStatus = document.getElementById('history-page-status');
  const purgeDialog = document.getElementById('purge-dialog');
  const purgeName = document.getElementById('purge-name');
  const editDialog = document.getElementById('edit-dialog');
  const editFrame = document.getElementById('edit-frame');
  const PAGE_SIZE = 15;
  let records = [];
  let currentPage = 0;
  let currentType = 'travel';
  let pendingPurge = null;
  let licenseExpiryTimer = null;
  let canViewStats = false;
  let historyRetentionMonths;
  let listRequestId = 0;
  function setStatValue(element, value, rollDigits) {
    const wrapper = element.parentElement;
    const previousReel = wrapper.querySelector('.history-stat-reel');
    if (previousReel) previousReel.remove();
    element.classList.remove('history-stat-reel-active');
    element.textContent = value;
    if (!rollDigits || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)) return;

    const reel = document.createElement('span');
    reel.className = 'stat-value history-stat-reel';
    reel.setAttribute('aria-hidden', 'true');
    for (const glyph of value) {
      if (/^[0-9]$/.test(glyph)) {
        const digit = document.createElement('span');
        digit.className = 'history-reel-digit';
        const track = document.createElement('span');
        track.className = 'history-reel-track';
        const finalStep = 20 + Number(glyph);
        track.style.setProperty('--history-reel-end', '-' + finalStep + 'em');
        for (let step = 0; step <= finalStep; step += 1) {
          const face = document.createElement('span');
          face.textContent = String(step % 10);
          track.append(face);
        }
        digit.append(track);
        reel.append(digit);
      } else {
        const fixed = document.createElement('span');
        fixed.textContent = glyph;
        reel.append(fixed);
      }
    }
    wrapper.append(reel);
    element.classList.add('history-stat-reel-active');
  }
  function lockStats() {
    canViewStats = false;
    statsGrid.dataset.statsLocked = 'true';
    statsOverlay.hidden = false;
    setStatValue(statCount, '•••', false);
    setStatValue(statAmount, '•••', false);
  }
  if (scope === 'trash') {
    recordPanel.removeAttribute('role');
    recordPanel.setAttribute('aria-labelledby', 'records-heading');
    recordsHeading.textContent = '报销文件';
    const description = document.querySelector('.page-header p');
    if (description) description.textContent = '恢复你删除的报销文件。';
    const heading = document.querySelector('[data-heading]');
    if (heading) {
      const label = heading.querySelector('[data-heading-label]');
      if (label) label.textContent = '回收站';
      else heading.textContent = '回收站';
      const icon = heading.querySelector('.ui-icon use');
      if (icon) icon.setAttribute('href', '/static/icons.svg#trash-2');
    }
  }
  document.querySelectorAll('[data-nav-active]').forEach(node => {
    if (node.dataset.navActive === scope) {
      node.setAttribute('aria-current', 'page');
    } else {
      node.removeAttribute('aria-current');
    }
  });
  const formatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short' });
  const amountFormatter = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const yearMonthFormatter = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric' });
  const formatDate = value => { if (!value) return ''; try { return formatter.format(new Date(value)); } catch (_) { return String(value); } };
  function generatedYearMonth(value) {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const parts = yearMonthFormatter.formatToParts(date);
    return {
      year: parts.find(part => part.type === 'year').value,
      month: parts.find(part => part.type === 'month').value,
    };
  }
  function updateYearOptions() {
    if (scope !== 'active') return;
    const selected = yearFilter.value;
    const years = [...new Set(typeRecords().map(record => generatedYearMonth(record.created_at)?.year).filter(Boolean))].sort((a, b) => Number(b) - Number(a));
    yearFilter.textContent = '';
    const allYears = document.createElement('option');
    allYears.value = '';
    allYears.textContent = '全部年份';
    yearFilter.append(allYears);
    years.forEach(year => {
      const option = document.createElement('option');
      option.value = year;
      option.textContent = year + ' 年';
      yearFilter.append(option);
    });
    yearFilter.value = years.includes(selected) ? selected : '';
  }
  function typeRecords() {
    if (scope !== 'active') return records;
    return records.filter(record => (record.form_type === 'expense' ? 'expense' : 'travel') === currentType);
  }
  function visibleRecords() {
    const selected = typeRecords();
    if (scope !== 'active' || (!yearFilter.value && !monthFilter.value)) return selected;
    return selected.filter(record => {
      const generated = generatedYearMonth(record.created_at);
      return generated && (!yearFilter.value || generated.year === yearFilter.value) && (!monthFilter.value || generated.month === monthFilter.value);
    });
  }
  const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = value || ''; if (className) node.className = className; return node; };
  function showError(message) { errorBox.textContent = message || ''; }
  function notifyHistoryUpdated() { try { localStorage.setItem('reimbursement-history-updated', String(Date.now())); } catch (_) {} }
  function actionButton(label, action, className) { const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.dataset.action = action; button.className = 'table-action-button' + (className ? ' ' + className : ''); return button; }
  function render(nextRecords, animateRefresh = false) {
    records = Array.isArray(nextRecords) ? nextRecords : [];
    const selected = typeRecords();
    const visible = visibleRecords();
    const totalPages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
    currentPage = Math.min(currentPage, totalPages - 1);
    const pageRecords = visible.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
    rows.textContent = '';
    if (scope === 'active' && canViewStats) {
      const totalCents = visible.reduce((sum, record) => sum + Math.round(Number(record.reimbursement_amount || 0) * 100), 0);
      setStatValue(statCount, visible.length + ' 笔', animateRefresh);
      setStatValue(statAmount, '¥' + amountFormatter.format(totalCents / 100), animateRefresh);
    }
    count.textContent = scope === 'active' && (yearFilter.value || monthFilter.value) ? visible.length + ' / ' + selected.length + ' 条' : selected.length + ' 条';
    empty.textContent = selected.length && !visible.length ? '该时间范围内暂无记录' : scope === 'active' ? '暂无' + (currentType === 'expense' ? '费用' : '差旅') + '报销记录' : '暂无记录';
    empty.hidden = visible.length !== 0;
    pagination.hidden = visible.length === 0;
    previousPage.disabled = currentPage === 0;
    nextPage.disabled = currentPage >= totalPages - 1;
    pageStatus.textContent = visible.length > PAGE_SIZE ? (currentPage + 1) + ' / ' + totalPages : '';
    pageRecords.forEach((record, index) => {
      const tr = document.createElement('tr');
      tr.dataset.recordId = record.id;
      tr.dataset.displayName = record.display_name || '';
      const sequenceCell = text('th', currentPage * PAGE_SIZE + index + 1, 'history-col-index');
      sequenceCell.scope = 'row';
      tr.append(
        sequenceCell,
        text('td', record.reason || ''),
        text('td', record.reimbursement_date || '未填写'),
        text('td', record.reimbursement_amount || ''),
        text('td', formatDate(record.created_at)),
        text('td', scope === 'trash' ? ('预计 ' + formatDate(record.purge_at) + ' 清理') : '正常')
      );
      const actions = document.createElement('td'); actions.className = 'table-actions';
      const actionItems = document.createElement('div'); actionItems.className = 'table-action-items';
      if (scope === 'active') {
        const pdf = actionButton('打开 PDF', 'pdf', 'secondary'); pdf.dataset.pdfUrl = record.pdf_url; actionItems.append(pdf, actionButton('再次编辑', 'edit', 'secondary'), actionButton('移入回收站', 'trash', 'danger'));
      } else {
        actionItems.append(actionButton('恢复', 'restore', 'secondary'), actionButton('永久删除', 'purge', 'danger'));
      }
      actions.append(actionItems);
      tr.append(actions); rows.append(tr);
    });
  }
  previousPage.addEventListener('click', function () {
    if (currentPage === 0) return;
    currentPage -= 1;
    render(records);
  });
  nextPage.addEventListener('click', function () {
    if (currentPage >= Math.ceil(visibleRecords().length / PAGE_SIZE) - 1) return;
    currentPage += 1;
    render(records);
  });
  if (scope === 'active') {
    filters.hidden = false;
    function selectType(type) {
      if (currentType === type) return;
      currentType = type;
      currentPage = 0;
      yearFilter.value = '';
      monthFilter.value = '';
      travelTrigger.setAttribute('aria-selected', String(type === 'travel'));
      expenseTrigger.setAttribute('aria-selected', String(type === 'expense'));
      travelTrigger.tabIndex = type === 'travel' ? 0 : -1;
      expenseTrigger.tabIndex = type === 'expense' ? 0 : -1;
      recordPanel.setAttribute('aria-labelledby', type === 'travel' ? 'travel-history-trigger' : 'expense-history-trigger');
      recordsHeading.textContent = type === 'travel' ? '差旅报销记录' : '费用报销记录';
      updateYearOptions();
      render(records, true);
    }
    travelTrigger.addEventListener('click', () => selectType('travel'));
    expenseTrigger.addEventListener('click', () => selectType('expense'));
    [travelTrigger, expenseTrigger].forEach(trigger => trigger.addEventListener('keydown', event => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' && event.key !== 'Home' && event.key !== 'End') return;
      event.preventDefault();
      const type = event.key === 'Home' ? 'travel' : event.key === 'End' ? 'expense' : currentType === 'travel' ? 'expense' : 'travel';
      selectType(type);
      (type === 'travel' ? travelTrigger : expenseTrigger).focus();
    }));
    [yearFilter, monthFilter].forEach(filter => filter.addEventListener('change', () => { currentPage = 0; render(records, true); }));
  }
  function clearRecords() {
    ++listRequestId;
    records = [];
    updateYearOptions();
    render(records);
  }
  async function load() {
    const requestId = ++listRequestId;
    showError('');
    try {
      const data = await apiFetch('/api/reimbursements?scope=' + encodeURIComponent(scope));
      if (requestId !== listRequestId) return;
      records = data.reimbursements || [];
      updateYearOptions();
      render(records, true);
    } catch (error) {
      if (requestId === listRequestId) showError(error.message || '加载记录失败');
    }
  }
  async function loadPermissions() {
    const access = await apiFetch('/api/permissions');
    if (historyRetentionMonths === null && access.history_retention_months != null) clearRecords();
    historyRetentionMonths = access.history_retention_months;
    retentionHint.hidden = scope !== 'active' || access.history_retention_months == null;
    canViewStats = access.reimbursement_stats === true;
    statsGrid.dataset.statsLocked = String(!canViewStats);
    statsOverlay.hidden = canViewStats;
    if (!canViewStats) lockStats();
    if (scope === 'active') render(records);
    if (licenseExpiryTimer) clearTimeout(licenseExpiryTimer);
    if (access.history_retention_months == null && access.license_expires_at) {
      const remaining = Date.parse(access.license_expires_at) - Date.parse(access.server_now);
      if (Number.isFinite(remaining)) {
        licenseExpiryTimer = setTimeout(function () {
          lockStats();
          clearRecords();
          loadPermissions().then(load).catch(function () {});
        }, Math.min(Math.max(remaining + 100, 1000), 2147483647));
      }
    }
  }
  const restorePath = '/restore'; const purgePath = '/purge';
  async function mutate(id, action, payload) { const suffix = action === 'restore' ? restorePath : action === 'purge' ? purgePath : '/trash'; const data = await apiFetch('/api/reimbursements/' + id + suffix, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) }); return data; }
  rows.addEventListener('click', async event => { const button = event.target.closest('button[data-action]'); if (!button) return; const tr = button.closest('tr'); const id = tr.dataset.recordId;
    if (button.dataset.action === 'pdf') { const pdfUrl = button.dataset.pdfUrl; window.open(pdfUrl, '_blank', 'noopener'); return; }
    if (button.dataset.action === 'edit') { editFrame.src = '/?edit=' + encodeURIComponent(id); editDialog.showModal(); return; }
    if (button.dataset.action === 'purge') { pendingPurge = { id: id, name: tr.dataset.displayName || '' }; purgeName.textContent = pendingPurge.name; purgeDialog.showModal(); return; }
    button.disabled = true; try { await mutate(id, button.dataset.action); notifyHistoryUpdated(); await load(); } catch (error) { showError(error.message || '操作失败'); button.disabled = false; }
  });
  document.getElementById('edit-close').addEventListener('click', () => editDialog.close());
  editDialog.addEventListener('close', () => { editFrame.removeAttribute('src'); });
  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== editFrame.contentWindow) return;
    if (event.data && event.data.type === 'reimbursement-regenerated') {
      editDialog.close(); notifyHistoryUpdated(); load();
    }
  });
  document.querySelector('[data-purge-form]').addEventListener('submit', async event => { if (event.submitter.value !== 'confirm' || !pendingPurge) return; event.preventDefault(); const item = pendingPurge; pendingPurge = null; purgeDialog.close(); try { await mutate(item.id, 'purge', { confirm: true }); notifyHistoryUpdated(); await load(); } catch (error) { showError(error.message || '永久删除失败'); } });
  const toggle = document.getElementById('sidebar-toggle'); const sidebar = document.getElementById('app-sidebar'); toggle.addEventListener('click', () => { const open = sidebar.classList.toggle('open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.getElementById('logout').addEventListener('click', async () => { try { await apiFetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); } finally { window.location.assign('/login'); } });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      lockStats();
      loadPermissions().catch(() => {}).then(load);
    }
  });
  loadPermissions().catch(() => {}).then(load);
}());
