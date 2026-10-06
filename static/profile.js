(function () {
  'use strict';

  const form = document.getElementById('profile-form');
  const username = document.getElementById('profile-username');
  const realName = document.getElementById('profile-real-name');
  const department = document.getElementById('profile-department');
  const status = document.getElementById('profile-status');
  const save = document.getElementById('profile-save');
  const profileMessage = document.getElementById('profile-message');
  const licenseForm = document.getElementById('license-import-form');
  const licenseSerial = document.getElementById('license-serial');
  const licenseExpiry = document.getElementById('license-expiry');
  const licenseMessage = document.getElementById('license-message');
  const licenseCode = document.getElementById('license-code');
  const licenseImport = document.getElementById('license-import');
  const plansDialog = document.getElementById('license-plans-dialog');
  const plansBuy = document.getElementById('license-buy');
  const plansClose = document.getElementById('license-plans-close');
  const plansUpgrade = document.getElementById('license-plans-upgrade');
  const plansGrid = document.getElementById('license-plans-grid');
  const plansFooter = document.getElementById('license-plans-footer');
  const plansPayment = document.getElementById('license-payment');
  const paymentBack = document.getElementById('license-payment-back');
  const paymentSerial = document.getElementById('license-payment-serial');
  const basicPlanBadge = document.getElementById('license-plan-basic-badge');
  const proPlanBadge = document.getElementById('license-plan-pro-badge');
  const passwordDialog = document.getElementById('change-password-dialog');
  const passwordForm = document.getElementById('change-password-form');
  const yearSelect = document.getElementById('travel-year');
  const monthSelect = document.getElementById('travel-month');
  const travelSection = document.getElementById('travel-section');
  const chart = document.getElementById('travel-chart');
  const chartPlaceholder = document.getElementById('travel-trend-placeholder');
  const travelOverlay = document.getElementById('travel-pro-overlay');
  const travelLoading = document.getElementById('travel-loading');
  const monthList = document.getElementById('travel-month-list');
  const monthPlaceholder = document.getElementById('travel-month-placeholder');
  const summaryLabel = document.getElementById('travel-summary-label');
  const summaryValue = document.getElementById('travel-summary-value');
  const empty = document.getElementById('travel-empty');
  const errorBox = document.getElementById('travel-error');
  const retry = document.getElementById('travel-retry');
  const excluded = document.getElementById('travel-excluded');
  const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
  const svgNamespace = 'http://www.w3.org/2000/svg';
  let records = [];
  let canViewTravelTrend = false;
  let trendRequestId = 0;
  let accessRequestId = 0;
  let trendExpiryTimer = null;

  function displayDays(value) {
    return numberFormat.format(Number(value) || 0) + ' 天';
  }

  function svgElement(tag, attributes, content) {
    const node = document.createElementNS(svgNamespace, tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      node.setAttribute(name, String(value));
    }
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function setSvgHidden(element, hidden) {
    if (hidden) element.setAttribute('hidden', '');
    else element.removeAttribute('hidden');
  }

  function renderChart(values, selectedMonth) {
    const width = 760;
    const height = 250;
    const left = 52;
    const right = 22;
    const top = 24;
    const bottom = 43;
    const innerWidth = width - left - right;
    const innerHeight = height - top - bottom;
    const maximum = Math.max(...values, 0);
    const targetStep = maximum / 4 || 1;
    const magnitude = 10 ** Math.floor(Math.log10(targetStep));
    const step = Math.ceil(targetStep / magnitude) * magnitude;
    const ceiling = step * 4;
    const x = index => left + (innerWidth * index / 11);
    const y = value => top + innerHeight * (1 - value / ceiling);
    chart.replaceChildren();
    chart.append(svgElement('title', {}, '每月出差天数趋势'));
    for (let tick = 0; tick <= 4; tick += 1) {
      const amount = step * tick;
      const position = y(amount);
      chart.append(svgElement('line', {
        x1: left, y1: position, x2: width - right, y2: position,
        class: 'travel-grid-line'
      }));
      chart.append(svgElement('text', {
        x: left - 12, y: position + 4, 'text-anchor': 'end', class: 'travel-axis-label'
      }, numberFormat.format(amount)));
    }
    values.forEach((value, index) => {
      chart.append(svgElement('text', {
        x: x(index), y: height - 12, 'text-anchor': 'middle', class: 'travel-axis-label'
      }, String(index + 1) + '月'));
    });
    const points = values.map((value, index) => ({ x: x(index), y: y(value) }));
    const slopes = points.map((point, index) => {
      if (index === 0 || index === points.length - 1) return 0;
      const before = point.y - points[index - 1].y;
      const after = points[index + 1].y - point.y;
      return before * after > 0 ? 2 * before * after / (before + after) : 0;
    });
    const curve = points.slice(1).reduce((path, point, index) => {
      const previous = points[index];
      const controlDistance = (point.x - previous.x) / 3;
      return path + ` C ${previous.x + controlDistance},${previous.y + slopes[index] / 3}`
        + ` ${point.x - controlDistance},${point.y - slopes[index + 1] / 3}`
        + ` ${point.x},${point.y}`;
    }, `M ${points[0].x},${points[0].y}`);
    chart.append(svgElement('path', {
      d: curve, class: 'travel-line', fill: 'none'
    }));
    values.forEach((value, index) => {
      const selected = selectedMonth === index + 1;
      if (selected) chart.append(svgElement('circle', {
        cx: x(index), cy: y(value), r: 11, class: 'travel-point-halo'
      }));
      chart.append(svgElement('circle', {
        cx: x(index), cy: y(value), r: selected ? 6 : 4,
        class: selected ? 'travel-point selected' : 'travel-point'
      }));
    });
    chart.setAttribute('aria-label', yearSelect.value + '年每月出差天数曲线图');
  }

  function renderTravel() {
    if (!canViewTravelTrend) return;
    const year = Number(yearSelect.value);
    const selectedMonth = Number(monthSelect.value) || 0;
    const values = Array.from({ length: 12 }, () => 0);
    records.forEach(item => {
      if (item.year === year && item.month >= 1 && item.month <= 12) {
        values[item.month - 1] = Number(item.days) || 0;
      }
    });
    const yearlyTotal = values.reduce((total, value) => total + value, 0);
    summaryLabel.textContent = selectedMonth ? year + ' 年 ' + selectedMonth + ' 月出差天数' : year + ' 年出差总天数';
    summaryValue.textContent = displayDays(selectedMonth ? values[selectedMonth - 1] : yearlyTotal);
    empty.hidden = records.length !== 0;
    setSvgHidden(chart, records.length === 0);
    monthList.hidden = records.length === 0;
    if (records.length === 0) {
      monthList.replaceChildren();
      chart.replaceChildren();
      return;
    }
    renderChart(values, selectedMonth);
    monthList.replaceChildren();
    values.forEach((value, index) => {
      const item = document.createElement('li');
      if (selectedMonth === index + 1) item.className = 'selected';
      const month = document.createElement('span');
      month.textContent = (index + 1) + ' 月';
      const days = document.createElement('strong');
      days.textContent = displayDays(value);
      item.append(month, days);
      monthList.append(item);
    });
  }

  function startTravelLoading() {
    ++trendRequestId;
    canViewTravelTrend = false;
    records = [];
    travelSection.dataset.trendLocked = 'true';
    travelSection.dataset.trendState = 'loading';
    setSvgHidden(chartPlaceholder, false);
    monthPlaceholder.hidden = false;
    travelOverlay.hidden = true;
    travelLoading.hidden = false;
    setSvgHidden(chart, true);
    chart.replaceChildren();
    monthList.hidden = true;
    monthList.replaceChildren();
    summaryLabel.textContent = '全年出差天数';
    summaryValue.textContent = '•••';
    excluded.textContent = '';
    empty.hidden = true;
    errorBox.hidden = true;
    retry.hidden = true;
    yearSelect.disabled = true;
    monthSelect.disabled = true;
    yearSelect.replaceChildren();
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '年份';
    yearSelect.append(option);
    yearSelect.value = '';
    monthSelect.value = '';
  }

  function lockTravelTrend() {
    startTravelLoading();
    travelSection.dataset.trendState = 'locked';
    travelOverlay.hidden = false;
    travelLoading.hidden = true;
  }

  function unlockTravelTrend() {
    canViewTravelTrend = true;
    travelSection.dataset.trendLocked = 'false';
    travelSection.dataset.trendState = 'loading';
    travelOverlay.hidden = true;
    travelLoading.hidden = false;
  }

  async function loadTravel(preferredYear, preferredMonth) {
    if (!canViewTravelTrend) return;
    const requestId = ++trendRequestId;
    travelSection.dataset.trendState = 'loading';
    setSvgHidden(chartPlaceholder, false);
    monthPlaceholder.hidden = false;
    setSvgHidden(chart, true);
    monthList.hidden = true;
    travelLoading.hidden = false;
    summaryValue.textContent = '•••';
    yearSelect.disabled = true;
    monthSelect.disabled = true;
    errorBox.hidden = true;
    retry.hidden = true;
    try {
      const data = await apiFetch('/api/travel-days');
      if (!canViewTravelTrend || requestId !== trendRequestId) return;
      records = Array.isArray(data.months) ? data.months : [];
      const years = [...new Set(records.map(item => item.year))].sort((a, b) => b - a);
      const selectedYear = Number(preferredYear || yearSelect.value);
      yearSelect.replaceChildren();
      if (years.length === 0) years.push(new Date().getFullYear());
      years.forEach(year => {
        const option = document.createElement('option');
        option.value = String(year);
        option.textContent = year + ' 年';
        yearSelect.append(option);
      });
      yearSelect.value = years.includes(selectedYear) ? String(selectedYear) : String(years[0]);
      if (preferredMonth) monthSelect.value = preferredMonth;
      excluded.textContent = data.excluded_records > 0
        ? data.excluded_records + ' 笔差旅记录没有可用的行程日期，未计入图表。' : '';
      renderTravel();
      setSvgHidden(chartPlaceholder, true);
      monthPlaceholder.hidden = true;
      travelLoading.hidden = true;
      yearSelect.disabled = false;
      monthSelect.disabled = false;
      travelSection.dataset.trendState = records.length ? 'ready' : 'empty';
    } catch (error) {
      if (!canViewTravelTrend || requestId !== trendRequestId) return;
      if (error.upgradeUrl) {
        lockTravelTrend();
        refreshTravelAccess();
        loadLicense();
        return;
      }
      errorBox.textContent = error.message || '出差天数加载失败';
      errorBox.hidden = false;
      retry.hidden = false;
      travelLoading.hidden = true;
      travelSection.dataset.trendState = 'error';
      setSvgHidden(chart, true);
      monthList.hidden = true;
    }
  }

  async function refreshTravelAccess(preferredYear, preferredMonth) {
    const requestId = ++accessRequestId;
    try {
      const access = await apiFetch('/api/permissions');
      if (requestId !== accessRequestId) return;
      if (trendExpiryTimer) clearTimeout(trendExpiryTimer);
      if (!access.travel_trend) {
        lockTravelTrend();
        return;
      }
      unlockTravelTrend();
      if (access.license_expires_at && access.server_now) {
        const remaining = Date.parse(access.license_expires_at) - Date.parse(access.server_now);
        if (Number.isFinite(remaining) && remaining > 0) {
          trendExpiryTimer = setTimeout(function () {
            lockTravelTrend();
            refreshTravelAccess();
            loadLicense();
          }, Math.min(Math.max(remaining + 100, 1000), 2147483647));
        }
      }
      await loadTravel(preferredYear, preferredMonth);
    } catch (_) {
      if (requestId !== accessRequestId) return;
      startTravelLoading();
      travelSection.dataset.trendState = 'error';
      travelLoading.hidden = true;
      errorBox.textContent = '出差数据加载失败，请重试。';
      errorBox.hidden = false;
      retry.hidden = false;
    }
  }

  async function loadProfile() {
    try {
      const data = await apiFetch('/api/session');
      const user = data.user;
      username.value = user.username;
      realName.value = user.real_name;
      department.value = user.department;
    } catch (error) {
      profileMessage.className = 'form-message error';
      profileMessage.textContent = error.message || '账户资料加载失败';
    }
  }

  function renderLicense(state) {
    status.dataset.tier = state.tier === 'pro' ? 'pro' : 'basic';
    status.textContent = state.tier === 'pro' ? 'Pro用户' : '普通用户';
    basicPlanBadge.textContent = state.tier === 'pro' ? '基础版本' : '当前版本';
    proPlanBadge.textContent = state.tier === 'pro' ? '当前版本' : '推荐选择';
    licenseSerial.value = state.serial || '';
    if (state.reason === 'clock_rollback') {
      licenseExpiry.textContent = '服务器时间发生回退，请联系管理员校准时间。';
    } else if (state.reason === 'not_yet_valid') {
      licenseExpiry.textContent = '授权码尚未生效，请检查服务器时间。';
    } else if (state.reason === 'invalid') {
      licenseExpiry.textContent = '已保存的授权码验证失败，请联系授权人。';
    } else if (state.expires_at) {
      const until = new Date(state.expires_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
      licenseExpiry.textContent = state.tier === 'pro' ? '授权有效期至：' + until : '上一授权已于 ' + until + ' 到期';
    } else {
      licenseExpiry.textContent = '尚未导入授权码。';
    }
  }

  async function loadLicense() {
    try {
      renderLicense(await apiFetch('/api/license'));
    } catch (error) {
      status.textContent = '授权状态加载失败';
      licenseMessage.className = 'form-message error';
      licenseMessage.textContent = error.message || '授权状态加载失败';
    }
  }

  licenseForm.addEventListener('submit', async event => {
    event.preventDefault();
    licenseMessage.textContent = '';
    licenseImport.disabled = true;
    try {
      const state = await apiFetch('/api/license/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: licenseCode.value.replace(/\s+/g, '') })
      });
      renderLicense(state);
      licenseCode.value = '';
      licenseMessage.className = 'form-message success';
      licenseMessage.textContent = '授权已验证，当前账号已升级为 Pro。';
      await refreshTravelAccess();
    } catch (error) {
      licenseMessage.className = 'form-message error';
      licenseMessage.textContent = error.message || '授权码验证失败';
    } finally {
      licenseImport.disabled = false;
    }
  });
  document.getElementById('license-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(licenseSerial.value);
      licenseMessage.className = 'form-message success';
      licenseMessage.textContent = '序列号已复制。';
    } catch (_) {
      licenseSerial.select();
      licenseMessage.className = 'form-message';
      licenseMessage.textContent = '请手动复制选中的序列号。';
    }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    profileMessage.textContent = '';
    save.disabled = true;
    try {
      const data = await apiFetch('/api/profile', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ real_name: realName.value.trim(), department: department.value.trim() })
      });
      realName.value = data.user.real_name;
      department.value = data.user.department;
      profileMessage.className = 'form-message success';
      profileMessage.textContent = '资料已保存，新报销单将使用更新后的姓名和部门。';
    } catch (error) {
      profileMessage.className = 'form-message error';
      profileMessage.textContent = error.message || '保存失败，请稍后重试';
    } finally {
      save.disabled = false;
    }
  });
  document.getElementById('change-password-open').addEventListener('click', () => {
    passwordDialog.showModal();
    passwordForm.elements.current_password.focus();
  });
  document.getElementById('change-password-cancel').addEventListener('click', () => {
    passwordDialog.close();
  });
  passwordDialog.addEventListener('close', () => {
    passwordForm.reset();
    showFormMessage(passwordForm, '');
  });
  function showPlansComparison(restoreFocus = false) {
    plansDialog.dataset.view = 'compare';
    plansGrid.inert = false;
    plansGrid.removeAttribute('aria-hidden');
    plansFooter.inert = false;
    plansFooter.removeAttribute('aria-hidden');
    plansPayment.inert = true;
    plansPayment.setAttribute('aria-hidden', 'true');
    if (restoreFocus) plansUpgrade.focus();
  }

  function showPayment() {
    plansPayment.inert = false;
    plansPayment.removeAttribute('aria-hidden');
    plansDialog.dataset.view = 'payment';
    plansPayment.focus();
    plansGrid.inert = true;
    plansGrid.setAttribute('aria-hidden', 'true');
    plansFooter.inert = true;
    plansFooter.setAttribute('aria-hidden', 'true');
  }

  plansBuy.addEventListener('click', () => {
    showPlansComparison();
    plansGrid.scrollTop = 0;
    plansDialog.showModal();
  });
  plansClose.addEventListener('click', () => plansDialog.close());
  plansDialog.addEventListener('close', () => showPlansComparison());
  plansDialog.addEventListener('click', event => {
    if (event.target === plansDialog) plansDialog.close();
  });
  plansUpgrade.addEventListener('click', showPayment);
  paymentBack.addEventListener('click', () => showPlansComparison(true));
  paymentSerial.addEventListener('click', () => {
    plansDialog.close();
    licenseSerial.scrollIntoView({ block: 'center' });
    licenseSerial.focus();
    licenseSerial.select();
    licenseMessage.className = 'form-message license-message';
    licenseMessage.textContent = '请复制序列号发送给授权人，获取授权码后在此处导入。';
  });
  yearSelect.addEventListener('change', renderTravel);
  monthSelect.addEventListener('change', renderTravel);
  retry.addEventListener('click', () => {
    if (canViewTravelTrend) return loadTravel();
    startTravelLoading();
    return refreshTravelAccess();
  });
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('app-sidebar');
  toggle.addEventListener('click', () => {
    const open = sidebar.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  });
  document.getElementById('logout').addEventListener('click', async () => {
    try {
      await apiFetch('/api/logout', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
    } finally {
      window.location.assign('/login');
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      const selectedYear = yearSelect.value;
      const selectedMonth = monthSelect.value;
      startTravelLoading();
      refreshTravelAccess(selectedYear, selectedMonth);
      loadLicense();
    }
  });
  startTravelLoading();
  loadProfile();
  loadLicense();
  refreshTravelAccess();
}());
