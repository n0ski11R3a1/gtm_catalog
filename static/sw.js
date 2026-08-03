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
//
// Bumped v19 -> v20: product detail pages (/product/<id>) can now be
// cached for offline use too, same idea as '/' - but in a SEPARATE,
// stable cache bucket (PRODUCT_PAGES_CACHE) rather than the main
// versioned CACHE_NAME. Reasoning: CACHE_NAME gets fully wiped and
// rebuilt on every version bump (routine CSS/JS deploys). With 225+
// product pages potentially precached, re-downloading all of them on
// every unrelated style tweak would be wasteful and slow, especially on
// a rep's mobile data. PRODUCT_PAGES_CACHE persists across normal
// version bumps and is only cleared if ITS OWN name changes.
//
// v20->v21: added a message listener so the page can ask "what version
// are you running" (powers a version display in the new Updates &
// Status panel) - service workers and page scripts are separate
// contexts, so this is the only way the page can know the SW's live
// CACHE_NAME without hardcoding a guess that could drift out of sync.
//
// v21->v22: BUG FIX - product images (/static/product-images/*) were
// GET requests that aren't /product/, aren't a navigation, and aren't
// /api/, so they fell through to the generic "static assets: cache-first"
// handler at the bottom and landed in CACHE_NAME - the VERSIONED bucket
// that gets fully wiped on every routine CSS/JS deploy. An image a rep
// already viewed offline would silently vanish from the offline cache on
// the next unrelated version bump. Product images now get their own
// path check (same pattern as /product/ below) routing them into
// PRODUCT_IMAGES_CACHE, a persistent bucket excluded from cleanup below -
// same reasoning as PRODUCT_PAGES_CACHE.
// v22->v23: BUG FIX - Gallery mode's own pages (/gallery and
// /gallery/<category>) were navigation requests that weren't '/', so
// they fell into the generic "Admin/login/etc while offline: no cached
// fallback, by design" branch below - the same branch meant for
// session-dependent pages. But Gallery is public/browsing-only, same
// audience as '/' and /product/<id> (see comments on those), so it
// should never have been lumped in with admin/login. Gallery pages now
// get the same cache-and-serve treatment as /product/ pages, in their
// own persistent bucket (GALLERY_PAGES_CACHE) so a routine CSS/JS
// version bump doesn't force re-downloading every category page -
// same reasoning as PRODUCT_PAGES_CACHE/PRODUCT_IMAGES_CACHE.
// v23->v24: BUG FIX - category names containing "&" (e.g. "Baby &
// Health", "Food & Snacks") were being cached under a different key
// than the one a real navigation actually requested, because a browser
// leaves "&" unescaped in a path (only spaces get auto-encoded) while
// precache.js's encodeURIComponent() escapes it to "%26". Any category
// without "&" happened to produce identical strings both ways, which
// is why this only ever broke those two categories. Gallery cache
// reads/writes now go through canonicalizeGalleryPath() so both forms
// resolve to one key.
const CACHE_NAME = 'gtm-catalog-v25';
const PRODUCT_PAGES_CACHE = 'gtm-product-pages-v1';
const PRODUCT_IMAGES_CACHE = 'gtm-product-images-v1';
const GALLERY_PAGES_CACHE = 'gtm-gallery-pages-v1';

