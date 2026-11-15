// User pages always come from the server; never cache tokens or VPN profiles.
const OFFLINE = `<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0d1117"><title>AutoVPN · Нет сети</title>
<style>body{margin:0;background:#0d1117;color:#e6edf3;font:18px/1.6 system-ui;display:grid;min-height:100dvh;place-items:center}main{max-width:28rem;padding:24px}a{display:inline-block;background:#0f766e;color:white;padding:12px 20px;border-radius:8px;text-decoration:none}</style>
<main><h1>Нет соединения</h1><p>Подключитесь к Интернету, чтобы получить актуальные настройки AutoVPN.</p><a href="">Повторить</a></main></html>`;
self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || event.request.mode !== 'navigate' ||
      url.origin !== self.location.origin || !url.pathname.startsWith('/client/')) return;
  event.respondWith(fetch(event.request, {cache: 'no-store'}).catch(() =>
    new Response(OFFLINE, {status: 503, headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
      'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    }})
  ));
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    const parsed = event.data ? event.data.json() : {};
    if (parsed && typeof parsed === 'object') data = parsed;
  } catch { /* still show a visible notification */ }
  event.waitUntil(self.registration.showNotification(
    typeof data.title === 'string' ? data.title.slice(0, 80) : 'Уведомление', {
      body: typeof data.body === 'string' ? data.body.slice(0, 500) : 'Новое уведомление',
      tag: typeof data.tag === 'string' ? data.tag : 'notice',
      renotify: false,
      data: {url: typeof data.url === 'string' && /^\/client\/[A-Za-z0-9_-]+$/.test(data.url) ? data.url : '/client/'}
    }
  ));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const path = event.notification.data?.url;
  if (typeof path !== 'string' || !/^\/client\/[A-Za-z0-9_-]+$/.test(path)) return;
  const url = new URL(path, self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async (windows) => {
    const existing = windows.find(client => client.url.split(/[?#]/)[0] === url);
    if (existing) return existing.focus();
    return self.clients.openWindow(url);
  }));
});
