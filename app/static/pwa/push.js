(() => {
  'use strict';
  const root = document.querySelector('[data-push-root]');
  if (!root) return;
  const button = root.querySelector('[data-push-toggle]');
  const status = root.querySelector('[data-push-status]');
  const base = root.dataset.pushBase;
  const tell = (text) => { status.textContent = text; };
  const bounded = (promise, ms = 10000) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    Promise.resolve(promise).then(resolve, reject).finally(() => clearTimeout(timer));
  });
  const api = async (suffix = '', value) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(base + suffix, {
        method: value === undefined ? 'GET' : 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, cache: 'no-store',
        signal: controller.signal, body: value === undefined ? undefined : JSON.stringify(value)
      });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error('request failed');
        error.userMessage = data.error || 'Не удалось сохранить настройки.';
        throw error;
      }
      return data;
    } finally { clearTimeout(timer); }
  };
  let registration, subscription, key, saved = false, ready = false;
  const paint = () => {
    button.textContent = !ready ? 'Повторить настройку уведомлений' : saved ? 'Выключить уведомления' : 'Включить уведомления';
    button.hidden = false;
  };
  const initialize = async () => {
    const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent || '') ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    if (ios && !standalone) {
      tell('На iPhone и iPad добавьте сайт на экран «Домой» и откройте оттуда. После этого здесь можно включить уведомления.');
      return;
    }
    if (!window.isSecureContext || !('serviceWorker' in navigator) ||
        !('PushManager' in window) || !('Notification' in window)) {
      tell('В этом браузере уведомления недоступны. Все остальные функции страницы работают без них.');
      return;
    }
    await bounded(navigator.serviceWorker.register('/client/service-worker.js', {scope: '/client/', updateViaCache: 'none'}));
    registration = await bounded(navigator.serviceWorker.ready);
    subscription = await bounded(registration.pushManager.getSubscription());
    const config = await api();
    key = Uint8Array.from(atob(config.publicKey.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(config.publicKey.length / 4) * 4, '=')), c => c.charCodeAt(0));
    // Only read the existing opt-in. Never subscribe or request permission on load.
    if (subscription && Notification.permission === 'granted') {
      saved = (await api('/status', {endpoint: subscription.endpoint})).enabled;
    }
    ready = true;
    paint();
    tell(saved ? 'Уведомления включены на этом устройстве.' : Notification.permission === 'denied'
      ? 'Уведомления заблокированы в настройках браузера. Сайт продолжит работать без них.'
      : 'Уведомления выключены. Разрешение запрашивается только по кнопке.');
  };
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      if (!ready) { await initialize(); return; }
      if (saved) {
        await api('/unsubscribe', {endpoint: subscription.endpoint});
        saved = false;
        // If the browser fails to remove its local subscription, the server has
        // already stopped delivery. Reconcile on the next explicit action.
        try { await bounded(subscription.unsubscribe()); } catch { /* optional */ }
        subscription = null;
        tell('Уведомления выключены на этом устройстве.');
      } else {
        // Must run directly in this click handler, before any network await (iOS).
        const permission = Notification.permission === 'default'
          ? await bounded(Notification.requestPermission(), 60000) : Notification.permission;
        if (permission !== 'granted') {
          tell(permission === 'denied' ? 'Разрешение не выдано. Изменить его можно в настройках браузера; сайт работает без уведомлений.'
            : 'Уведомления не включены. Можно попробовать позже.');
          return;
        }
        subscription = await bounded(registration.pushManager.getSubscription());
        if (!subscription) subscription = await bounded(registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key}));
        await api('', subscription.toJSON());
        saved = true;
        tell('Уведомления включены на этом устройстве.');
      }
    } catch (error) {
      tell(typeof error?.userMessage === 'string' ? error.userMessage + ' Остальные функции страницы доступны.'
        : 'Не удалось изменить уведомления. Попробуйте позже. Остальные функции страницы доступны.');
    } finally {
      paint();
      button.disabled = false;
    }
  });
  initialize().catch(() => {
    tell('Уведомления временно недоступны. Остальные функции страницы доступны.');
    // Retry initialization only on a deliberate click; no permission prompts.
    ready = false;
    paint();
    button.disabled = false;
  });
})();
