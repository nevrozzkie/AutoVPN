(() => {
  const button = document.querySelector('[data-pwa-install]');
  const help = document.querySelector('[data-pwa-help]');
  const section = document.querySelector('[data-pwa-section]');
  let promptEvent;
  const standalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  const installed = () => {
    if (button) button.hidden = true;
    if (section) section.hidden = true;
    promptEvent = undefined;
  };
  if (standalone()) installed();
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    if (standalone()) return;
    promptEvent = event;
    if (button) button.hidden = false;
  });
  window.addEventListener('appinstalled', installed);
  button?.addEventListener('click', async () => {
    if (!promptEvent) {
      if (help) {
        help.textContent = /iPhone|iPad|iPod/.test(navigator.userAgent || '')
          ? 'В Safari нажмите «Поделиться» → «На экран Домой». Браузер требует выполнить этот шаг вручную.'
          : 'Откройте меню браузера → «Установить приложение» или «Добавить на главный экран». Если предложение установки появится, эта кнопка откроет его.';
      }
      return;
    }
    const pending = promptEvent;
    promptEvent = undefined;
    try {
      await pending.prompt();
      await pending.userChoice;
    } catch {
      if (help) help.textContent = 'Установите AutoVPN через меню браузера: «Добавить на главный экран».';
    }
  });
  if (!window.isSecureContext) {
    if (help) help.textContent = 'Для установки откройте личную страницу AutoVPN по HTTPS.';
    return;
  }
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/client/service-worker.js', {
      scope: '/client/', updateViaCache: 'none'
    }).catch(() => {
      if (help) help.textContent = 'Не удалось подготовить приложение. Обновите страницу или добавьте её на главный экран через меню браузера.';
    });
  }
})();
