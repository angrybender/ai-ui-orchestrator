(function () {
  const region = document.createElement('div');
  region.className = 'toast-region';
  region.setAttribute('aria-live', 'polite');
  region.setAttribute('aria-atomic', 'true');
  document.body.append(region);

  window.showToast = function (message, options = {}) {
    const toast = document.createElement('div');
    toast.className = `toast${options.type === 'error' ? ' is-error' : ''}`;
    toast.setAttribute('role', options.type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    region.append(toast);
    requestAnimationFrame(() => toast.classList.add('is-visible'));

    window.setTimeout(() => {
      toast.classList.remove('is-visible');
      toast.addEventListener('transitionend', () => toast.remove(), {once: true});
    }, options.duration || 3000);
  };
}());
