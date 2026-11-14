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
