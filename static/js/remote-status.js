(() => {
  'use strict';
  const root = document.getElementById('remote-status');
  if (!root) return;
  const dot = root.querySelector('.remote-status-dot');
  const text = root.querySelector('.remote-status-limits');
  let timer;
  let pending = false;
  let controller;
  function render(data) {
    const reachable = data.reachable === true;
    root.dataset.state = reachable ? 'online' : 'offline';
    dot.setAttribute('aria-label', reachable ? 'Remote SSH available' : 'Remote SSH unavailable');
    dot.title = dot.getAttribute('aria-label');
    const limits = reachable && Array.isArray(data.limits) ? data.limits : [];
    text.textContent = limits.length ? `${data.agent === 'claude' ? 'Claude' : 'Codex'} limits remaining (reset at, UTC): ${limits.map(limit => {
      const date = new Date(limit.resets_at);
      const remaining = Number((100 - limit.used_percent).toFixed(2));
      return `${limit.name} ${remaining}% (${Number.isNaN(date.getTime()) ? '—' : date.toISOString().slice(0, 19).replace('T', ' ')})`;
    }).join(' / ')}` : '';
  }
  async function refresh() {
    if (pending) return;
    pending = true;
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch('/api/agent/status', { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error('Status unavailable');
      render(await response.json());
    } catch (_) {
      root.dataset.state = 'unknown';
      dot.setAttribute('aria-label', 'Remote SSH status unknown');
      dot.title = dot.getAttribute('aria-label');
      text.textContent = '';
    } finally {
      clearTimeout(timeout);
      pending = false;
    }
  }
  function start() {
    clearInterval(timer);
    refresh();
    timer = setInterval(refresh, 60000);
  }
  window.addEventListener('pagehide', () => { clearInterval(timer); if (controller) controller.abort(); });
  window.addEventListener('pageshow', event => { if (event.persisted) start(); });
  start();
})();
