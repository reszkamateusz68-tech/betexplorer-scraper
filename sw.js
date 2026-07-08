// Pusty Service Worker, który oszukuje wymogi instalacji PWA w Chrome
self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
    // Nie robimy nic, po prostu przepuszczamy ruch
});
