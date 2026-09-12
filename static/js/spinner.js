(() => {
  const spinner = {};
  const states = new WeakMap();

  spinner.start = (button) => {
    if (button.disabled) return false;
    const form = button.form;
    if (form && form.dataset.spinnerActive === 'true') return false;

    const controls = form ? [...form.querySelectorAll('input, select, textarea, button')] : [button];
    states.set(button, {
      controls: controls.map((control) => ({control, disabled: control.disabled})),
      form,
      label: button.textContent,
    });
    if (form) {
      form.dataset.spinnerActive = 'true';
      form.setAttribute('aria-busy', 'true');
    }
    controls.forEach((control) => {
      control.disabled = true;
    });
    button.classList.add('is-loading');
    button.setAttribute('aria-busy', 'true');
    button.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>Saving…</span>';
    return true;
  };

  spinner.stop = (button) => {
    const state = states.get(button);
    if (!state) return;
    state.controls.forEach(({control, disabled}) => {
      control.disabled = disabled;
    });
    if (state.form) {
      delete state.form.dataset.spinnerActive;
      state.form.removeAttribute('aria-busy');
    }
    button.classList.remove('is-loading');
    button.removeAttribute('aria-busy');
    button.textContent = state.label;
    states.delete(button);
  };

  window.spinner = spinner;
})();
