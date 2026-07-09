self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
    // Przepuszcza ruch, spełniając wymóg Chrome dla aplikacji PWA
    e.respondWith(fetch(e.request).catch(() => new Response('Oczekiwanie na połączenie sieciowe...')));
});
