const form = document.querySelector('#settings-form');
form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (form.dataset.spinnerActive === 'true') return;
    const values = {};
    form.querySelectorAll('[name]').forEach((field) => {
      values[field.name] = field.type === 'checkbox' ? field.checked : field.value;
    });
    const button = form.querySelector('button[type="submit"]');
    window.spinner.start(button);
    try {
      const response = await fetch(`/api/settings/${form.dataset.section}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({values}),
      });
      const payload = await response.json();
      if (!response.ok) {
        const detail = Array.isArray(payload.detail) ? payload.detail.join('; ') : payload.detail;
        window.showToast(detail || 'Could not save', {type: 'error'});
      }
      else {
        window.showToast('Saved successfully');
      }
    } catch (error) {
      window.showToast(error.message, {type: 'error'});
    } finally {
      window.spinner.stop(button);
    }
});