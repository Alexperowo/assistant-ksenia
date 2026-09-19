// Ксения Service Worker — PWA offline capability and cache bypass for live API
const CACHE_NAME = 'ksenia-pwa-v1';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Never cache API calls or non-GET requests
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api')) {
    return;
  }
  event.respondWith(
    fetch(event.request, { cache: 'no-cache' }).catch(async () => {
      const cached = await caches.match(event.request);
      if (cached) return cached;
      if (event.request.mode === 'navigate') {
        const rootCached = await caches.match('/');
        if (rootCached) return rootCached;
      }
      return new Response('Нет соединения с сервером Ксении', {
        status: 503,
        statusText: 'Service Unavailable',
        headers: { 'Content-Type': 'text/plain; charset=utf-8' }
      });
    })
  );
});
