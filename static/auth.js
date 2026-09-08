(function () {
  'use strict';
  const forms = document.querySelectorAll('[data-auth-form]');
  forms.forEach(function (form) {
    const submit = form.querySelector('button[type="submit"]');
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      showFormMessage(form, '');
      const values = Object.fromEntries(new FormData(form));
      const confirm = values.confirm_password;
      delete values.confirm_password;
      if (form.querySelector('[name="confirm_password"]') && values.password !== confirm && values.new_password !== confirm) {
        showFormMessage(form, '两次输入的密码不一致', 'error');
        return;
      }
      for (const field of form.querySelectorAll('[required]')) {
        if (!field.value.trim()) {
          showFormMessage(form, field.previousElementSibling.textContent + '为必填项', 'error');
          field.focus();
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
        showFormMessage(form, error.message || '请求失败，请稍后重试', 'error');
      } finally {
        submit.disabled = false;
      }
    });
  });
}());
