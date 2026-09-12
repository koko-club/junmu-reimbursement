(function () {
  'use strict';
  const scope = document.body.dataset.scope === 'trash' ? 'trash' : 'active';
  const rows = document.getElementById('history-rows');
  const empty = document.getElementById('history-empty');
  const errorBox = document.getElementById('history-error');
  const count = document.getElementById('record-count');
  const pagination = document.getElementById('history-pagination');
  const previousPage = document.getElementById('history-previous-page');
  const nextPage = document.getElementById('history-next-page');
  const pageStatus = document.getElementById('history-page-status');
  const purgeDialog = document.getElementById('purge-dialog');
  const purgeName = document.getElementById('purge-name');
  const PAGE_SIZE = 15;
  let records = [];
  let currentPage = 0;
  let pendingPurge = null;
  if (scope === 'trash') {
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
  const formatDate = value => { if (!value) return ''; try { return formatter.format(new Date(value)); } catch (_) { return String(value); } };
  const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = value || ''; if (className) node.className = className; return node; };
  function showError(message) { errorBox.textContent = message || ''; }
  function notifyHistoryUpdated() { try { localStorage.setItem('reimbursement-history-updated', String(Date.now())); } catch (_) {} }
  function actionButton(label, action, className) { const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.dataset.action = action; button.className = 'table-action-button' + (className ? ' ' + className : ''); return button; }
  function render(nextRecords) {
    records = Array.isArray(nextRecords) ? nextRecords : [];
    const totalPages = Math.max(1, Math.ceil(records.length / PAGE_SIZE));
    currentPage = Math.min(currentPage, totalPages - 1);
    const pageRecords = records.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
    rows.textContent = '';
    count.textContent = records.length + ' 条';
    empty.hidden = records.length !== 0;
    pagination.hidden = records.length === 0;
    previousPage.disabled = currentPage === 0;
    nextPage.disabled = currentPage >= totalPages - 1;
    pageStatus.textContent = records.length > PAGE_SIZE ? (currentPage + 1) + ' / ' + totalPages : '';
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
        const xlsx = document.createElement('a'); xlsx.className = 'table-action-button secondary'; xlsx.href = record.xlsx_url; xlsx.textContent = '下载 Excel'; xlsx.download = record.display_name; actionItems.append(xlsx);
        const pdf = actionButton('打开 PDF', 'pdf', 'secondary'); pdf.dataset.pdfUrl = record.pdf_url; actionItems.append(pdf, actionButton('移入回收站', 'trash', 'danger'));
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
    if (currentPage >= Math.ceil(records.length / PAGE_SIZE) - 1) return;
    currentPage += 1;
    render(records);
  });
  async function load() { showError(''); try { const data = await apiFetch('/api/reimbursements?scope=' + encodeURIComponent(scope)); render(data.reimbursements || []); } catch (error) { showError(error.message || '加载记录失败'); } }
  const restorePath = '/restore'; const purgePath = '/purge';
  async function mutate(id, action, payload) { const suffix = action === 'restore' ? restorePath : action === 'purge' ? purgePath : '/trash'; const data = await apiFetch('/api/reimbursements/' + id + suffix, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) }); return data; }
  rows.addEventListener('click', async event => { const button = event.target.closest('button[data-action]'); if (!button) return; const tr = button.closest('tr'); const id = tr.dataset.recordId;
    if (button.dataset.action === 'pdf') { const pdfUrl = button.dataset.pdfUrl; window.open(pdfUrl, '_blank', 'noopener'); return; }
    if (button.dataset.action === 'purge') { pendingPurge = { id: id, name: tr.dataset.displayName || '' }; purgeName.textContent = pendingPurge.name; purgeDialog.showModal(); return; }
    button.disabled = true; try { await mutate(id, button.dataset.action); notifyHistoryUpdated(); await load(); } catch (error) { showError(error.message || '操作失败'); button.disabled = false; }
  });
  document.querySelector('[data-purge-form]').addEventListener('submit', async event => { if (event.submitter.value !== 'confirm' || !pendingPurge) return; event.preventDefault(); const item = pendingPurge; pendingPurge = null; purgeDialog.close(); try { await mutate(item.id, 'purge', { confirm: true }); notifyHistoryUpdated(); await load(); } catch (error) { showError(error.message || '永久删除失败'); } });
  const toggle = document.getElementById('sidebar-toggle'); const sidebar = document.getElementById('app-sidebar'); toggle.addEventListener('click', () => { const open = sidebar.classList.toggle('open'); toggle.setAttribute('aria-expanded', String(open)); });
  document.getElementById('logout').addEventListener('click', async () => { try { await apiFetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); } finally { window.location.assign('/login'); } });
  load();
}());
