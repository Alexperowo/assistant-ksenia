// Ксения: офлайн-оболочка приложения. Сначала сеть (всегда свежая версия), из кэша — только если сети нет.
// API, WebSocket и звук никогда не кэшируются.
const CACHE = 'ksenia-pwa-v3';  // v2: контраст кнопок для слабого зрения
const SHELL = ['/', '/app.js', '/control.js', '/styles.css', '/recorder-worklet.js', '/manifest.webmanifest',
  '/icon-192x192.png', '/icon-512x512.png', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(e.request, { cache: 'no-cache' }).then((resp) => {
    if (resp.ok && SHELL.includes(url.pathname)) {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
    }
    return resp;
  }).catch(() => caches.match(e.request).then((hit) => hit || caches.match('/'))));
});
