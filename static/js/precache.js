// =====================================================
// GTM CATALOG - Product & Gallery Page Precache (offline support)
// =====================================================
// Downloads every product's detail page, plus every Gallery page
// (/gallery and each /gallery/<category>), into dedicated cache buckets
// so they work offline, showing a visible progress indicator while
// doing so. Runs once per page load on the catalog page - skips
// anything already cached, so repeat visits only download what's
// actually new or missing (usually nothing, meaning no UI ever appears
// at all).
//
// IMPORTANT: PRODUCT_PAGES_CACHE, PRODUCT_IMAGES_CACHE, and
// GALLERY_PAGES_CACHE must exactly match the constants of the same
// names in sw.js. A page script and a service worker are separate
// files that can't share a JS import, so if you ever rename one,
// rename the other to match, or the two will silently stop agreeing on
// where product/gallery pages or images live.

const PRODUCT_PAGES_CACHE = 'gtm-product-pages-v1';
const PRODUCT_IMAGES_CACHE = 'gtm-product-images-v1';
const GALLERY_PAGES_CACHE = 'gtm-gallery-pages-v1';
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
    const galleryCache = await caches.open(GALLERY_PAGES_CACHE);

    // Figure out what's actually missing BEFORE showing any UI - a
    // repeat visit with everything already cached should be silent.
    // Checked in PARALLEL (Promise.all), not one at a time - a
    // sequential await-in-a-loop over 200+ products meant this ran on
    // EVERY page load (even when nothing was missing) with 200+ back-
    // to-back cache lookups before the "nothing to do" early-return
    // could even fire. Cache.match() is cheap per call, but 200+ of them
    // one after another still adds up to real time competing with
    // whatever else is happening on the page right after load.
    const pageChecks = await Promise.all(
        products.map(async (p) => {
            const slug = slugify(p['Product ID']);
            if (!slug) return null;
            const url = '/product/' + slug;
            const cached = await pagesCache.match(url);
            return cached ? null : { url, cache: pagesCache };
        })
    );
    const toFetch = pageChecks.filter(Boolean);

    if (Array.isArray(images)) {
        const imageChecks = await Promise.all(
            images.map(async (img) => {
                if (!img.path) return null;
                const url = '/static/' + img.path;
                const cached = await imagesCache.match(url);
                return cached ? null : { url, cache: imagesCache };
            })
        );
        toFetch.push(...imageChecks.filter(Boolean));
    }

    // Gallery pages: /gallery itself, plus one /gallery/<category> per
    // distinct category. Categories are derived from the products list
    // we already have in hand - no need for a separate endpoint, and it
    // guarantees the precached category set always matches what /gallery
    // itself would actually render. encodeURIComponent handles category
    // names with spaces/punctuation (e.g. "Food & Snacks") the same way
    // a real browser navigation would.
    const galleryUrls = ['/gallery'];
    const seenCategories = new Set();
    for (const p of products) {
        const cat = p['Category'];
        if (!cat || seenCategories.has(cat)) continue;
        seenCategories.add(cat);
        galleryUrls.push('/gallery/' + encodeURIComponent(cat));
    }

    const galleryChecks = await Promise.all(
        galleryUrls.map(async (url) => {
            const cached = await galleryCache.match(url);
            return cached ? null : { url, cache: galleryCache };
        })
    );
    toFetch.push(...galleryChecks.filter(Boolean));

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
