// =====================================================
// GTM CATALOG - Product Page Precache (offline support)
// =====================================================
// Downloads every product's detail page into a dedicated cache bucket so
// they work offline, showing a visible progress indicator while doing
// so. Runs once per page load on the catalog page - skips anything
// already cached, so repeat visits only download what's actually new or
// missing (usually nothing, meaning no UI ever appears at all).
//
// IMPORTANT: PRODUCT_PAGES_CACHE and PRODUCT_IMAGES_CACHE must exactly
// match the constants of the same names in sw.js. A page script and a
// service worker are separate files that can't share a JS import, so if
// you ever rename one, rename the other to match, or the two will
// silently stop agreeing on where product pages/images live.

const PRODUCT_PAGES_CACHE = 'gtm-product-pages-v1';
const PRODUCT_IMAGES_CACHE = 'gtm-product-images-v1';
const PRECACHE_CONCURRENCY = 5;

// Read by status.js's Updates & Status panel to show "Last Offline Sync".
// Written every time a check completes successfully - even a "nothing
// was missing" run - since it represents "last time we confirmed
// offline data is current," not "last time something new downloaded."
const LAST_SYNC_KEY = 'gtm_last_sync_v1';

function recordSyncTime() {
    try {
        localStorage.setItem(LAST_SYNC_KEY, String(Date.now()));
    } catch (e) {
        // storage unavailable - the status panel just shows "Never" instead
    }
}

function slugify(productId) {
    return (productId || '').replace(/\s+/g, '');
}

function createProgressBanner() {
    const banner = document.createElement('div');
    banner.id = 'precacheBanner';
    banner.className = 'precache-banner';
    banner.innerHTML =
        '<span class="precache-spinner"></span>' +
        '<span class="precache-text">Downloading offline data: <span id="precacheCount">0</span></span>';
    document.body.appendChild(banner);
    return banner;
}

function showBannerDone(banner) {
    banner.classList.add('precache-done');
    banner.innerHTML = '<i class="bi bi-check-circle-fill"></i> <span class="precache-text">Offline data ready</span>';
    setTimeout(() => {
        banner.classList.add('precache-fade');
        setTimeout(() => banner.remove(), 400);
    }, 1800);
}

async function runProductPrecache() {
    if (!('caches' in window) || !navigator.onLine) {
        return;
    }

    let products;
    try {
        const res = await fetch('/api/prices');
        products = await res.json();
    } catch (e) {
        return; // background enhancement only - fail silently, try again next load
    }

    if (!Array.isArray(products) || products.length === 0) {
        return;
    }

    // Product images (Gallery mode's cold-open case): sw.js only caches an
    // image AFTER someone views it online once, which doesn't help a rep
    // opening Gallery cold with no signal in a shop - so images need the
    // same proactive queueing as pages, from a dedicated endpoint that
    // only lists products with an actual resolvable file (no client-side
    // extension-guessing, no wasted 404 requests).
    let images = [];
    try {
        const imgRes = await fetch('/api/product-images');
        images = await imgRes.json();
    } catch (e) {
        // images just won't precache this run - pages still will, below
    }

    const pagesCache = await caches.open(PRODUCT_PAGES_CACHE);
    const imagesCache = await caches.open(PRODUCT_IMAGES_CACHE);

    // Figure out what's actually missing BEFORE showing any UI - a
    // repeat visit with everything already cached should be silent.
    const toFetch = [];
    for (const p of products) {
        const slug = slugify(p['Product ID']);
        if (!slug) continue;

        const url = '/product/' + slug;
        const cached = await pagesCache.match(url);
        if (!cached) {
            toFetch.push({ url, cache: pagesCache });
        }
    }

    if (Array.isArray(images)) {
        for (const img of images) {
            if (!img.path) continue;

            const url = '/static/' + img.path;
            const cached = await imagesCache.match(url);
            if (!cached) {
                toFetch.push({ url, cache: imagesCache });
            }
        }
    }

    if (toFetch.length === 0) {
        recordSyncTime();
        return;
    }

    const banner = createProgressBanner();
    const countEl = document.getElementById('precacheCount');
    let done = 0;

    function updateCount() {
        if (countEl) countEl.textContent = done + ' / ' + toFetch.length;
    }
    updateCount();

    // Bounded-concurrency worker pool: fast enough for 200+ pages/images
    // without opening an unreasonable number of simultaneous connections.
    let nextIndex = 0;

    async function worker() {
        while (nextIndex < toFetch.length) {
            const item = toFetch[nextIndex++];
            try {
                const response = await fetch(item.url);
                if (response.ok) {
                    await item.cache.put(item.url, response);
                }
            } catch (e) {
                // one item failing (e.g. a mid-download connection drop)
                // shouldn't stop the rest of the batch
            }
            done++;
            updateCount();
        }
    }

    const workers = [];
    for (let i = 0; i < PRECACHE_CONCURRENCY; i++) {
        workers.push(worker());
    }
    await Promise.all(workers);

    showBannerDone(banner);
    recordSyncTime();
}

if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        // Small delay so this background download never competes with
        // the page's own initial render/interaction - not urgent.
        setTimeout(runProductPrecache, 1500);
    });
}
