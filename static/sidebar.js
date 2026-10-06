(function () {
  'use strict';

  const root = document.documentElement;
  root.classList.add('sidebar-collapsed');

  document.addEventListener('DOMContentLoaded', function () {
    const button = document.getElementById('sidebar-collapse');
    if (!button) return;
    const icon = button.querySelector('use');

    function updateControl() {
      const collapsed = root.classList.contains('sidebar-collapsed');
      const label = collapsed ? '展开侧边栏' : '收起侧边栏';
      button.setAttribute('aria-expanded', String(!collapsed));
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
      if (icon) icon.setAttribute('href', '/static/icons.svg?v=20261001-double-chevron#chevrons-' + (collapsed ? 'right' : 'left'));
    }

    button.addEventListener('click', function () {
      root.classList.toggle('sidebar-collapsed');
      updateControl();
    });
    updateControl();

    const versionTrigger = document.getElementById('version-details-trigger');
    if (!versionTrigger) return;

    const versionDialog = document.createElement('dialog');
    versionDialog.id = 'version-details-dialog';
    versionDialog.className = 'version-details-dialog';
    versionDialog.setAttribute('aria-labelledby', 'version-details-title');
    versionDialog.innerHTML = `
      <div class="version-details-content">
        <header class="version-details-header">
          <div><h2 id="version-details-title">v1.1.0</h2><p>发布日期：2026-10-01</p></div>
          <button type="button" class="version-details-close" data-version-close aria-label="关闭版本更新详情">×</button>
        </header>
        <p class="version-details-intro">本次更新进一步完善报销业务流程与个人数据管理能力，新增费用报销、发票上传、历史记录编辑及多项数据统计功能，同时优化系统界面与使用体验。</p>
        <section aria-labelledby="version-features-title">
          <h3 id="version-features-title">✨ 新增功能</h3>
          <ul>
            <li><strong>发票上传</strong><p>支持在报销过程中上传并管理相关发票资料。</p></li>
            <li><strong>费用报销</strong><p>新增费用报销功能，进一步完善系统报销业务场景。</p></li>
            <li><strong>历史记录再次编辑</strong><p>支持对已有报销记录重新编辑和修改，方便信息调整与补充。</p></li>
            <li><strong>个人中心</strong><p>新增个人中心，集中展示用户个人信息及相关数据。</p></li>
            <li><strong>用户资料修改</strong><p>支持用户修改和维护个人基础信息。</p></li>
            <li><strong>报销统计看板</strong><p>新增报销数据统计与可视化展示，更直观地查看报销情况。</p></li>
            <li><strong>出差天数看板</strong><p>新增出差天数统计与趋势展示，方便查看个人出差情况。</p></li>
          </ul>
        </section>
        <section aria-labelledby="version-experience-title">
          <h3 id="version-experience-title">🎨 体验优化</h3>
          <ul><li><strong>侧栏 UI 优化</strong><p>优化系统侧栏布局、导航及视觉效果，提升整体使用体验。</p></li></ul>
        </section>
        <section aria-labelledby="version-fixes-title">
          <h3 id="version-fixes-title">🛠 问题修复</h3>
          <ul>
            <li>修复登录失败时超时提示异常的问题。</li>
            <li>修复用户注册条件提示不准确的问题。</li>
            <li>修复其他已知问题，提升系统稳定性与使用体验。</li>
          </ul>
        </section>
      </div>`;
    document.body.appendChild(versionDialog);
    window.showVersionDetails = function (expectedVersion) {
      const displayedVersion = document.getElementById('app-version');
      if (expectedVersion && displayedVersion?.textContent.trim() !== expectedVersion) return false;
      if (!versionDialog.open) versionDialog.showModal();
      return true;
    };
    versionTrigger.addEventListener('click', function () { window.showVersionDetails(); });
    versionDialog.querySelector('[data-version-close]').addEventListener('click', function () { versionDialog.close(); });
  });
}());