const STATIC_ASSETS = [
    '/static/css/style.css',
    '/static/css/admin.css',
    '/static/js/app.js',
    '/static/js/admin.js',
    '/static/js/order.js',
    '/static/js/precache.js',
    '/static/js/status.js',
    '/static/js/gallery.js',
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

// Activate: clean up old caches - but never the product-pages cache,
// which is deliberately versioned separately (see note above CACHE_NAME).
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((key) => key !== CACHE_NAME && key !== PRODUCT_PAGES_CACHE && key !== PRODUCT_IMAGES_CACHE && key !== GALLERY_PAGES_CACHE)
                    .map((key) => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

// Fetch strategy:
// - Gallery pages (/gallery, /gallery/<category>) and product detail
//   pages (/product/<id>): cache-and-serve from their own persistent
//   buckets - public, session-independent, safe to serve stale-while-
//   revalidate style.
// - Other HTML page navigations (/login, /admin, /admin/products, etc.):
//   ALWAYS go to the network. Session/auth state changes per request,
//   so these must never be served from cache. ('/' is the one exception,
//   see isCatalogHome below.)
// - API calls (/api/*): network-first, fall back to cache if offline.
// - Static assets (css/js/manifest): cache-first, refreshed in the background.
// Normalizes a URL path so category names with characters like "&" always
// produce the same cache key, regardless of whether the path arrived with
// that character literal (a real browser navigation) or percent-encoded
// (precache.js's encodeURIComponent()) - see the BUG FIX note on the
// gallery block below for why this exists. Must produce the same output
// as an un-encoded category name run through encodeURIComponent() alone,
// since that's exactly what precache.js does when it builds a gallery URL
// straight from the raw category string with no prior encoding to undo.
function canonicalizeGalleryPath(pathname) {
    return pathname
        .split('/')
        .map((segment) => (segment ? encodeURIComponent(decodeURIComponent(segment)) : segment))
        .join('/');
}

self.addEventListener('fetch', (event) => {
    const { request } = event;

    if (request.method !== 'GET') {
        return;
    }

    const url = new URL(request.url);

    // Gallery pages (/gallery and /gallery/<category>): cache-and-serve
    // from GALLERY_PAGES_CACHE, same pattern as /product/ below and for
    // the same reason - public, session-independent, and precache.js
    // needs to be able to reach this with a plain fetch() (not
    // navigate-mode), so this is checked by path, before the
    // navigate-only branch further down would otherwise catch it.
    //
    // BUG FIX: category names can contain characters like "&" that are
    // valid, unescaped path characters as far as a real browser
    // navigation is concerned (only space gets auto-encoded to %20),
    // but precache.js's encodeURIComponent() escapes them (& -> %26).
    // Caching by the raw request.url meant the SAME category page ended
    // up under two different keys depending on which path created it -
    // a real click-through vs. precache.js's proactive download - so an
    // offline visit to a category that was only ever proactively
    // precached (never actually clicked while online) missed the cache
    // entirely. canonicalizeGalleryPath() normalizes both forms to one
    // string before it's ever used as a cache key, on both the write
    // and the read side, so it no longer matters which encoding a given
    // request happened to arrive with.
    if (url.pathname === '/gallery' || url.pathname.startsWith('/gallery/')) {
        const cacheKey = self.location.origin + canonicalizeGalleryPath(url.pathname) + url.search;
        event.respondWith(
            fetch(request)
                .then((response) => {
                    const clone = response.clone();
                    caches.open(GALLERY_PAGES_CACHE).then((cache) => cache.put(cacheKey, clone));
                    return response;
                })
                .catch(() => caches.match(cacheKey))
        );
        return;
    }

    // Product detail pages: cache-and-serve from PRODUCT_PAGES_CACHE.
    // Checked by PATH here, not request.mode - the offline precache
    // script (precache.js) downloads these with a plain fetch(), which
    // is NOT navigate-mode (that only applies to real browser
    // navigations), so a mode check alone would miss it and let it fall
    // through to the generic static-asset handler below, caching it in
    // the wrong (main, versioned) bucket instead.
    if (url.pathname.startsWith('/product/')) {
        event.respondWith(
            fetch(request)
                .then((response) => {
                    const clone = response.clone();
                    caches.open(PRODUCT_PAGES_CACHE).then((cache) => cache.put(request.url, clone));
                    return response;
                })
                .catch(() => caches.match(request.url))
        );
        return;
    }

    // Product images: cache-and-serve from PRODUCT_IMAGES_CACHE, not the
    // versioned CACHE_NAME - see the v21->v22 note above. Checked by path
    // for the same reason as /product/ below: precache.js downloads these
    // with a plain fetch(), not a navigation, so this has to run before
    // the generic static-asset handler ever sees the request.
    if (url.pathname.startsWith('/static/product-images/')) {
        event.respondWith(
            fetch(request)
                .then((response) => {
                    const clone = response.clone();
                    caches.open(PRODUCT_IMAGES_CACHE).then((cache) => cache.put(request.url, clone));
                    return response;
                })
                .catch(() => caches.match(request.url))
        );
        return;
    }

    // Navigation requests = full page loads. Session/auth state changes
    // per request, so admin/login pages must NEVER be served from cache -
    // that's the original v1->v2 fix, preserved below.
    //
    // The public catalog page ('/') is the one exception here: every
    // successful online visit re-caches it, so there's a real fallback to
    // serve when the device is genuinely offline.
    if (request.mode === 'navigate') {
        const isCatalogHome = url.pathname === '/';

        if (isCatalogHome) {
            event.respondWith(
                fetch(request)
                    .then((response) => {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put('/', clone));
                        return response;
                    })
                    .catch(() => caches.match('/'))
            );
            return;
        }

        // Admin/login/etc while offline: no cached fallback, by design -
        // better an honest network error than a stale authenticated page.
        event.respondWith(
            fetch(request).catch(() => caches.match(request))
        );
        return;
    }

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

// Lets a page ask "what version are you running" - used by the Updates
// & Status panel to display the live app version. Replies on the
// MessageChannel port the page sent along with the request, rather than
// a broadcast, so concurrent requests from multiple tabs don't cross
// wires with each other.
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'GET_VERSION' && event.ports && event.ports[0]) {
        event.ports[0].postMessage({ version: CACHE_NAME });
    }
});
