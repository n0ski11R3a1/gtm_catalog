// =====================================================
// GTM PRODUCT & PRICE CATALOG - Service Worker
// =====================================================

// Bumped v8 -> v9: navigation requests fell back to caches.match('/') when
// offline, but '/' was never actually being cached anywhere - STATIC_ASSETS
// only lists CSS/JS/manifest. That fallback pointed at an empty slot, so
// true offline page loads never worked. Now the catalog page ('/' only -
// never /admin or /login, to preserve the v1->v2 fix that stopped stale
// authenticated pages from causing a login loop) gets cached every time
// it's successfully fetched online, so there's something real to fall
// back to when offline.
//
// Bumped v9 -> v10: also precache '/' during the install step itself, not
// only on the next successful navigation. Closes a gap specifically
// relevant to iOS - a device can add this to the Home Screen while online
// but not complete a full page navigation before going offline, in which
// case nothing would be cached for '/' yet under v9 alone.
const CACHE_NAME = 'gtm-catalog-v19';

const STATIC_ASSETS = [
    '/static/css/style.css',
    '/static/css/admin.css',
    '/static/js/app.js',
    '/static/js/admin.js',
    '/static/js/order.js',
    '/static/manifest.json'
];

// Install: pre-cache static assets, plus the catalog page itself so
// there's something real to fall back to offline even before the first
// successful navigation completes (see v9->v10 note above). The '/'
// precache is isolated in its own promise with a swallowed .catch() so a
// transient failure there never blocks the STATIC_ASSETS precache.
self.addEventListener('install', (event) => {
    event.waitUntil(
        Promise.all([
            caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)),
            caches.open(CACHE_NAME).then((cache) => cache.add('/').catch(() => {})),
        ])
    );
    self.skipWaiting();
});

// Activate: clean up old caches
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((key) => key !== CACHE_NAME)
                    .map((key) => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

// Fetch strategy:
// - HTML page navigations (/, /login, /admin, /admin/products, etc.):
//   ALWAYS go to the network. Session/auth state changes per request,
//   so these must never be served from cache.
// - API calls (/api/*): network-first, fall back to cache if offline.
// - Static assets (css/js/manifest): cache-first, refreshed in the background.
self.addEventListener('fetch', (event) => {
    const { request } = event;

    if (request.method !== 'GET') {
        return;
    }

    // Navigation requests = full page loads. Session/auth state changes
    // per request, so admin/login pages must NEVER be served from cache -
    // that's the original v1->v2 fix, preserved below.
    //
    // The public catalog page ('/' only) is the one exception: every
    // successful online visit re-caches it, so there's a real fallback to
    // serve when the device is genuinely offline (e.g. a rep out in the
    // field with no signal). It'll be whatever was cached on the last
    // successful load - not live data, but usable, which is the whole
    // point of "offline mode" for this page.
    if (request.mode === 'navigate') {
        const url = new URL(request.url);
        const isCatalogHome = url.pathname === '/';

        event.respondWith(
            fetch(request)
                .then((response) => {
                    if (isCatalogHome) {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put('/', clone));
                    }
                    return response;
                })
                .catch(() => {
                    if (isCatalogHome) {
                        return caches.match('/');
                    }
                    // Admin/login/etc while offline: no cached fallback,
                    // by design - better an honest network error than a
                    // stale authenticated page.
                    return caches.match(request);
                })
        );
        return;
    }

    const url = new URL(request.url);

    if (url.pathname.startsWith('/api/')) {
        event.respondWith(
            fetch(request)
                .then((response) => {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
                    return response;
                })
                .catch(() => caches.match(request))
        );
        return;
    }

    // Static assets: cache-first
    event.respondWith(
        caches.match(request).then((cached) => {
            if (cached) {
                return cached;
            }

            return fetch(request).then((response) => {
                const clone = response.clone();
                caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
                return response;
            });
        })
    );
});
