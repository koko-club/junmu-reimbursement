(function () {
  'use strict';
  const forms = document.querySelectorAll('[data-auth-form]');
  forms.forEach(function (form) {
    const submit = form.querySelector('button[type="submit"]');
    let lockUntil = 0;
    let countdownTimer = null;
    let countdownKind = 'account';

    function showFieldError(fieldName, message) {
      const field = form.querySelector('[name="' + fieldName + '"]');
      const detail = form.querySelector('[data-field-error="' + fieldName + '"]');
      if (!field || !detail) {
        showFormMessage(form, message, 'error');
        return;
      }
      detail.textContent = message;
      field.setAttribute('aria-invalid', 'true');
      field.focus();
    }

    function clearFieldErrors() {
      form.querySelectorAll('[data-field-error]').forEach(function (detail) { detail.textContent = ''; });
      form.querySelectorAll('[aria-invalid="true"]').forEach(function (field) { field.removeAttribute('aria-invalid'); });
    }

    function updateCountdown() {
      const seconds = Math.max(0, Math.ceil((lockUntil - Date.now()) / 1000));
      if (seconds === 0) {
        clearInterval(countdownTimer);
        countdownTimer = null;
        lockUntil = 0;
        submit.disabled = false;
        showFormMessage(form, '');
        return;
      }
      const text = countdownKind === 'account'
        ? '登录失败 5 次，已锁定 ' + seconds + ' 秒'
        : '登录请求过于频繁，请在 ' + seconds + ' 秒后重试';
      showFormMessage(form, text, 'error');
    }

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (lockUntil > Date.now()) return;
      showFormMessage(form, '');
      clearFieldErrors();
      const values = Object.fromEntries(new FormData(form));
      const confirm = values.confirm_password;
      delete values.confirm_password;
      if (form.querySelector('[name="confirm_password"]') && values.password !== confirm && values.new_password !== confirm) {
        showFieldError('confirm_password', '两次输入的密码不一致');
        return;
      }
      for (const field of form.querySelectorAll('[required]')) {
        if (!field.value.trim()) {
          showFieldError(field.name, field.previousElementSibling.textContent + '为必填项');
          return;
        }
      }
      submit.disabled = true;
      try {
        const data = await window.reimbursementApi(form, values);
        form.querySelectorAll('input[type="password"]').forEach(function (field) { field.value = ''; });
        showFormMessage(form, data.message || '操作成功', 'success');
        if (data.next) window.location.assign(data.next);
      } catch (error) {
        if (form.dataset.endpoint === '/api/login' && (error.lockSeconds || error.retryAfter)) {
          countdownKind = error.lockSeconds ? 'account' : 'rate';
          lockUntil = Date.now() + (error.lockSeconds || error.retryAfter) * 1000;
          clearInterval(countdownTimer);
          updateCountdown();
          countdownTimer = setInterval(updateCountdown, 1000);
        } else {
          if (error.field && /^[a-z_]+$/.test(error.field)) {
            showFieldError(error.field, error.message || '输入内容不符合要求');
          } else {
            showFormMessage(form, error.message || '请求失败，请稍后重试', 'error');
          }
        }
      } finally {
        if (!lockUntil) submit.disabled = false;
      }
    });
  });
}());
