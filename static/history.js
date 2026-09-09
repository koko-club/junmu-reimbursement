(function () {
  'use strict';
  const scope = document.body.dataset.scope === 'trash' ? 'trash' : 'active';
  const rows = document.getElementById('history-rows');
  const empty = document.getElementById('history-empty');
  const errorBox = document.getElementById('history-error');
  const count = document.getElementById('record-count');
  const purgeDialog = document.getElementById('purge-dialog');
  const purgeName = document.getElementById('purge-name');
  let pendingPurge = null;
  if (scope === 'trash') {
    document.title = '回收站';
    const heading = document.querySelector('[data-heading]');
    if (heading) {
      const label = heading.querySelector('[data-heading-label]');
      if (label) label.textContent = '回收站';
      else heading.textContent = '回收站';
      const icon = heading.querySelector('.ui-icon use');
      if (icon) icon.setAttribute('href', '/static/icons.svg#trash-2');
    }
    document.querySelectorAll('[data-nav-active]').forEach(node => {
      node.toggleAttribute('aria-current', node.dataset.navActive === 'trash');
    });
  } else {
    const active = document.querySelector('[data-nav-active="active"]');
    if (active) active.setAttribute('aria-current', 'page');
  }
  const formatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short' });
  const formatDate = value => { if (!value) return ''; try { return formatter.format(new Date(value)); } catch (_) { return String(value); } };
  const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = value || ''; if (className) node.className = className; return node; };
  function showError(message) { errorBox.textContent = message || ''; }
  function notifyHistoryUpdated() { try { localStorage.setItem('reimbursement-history-updated', String(Date.now())); } catch (_) {} }
  function actionButton(label, action, className) { const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.dataset.action = action; if (className) button.className = className; return button; }
  function render(records) {
    rows.textContent = ''; count.textContent = records.length + ' 条'; empty.hidden = records.length !== 0;
    records.forEach(record => {
      const tr = document.createElement('tr');
      tr.dataset.recordId = record.id;
      tr.dataset.displayName = record.display_name || '';
      tr.append(
        text('td', record.reason || ''),
        text('td', record.reimbursement_date || '未填写'),
        text('td', record.reimbursement_amount || ''),
        text('td', formatDate(record.created_at)),
        text('td', scope === 'trash' ? ('预计 ' + formatDate(record.purge_at) + ' 清理') : '正常')
      );
      const actions = document.createElement('td'); actions.className = 'table-actions';
      const actionItems = document.createElement('div'); actionItems.className = 'table-action-items';
      if (scope === 'active') {
        const xlsx = document.createElement('a'); xlsx.className = 'table-action-button'; xlsx.href = record.xlsx_url; xlsx.textContent = '下载 Excel'; xlsx.download = record.display_name; actionItems.append(xlsx);
        const pdf = actionButton('打开 PDF', 'pdf'); pdf.dataset.pdfUrl = record.pdf_url; actionItems.append(pdf, actionButton('移入回收站', 'trash', 'danger'));
      } else {
        actionItems.append(actionButton('恢复', 'restore'), actionButton('永久删除', 'purge', 'danger'));
      }
      actions.append(actionItems);
      tr.append(actions); rows.append(tr);
    });
  }
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
