/* 微知 Service Worker：网络优先，缓存兜底（静态资源），API 不缓存 */
const CACHE = 'weizhi-v1';
const CORE = ['/', '/reader.html', '/manifest.json', '/icon.svg', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // API / 数据接口不缓存，直连网络（保证卡片/进度实时）
  if (url.origin === location.origin && url.pathname.startsWith('/api/')) return;
  // 其他 GET：网络优先，失败时缓存兜底
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((m) => m || caches.match('/'))
      )
  );
});
