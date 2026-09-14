(() => {
  const form = document.querySelector('#http-proxy-form');
  if (!form) return;
  const blocks = form.querySelector('#proxies');

  function updateRemoveButtons(list) {
    list.querySelectorAll('[data-action="remove-rule"]').forEach(button => {
      button.disabled = list.children.length === 1;
    });
  }

  function addRule(list, value) {
    const method = list.dataset.kind === 'methods';
    const template = document.querySelector(method ? '#proxy-method-template' : '#proxy-pattern-template');
    const row = template.content.firstElementChild.cloneNode(true);
    if (method) row.querySelector('select').value = value || 'GET';
    else {
      row.querySelector('input').value = value ? value.pattern : '';
      row.querySelector('[data-action="operator"]').textContent = value ? value.operator : '=';
    }
    list.append(row);
    updateRemoveButtons(list);
  }

  function addProxy(value) {
    const block = document.querySelector('#proxy-template').content.firstElementChild.cloneNode(true);
    if (value) {
      for (const name of ['port', 'timeout', 'remote_url']) block.querySelector(`[name="${name}"]`).value = value[name];
    }
    const methods = value ? (value.methods.length && !value.methods.includes('ALL') ? value.methods : ['ALL']) : ['GET'];
    methods.forEach(method => addRule(block.querySelector('[data-kind="methods"]'), method));
    const patterns = value && value.patterns.length ? value.patterns : [{operator: '=', pattern: ''}];
    patterns.forEach(pattern => addRule(block.querySelector('[data-kind="patterns"]'), pattern));
    blocks.append(block);
  }

  try {
    JSON.parse(form.dataset.proxies).forEach(addProxy);
  } catch (error) {
    window.showToast('Could not load HTTP proxy configuration', {type: 'error'});
    form.querySelector('button[type="submit"]').disabled = true;
  }
  form.querySelector('#add-proxy').addEventListener('click', () => addProxy());
  form.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button || form.dataset.spinnerActive === 'true') return;
    const list = button.closest('.proxy-list');
    switch (button.dataset.action) {
      case 'remove-proxy': button.closest('.proxy-block').remove(); break;
      case 'add-rule': addRule(list); break;
      case 'remove-rule':
        if (list.children.length > 1) button.closest('.proxy-rule').remove();
        updateRemoveButtons(list);
        break;
      case 'operator': button.textContent = button.textContent === '=' ? '!' : '='; break;
    }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.dataset.spinnerActive === 'true') return;
    const proxies = Array.from(blocks.children, block => {
      const methods = Array.from(block.querySelectorAll('[name="method"]'), field => field.value);
      return {
        port: block.querySelector('[name="port"]').value,
        timeout: block.querySelector('[name="timeout"]').value,
        remote_url: block.querySelector('[name="remote_url"]').value,
        methods: methods.includes('ALL') ? ['ALL'] : methods,
        patterns: Array.from(block.querySelectorAll('.pattern-rule'), row => ({
          operator: row.querySelector('[data-action="operator"]').textContent,
          pattern: row.querySelector('[name="pattern"]').value,
        })).filter(rule => rule.pattern !== ''),
      };
    });
    const button = form.querySelector('button[type="submit"]');
    window.spinner.start(button);
    try {
      const response = await fetch('/api/settings/http_proxy', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({values: {proxies}}),
      });
      const payload = await response.json();
      if (response.ok) {
        const message = payload.restart_required
          ? 'Saved. Methods and patterns applied to running proxies. Restart to apply proxy additions, removals, port, remote URL or timeout changes.'
          : 'Saved. Methods and patterns applied without restarting.';
        window.showToast(message, {duration: 6000});
      }
      else {
        const detail = Array.isArray(payload.detail) ? payload.detail.join('; ') : payload.detail;
        window.showToast(detail || 'Could not save HTTP proxy configuration', {type: 'error'});
      }
    } catch (error) {
      window.showToast('Could not save HTTP proxy configuration', {type: 'error'});
    } finally {
      window.spinner.stop(button);
    }
  });
})();
