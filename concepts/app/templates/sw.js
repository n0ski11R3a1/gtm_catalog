const CACHE = 'gtm-erp-v97';
// v97: install caches ONLY the public asset. Role-gated pages must never be
// precached together: the old all-or-nothing addAll(['/catalog',
// '/delivery-offline', '/static/style.css', '/orders/new']) failed for every
// non-Owner role — Sales gets 403 on /delivery-offline, Delivery gets 403
// on /orders/new — so one refused page left staff phones with no offline
// copy at all (even /catalog would not survive a refresh). Each navigation
// page is instead cached individually on its own first successful online
// visit in the navigate handler below, so a 403 on one page can never break
// the others.
const CORE = ['/static/style.css'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(CORE)).then(()=>self.skipWaiting())); });
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(names => Promise.all(names.filter(n => n !== CACHE).map(n => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});
// v78: a handler must NEVER resolve null/undefined — offline Safari turns
// that into a full-page 'Returned response is null' error. Navigations fall
// back to the cached /catalog page (the offline-capable surface); everything
// else falls back to a 503 Response on a double miss.
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/static/product-images/')) {
    e.respondWith(caches.open(CACHE).then(async c => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      try { const res = await fetch(e.request); c.put(e.request, res.clone()); return res; }
      catch (err) { return new Response('', {status: 503, statusText: 'Offline'}); }
    }));
    return;
  }
  if (url.pathname === '/api/products') {
    e.respondWith(fetch(e.request).catch(async () => {
      const hit = await caches.match(e.request);
      return hit || new Response('', {status: 503, statusText: 'Offline'});
    }));
    return;
  }
  if (e.request.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const res = await fetch(e.request);
        // Cache only a successful same-origin page, on its own: a 403/404/500
        // must never be stored, or offline would serve the error page itself.
        if (res.ok && url.origin === self.location.origin) {
          const c = await caches.open(CACHE);
          c.put(e.request, res.clone());
        }
        return res;
      } catch (err) {}
      const hit = await caches.match(e.request);
      if (hit) return hit;
      const cat = await caches.match('/catalog');
      if (cat) return cat;
      return new Response('', {status: 503, statusText: 'Offline'});
    })());
    return;
  }
  e.respondWith(fetch(e.request).catch(async () => {
    const hit = await caches.match(e.request);
    return hit || new Response('', {status: 503, statusText: 'Offline'});
  }));
});
