(function () {
  'use strict';

  const form = document.getElementById('reimbursement-form');
  if (!form) return;

  const inputs = Array.from(form.querySelectorAll('input[type="date"]'));
  if (!inputs.length) return;

  const weekdayLabels = ['一', '二', '三', '四', '五', '六', '日'];
  const iconNamespace = 'http://www.w3.org/2000/svg';
  const today = getTodayParts();
  if (!today) return;
  const todayIso = toIso(today.year, today.month, today.day);
  let activeInput = null;
  let viewYear = today.year;
  let viewMonth = today.month;
  let focusedIso = null;
  let suppressFocusOpen = false;
  let pointerInsidePopover = false;

  const popover = document.createElement('div');
  popover.id = 'date-picker-popover';
  popover.className = 'date-picker-popover';
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-label', '选择日期');
  popover.hidden = true;

  const header = document.createElement('div');
  header.className = 'date-picker-header';
  const previousButton = iconButton('上个月', 'chevron-left');
  const nextButton = iconButton('下个月', 'chevron-right');
  const title = document.createElement('h3');
  title.className = 'date-picker-title';
  title.setAttribute('aria-live', 'polite');
  header.append(previousButton, title, nextButton);

  const weekRow = document.createElement('div');
  weekRow.className = 'date-picker-weekdays';
  weekRow.setAttribute('role', 'row');
  weekdayLabels.forEach(function (label) {
    const cell = document.createElement('span');
    cell.className = 'date-picker-weekday';
    cell.setAttribute('role', 'columnheader');
    cell.textContent = label;
    weekRow.appendChild(cell);
  });

  const grid = document.createElement('div');
  grid.className = 'date-picker-grid';
  grid.setAttribute('role', 'grid');
  grid.setAttribute('aria-label', '日期');
  const dayCells = [];
  const dayButtons = [];
  for (let index = 0; index < 42; index += 1) {
    if (index % 7 === 0) {
      const row = document.createElement('div');
      row.className = 'date-picker-row';
      row.setAttribute('role', 'row');
      grid.appendChild(row);
    }
    const row = grid.lastElementChild;
    const cell = document.createElement('div');
    cell.className = 'date-picker-cell';
    cell.setAttribute('role', 'gridcell');
    const dayButton = document.createElement('button');
    dayButton.type = 'button';
    dayButton.className = 'date-picker-day';
    cell.appendChild(dayButton);
    row.appendChild(cell);
    dayCells.push(cell);
    dayButtons.push(dayButton);
  }

  const footer = document.createElement('div');
  footer.className = 'date-picker-footer';
  const todayButton = document.createElement('button');
  todayButton.type = 'button';
  todayButton.className = 'date-picker-today';
  todayButton.textContent = '今天';
  const clearButton = document.createElement('button');
  clearButton.type = 'button';
  clearButton.className = 'date-picker-clear';
  clearButton.textContent = '清除';
  footer.append(todayButton, clearButton);

  popover.append(header, weekRow, grid, footer);
  document.body.appendChild(popover);

  inputs.forEach(enhanceInput);
  previousButton.addEventListener('click', function (event) {
    event.preventDefault();
    shiftMonth(-1);
  });
  nextButton.addEventListener('click', function (event) {
    event.preventDefault();
    shiftMonth(1);
  });
  todayButton.addEventListener('click', function (event) {
    event.preventDefault();
    viewYear = today.year;
    viewMonth = today.month;
    render();
    selectDate(todayIso);
  });
  clearButton.addEventListener('click', function (event) {
    event.preventDefault();
    clearDate();
  });
  grid.addEventListener('click', function (event) {
    const cell = event.target.closest('[data-date]');
    if (!cell || !grid.contains(cell)) return;
    event.preventDefault();
    selectDate(cell.dataset.date);
  });
  grid.addEventListener('keydown', handleGridKeydown);
  popover.addEventListener('focusout', handlePopoverFocusout);
  document.addEventListener('pointerdown', handleOutsidePointer, true);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !popover.hidden) {
      event.preventDefault();
      closePopover(true);
    }
  });
  window.addEventListener('resize', function () {
    if (!popover.hidden) positionPopover();
  });
  window.addEventListener('scroll', function () {
    if (!popover.hidden) positionPopover();
  }, true);
  form.addEventListener('reset', function () {
    window.setTimeout(function () {
      closePopover(true);
      inputs.forEach(syncInputState);
    }, 0);
  });

  function enhanceInput(input) {
    addInputShell(input);
    input.dataset.calendarEnhanced = 'true';
    input.setAttribute('aria-haspopup', 'dialog');
    input.setAttribute('aria-controls', popover.id);
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('title', '选择日期');
    input.readOnly = true;
    syncInputState(input);

    input.addEventListener('pointerdown', function (event) {
      if (event.button !== 0) return;
      event.preventDefault();
      openPopover(input);
    });
    input.addEventListener('click', function (event) {
      event.preventDefault();
      openPopover(input);
    });
    input.addEventListener('focus', function () {
      if (!suppressFocusOpen) openPopover(input);
    });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown') {
        event.preventDefault();
        openPopover(input);
      } else if (event.key === 'Backspace' || event.key === 'Delete') {
        event.preventDefault();
        clearInputValue(input);
      } else if (event.key === 'Escape' && !popover.hidden) {
        event.preventDefault();
        closePopover(true);
      }
    });
    input.addEventListener('input', function () {
      syncInputState(input);
      if (activeInput === input && !popover.hidden) render();
    });
    input.addEventListener('change', function () {
      syncInputState(input);
      if (activeInput === input && !popover.hidden) render();
    });
  }

  function addInputShell(input) {
    if (input.parentElement && input.parentElement.classList.contains('date-picker-field')) return;
    const shell = document.createElement('span');
    shell.className = 'date-picker-field';
    input.parentNode.insertBefore(shell, input);
    shell.appendChild(input);
    const icon = document.createElementNS(iconNamespace, 'svg');
    icon.classList.add('date-picker-field-icon');
    icon.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(iconNamespace, 'use');
    use.setAttribute('href', '/static/icons.svg#calendar-days');
    icon.appendChild(use);
    shell.appendChild(icon);
  }

  function openPopover(input) {
    if (activeInput === input && !popover.hidden) {
      positionPopover();
      return;
    }
    activeInput = input;
    const selected = parseIso(input.value);
    const initial = selected || today;
    viewYear = initial.year;
    viewMonth = initial.month;
    focusedIso = selected ? toIso(selected.year, selected.month, selected.day) : todayIso;
    input.setAttribute('aria-expanded', 'true');
    popover.hidden = false;
    render();
    positionPopover();
    focusCell(focusedIso);
  }

  function closePopover(restoreFocus) {
    const closingInput = activeInput;
    const focusWasInside = popover.contains(document.activeElement);
    popover.hidden = true;
    if (closingInput) closingInput.setAttribute('aria-expanded', 'false');
    activeInput = null;
    focusedIso = null;
    if ((restoreFocus || focusWasInside) && closingInput && document.contains(closingInput)) {
      suppressFocusOpen = true;
      closingInput.focus({ preventScroll: true });
      suppressFocusOpen = false;
    }
  }

  function selectDate(iso) {
    if (!activeInput || !parseIso(iso)) return;
    activeInput.value = iso;
    activeInput.dispatchEvent(new Event('input', { bubbles: true }));
    activeInput.dispatchEvent(new Event('change', { bubbles: true }));
    syncInputState(activeInput);
    closePopover(true);
  }

  function clearDate() {
    if (!activeInput) return;
    const input = activeInput;
    clearInputValue(input);
    closePopover(true);
  }

  function clearInputValue(input) {
    if (input.value !== '') {
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    syncInputState(input);
    if (activeInput === input && !popover.hidden) render();
  }

  function shiftMonth(delta) {
    viewMonth += delta;
    if (viewMonth < 1) {
      viewMonth = 12;
      viewYear -= 1;
    } else if (viewMonth > 12) {
      viewMonth = 1;
      viewYear += 1;
    }
    focusedIso = toIso(viewYear, viewMonth, 1);
    render();
    positionPopover();
  }

  function render() {
    title.textContent = viewYear + '年' + viewMonth + '月';
    const selected = activeInput ? parseIso(activeInput.value) : null;
    const selectedIso = selected ? toIso(selected.year, selected.month, selected.day) : '';
    clearButton.disabled = !selectedIso;
    const firstWeekday = mondayFirstWeekday(viewYear, viewMonth, 1);
    const monthDays = daysInMonth(viewYear, viewMonth);
    for (let index = 0; index < 42; index += 1) {
      const date = dateFromOffset(viewYear, viewMonth, index - firstWeekday + 1);
      const iso = toIso(date.year, date.month, date.day);
      const cell = dayCells[index];
      const dayButton = dayButtons[index];
      cell.className = 'date-picker-cell';
      cell.setAttribute('aria-selected', String(iso === selectedIso));
      dayButton.className = 'date-picker-day';
      dayButton.dataset.date = iso;
      dayButton.textContent = String(date.day);
      dayButton.setAttribute('aria-label', date.year + '年' + date.month + '月' + date.day + '日');
      dayButton.setAttribute('aria-pressed', String(iso === selectedIso));
      dayButton.tabIndex = iso === (focusedIso || selectedIso || todayIso) ? 0 : -1;
      if (iso === selectedIso) dayButton.classList.add('is-selected');
      if (iso === todayIso) dayButton.classList.add('is-today');
      if (date.month !== viewMonth || date.year !== viewYear) dayButton.classList.add('is-outside-month');
      dayButton.setAttribute('data-month-days', String(monthDays));
    }
  }

  function handleGridKeydown(event) {
    const current = event.target.closest('[data-date]');
    if (!current) return;
    const currentDate = parseIso(current.dataset.date);
    if (!currentDate) return;
    if (event.key === 'Backspace' || event.key === 'Delete') {
      event.preventDefault();
      clearDate();
      return;
    }
    let nextIso = null;
    if (event.key === 'ArrowLeft') nextIso = moveDate(currentDate, -1);
    if (event.key === 'ArrowRight') nextIso = moveDate(currentDate, 1);
    if (event.key === 'ArrowUp') nextIso = moveDate(currentDate, -7);
    if (event.key === 'ArrowDown') nextIso = moveDate(currentDate, 7);
    if (event.key === 'Home') nextIso = moveDate(currentDate, -mondayFirstWeekday(currentDate.year, currentDate.month, currentDate.day));
    if (event.key === 'End') nextIso = moveDate(currentDate, 6 - mondayFirstWeekday(currentDate.year, currentDate.month, currentDate.day));
    if (nextIso) {
      event.preventDefault();
      focusCell(nextIso);
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      selectDate(current.dataset.date);
    }
  }

  function focusCell(iso) {
    const date = parseIso(iso);
    if (!date) return;
    if (date.year !== viewYear || date.month !== viewMonth) {
      viewYear = date.year;
      viewMonth = date.month;
      focusedIso = iso;
      render();
    } else {
      focusedIso = iso;
      grid.querySelectorAll('[data-date]').forEach(function (cell) {
        cell.tabIndex = cell.dataset.date === iso ? 0 : -1;
      });
    }
    const cell = grid.querySelector('button[data-date="' + iso + '"]');
    if (cell) {
      cell.focus({ preventScroll: true });
      if (typeof cell.scrollIntoView === 'function') cell.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }

  function handlePopoverFocusout(event) {
    const inputAtLeave = activeInput;
    const nextTarget = event.relatedTarget;
    if (pointerInsidePopover || (nextTarget && popover.contains(nextTarget))) return;
    window.setTimeout(function () {
      pointerInsidePopover = false;
      if (popover.hidden || popover.contains(document.activeElement) || activeInput !== inputAtLeave) return;
      closePopover(false);
    }, 0);
  }

  function handleOutsidePointer(event) {
    if (popover.hidden) return;
    pointerInsidePopover = popover.contains(event.target);
    if (pointerInsidePopover) {
      window.setTimeout(function () { pointerInsidePopover = false; }, 0);
      return;
    }
    if (activeInput && event.target === activeInput) return;
    closePopover(true);
  }

  function positionPopover() {
    if (!activeInput || popover.hidden) return;
    const rect = activeInput.getBoundingClientRect();
    const width = popover.offsetWidth || 320;
    const height = popover.offsetHeight || 390;
    const gutter = 12;
    let left = rect.left;
    let top = rect.bottom + 8;
    if (left + width > window.innerWidth - gutter) left = window.innerWidth - width - gutter;
    if (left < gutter) left = gutter;
    if (top + height > window.innerHeight - gutter && rect.top - height - 8 >= gutter) top = rect.top - height - 8;
    if (top < gutter) top = gutter;
    popover.style.left = Math.round(left) + 'px';
    popover.style.top = Math.round(top) + 'px';
  }

  function syncInputState(input) {
    input.classList.toggle('has-value', input.value.trim() !== '');
  }

  function iconButton(label, iconName) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'date-picker-nav';
    button.setAttribute('aria-label', label);
    button.title = label;
    const svg = document.createElementNS(iconNamespace, 'svg');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(iconNamespace, 'use');
    use.setAttribute('href', '/static/icons.svg#' + iconName);
    svg.appendChild(use);
    button.appendChild(svg);
    return button;
  }

  function parseIso(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || '').trim());
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null;
    return { year: year, month: month, day: day };
  }

  function toIso(year, month, day) {
    return String(year).padStart(4, '0') + '-' + String(month).padStart(2, '0') + '-' + String(day).padStart(2, '0');
  }

  function isLeapYear(year) {
    return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  }

  function daysInMonth(year, month) {
    if (month === 2) return isLeapYear(year) ? 29 : 28;
    return [4, 6, 9, 11].indexOf(month) >= 0 ? 30 : 31;
  }

  function mondayFirstWeekday(year, month, day) {
    const offsets = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
    let adjustedYear = year;
    if (month < 3) adjustedYear -= 1;
    const sunday = (adjustedYear + Math.floor(adjustedYear / 4) - Math.floor(adjustedYear / 100) + Math.floor(adjustedYear / 400) + offsets[month - 1] + day) % 7;
    return (sunday + 6) % 7;
  }

  function dateFromOffset(year, month, offset) {
    let targetYear = year;
    let targetMonth = month;
    let targetDay = offset;
    while (targetDay < 1) {
      targetMonth -= 1;
      if (targetMonth < 1) {
        targetMonth = 12;
        targetYear -= 1;
      }
      targetDay += daysInMonth(targetYear, targetMonth);
    }
    while (targetDay > daysInMonth(targetYear, targetMonth)) {
      targetDay -= daysInMonth(targetYear, targetMonth);
      targetMonth += 1;
      if (targetMonth > 12) {
        targetMonth = 1;
        targetYear += 1;
      }
    }
    return { year: targetYear, month: targetMonth, day: targetDay };
  }

  function moveDate(date, delta) {
    let year = date.year;
    let month = date.month;
    let day = date.day;
    const direction = delta < 0 ? -1 : 1;
    let remaining = Math.abs(delta);
    while (remaining > 0) {
      day += direction;
      if (day < 1) {
        month -= 1;
        if (month < 1) {
          month = 12;
          year -= 1;
        }
        day = daysInMonth(year, month);
      } else if (day > daysInMonth(year, month)) {
        day = 1;
        month += 1;
        if (month > 12) {
          month = 1;
          year += 1;
        }
      }
      remaining -= 1;
    }
    return toIso(year, month, day);
  }

  function getTodayParts() {
    try {
      const parts = new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      }).formatToParts(Date.now());
      const values = {};
      parts.forEach(function (part) {
        if (part.type === 'year' || part.type === 'month' || part.type === 'day') values[part.type] = Number(part.value);
      });
      if (values.year && values.month && values.day) return values;
    } catch (_) {
      return null;
    }
    return null;
  }
})();
