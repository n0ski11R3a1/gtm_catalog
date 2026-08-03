// =====================================================
// GTM CATALOG - Gallery mode (browsing-only List/Gallery toggle)
// =====================================================
// Add-to-order is explicitly out of scope here - this file never touches
// order.js, the cart, or /order/submit. Gallery is browsing-only.

const GALLERY_MODE_KEY = 'gtm_gallery_mode_v1';

// Called from the tab bar's onclick (see _glass_tabbar.html) - exposed on
// window since it's invoked from an inline handler, same pattern as the
// other on* attributes already in this codebase (openOrderPanel, etc.).
function setGalleryModePreference(mode) {
    try {
        localStorage.setItem(GALLERY_MODE_KEY, mode);
    } catch (e) {
        // storage unavailable - preference just won't persist, same as
        // today's behavior for any other localStorage write in this app
    }
}
window.setGalleryModePreference = setGalleryModePreference;

// --------------------------------------
// Mode-preference redirect (runs only on '/')
// --------------------------------------
// The installed PWA always launches at '/' (manifest start_url), so a rep
// who prefers Gallery needs a client-side redirect on that fixed entry
// point - there's no way to change start_url based on a stored
// preference. Guarded on #catalogGrid (List-only markup) so this never
// fires on any other page. location.replace(), not href assignment - a
// preference redirect shouldn't leave '/' as a back-button stop.
(function () {
    if (!document.getElementById('catalogGrid')) return;

    let preferred;
    try {
        preferred = localStorage.getItem(GALLERY_MODE_KEY);
    } catch (e) {
        return;
    }

    if (preferred === 'gallery') {
        window.location.replace('/gallery');
    }
})();

// --------------------------------------
// Level 1: category-card search (gallery.html topbar)
// --------------------------------------
function filterGalleryCategories() {
    const searchBox = document.getElementById('galleryCategorySearch');
    const grid = document.getElementById('galleryCategoryGrid');
    const empty = document.getElementById('galleryCategoryEmpty');
    if (!searchBox || !grid) return; // not on the Level 1 page

    const query = searchBox.value.toLowerCase().trim();
    let visibleCount = 0;

    grid.querySelectorAll('.gallery-category-card').forEach((card) => {
        const nameData = card.getAttribute('data-name') || '';
        const matches = !query || nameData.includes(query);
        card.style.display = matches ? '' : 'none';
        if (matches) visibleCount += 1;
    });

    if (empty) empty.style.display = visibleCount === 0 ? 'flex' : 'none';
}

// --------------------------------------
// Level 2: thumbnail grid - search + status filter (gallery_category.html
// topbar). Reuses the exact sessionStorage key app.js already writes
// (gtm_catalog_state_v1) so switching between List and Gallery mid-search
// keeps the same search text / status filter - and now also writes back
// to that same key so a change made here carries back to List too.
// Category is already fixed by the route; sort has no meaning in a photo
// grid, so only search + status apply here.
let galleryCurrentStatus = 'ALL';

function toggleGalleryFilters() {
    const panel = document.getElementById('galleryFilterPanel');
    const btn = document.querySelector('#galleryTopbar .filter-toggle-btn');
    if (!panel || !btn) return;

    const isOpen = panel.classList.toggle('open');
    btn.classList.toggle('active', isOpen);
    btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
}

function saveGalleryThumbState() {
    const searchBox = document.getElementById('galleryThumbSearch');
    if (!searchBox) return;

    let saved;
    try {
        saved = JSON.parse(sessionStorage.getItem('gtm_catalog_state_v1')) || {};
    } catch (e) {
        saved = {};
    }

    saved.search = searchBox.value;
    saved.status = galleryCurrentStatus;

    try {
        sessionStorage.setItem('gtm_catalog_state_v1', JSON.stringify(saved));
    } catch (e) {
        // sessionStorage unavailable - state just won't persist
    }
}

function filterGalleryThumbs() {
    const searchBox = document.getElementById('galleryThumbSearch');
    const grid = document.getElementById('galleryThumbGrid');
    const empty = document.getElementById('galleryThumbEmpty');
    if (!searchBox || !grid) return; // not on the Level 2 page

    const query = searchBox.value.toLowerCase().trim();
    let visibleCount = 0;

    grid.querySelectorAll('.gallery-thumb-tile').forEach((tile) => {
        const nameData = tile.getAttribute('data-name') || '';
        const statusData = tile.getAttribute('data-status') || '';

        const matchesSearch = !query || nameData.includes(query);
        const matchesStatus = (galleryCurrentStatus === 'ALL' || statusData.toLowerCase() === galleryCurrentStatus.toLowerCase());
        const visible = matchesSearch && matchesStatus;

        tile.style.display = visible ? '' : 'none';
        if (visible) visibleCount += 1;
    });

    if (empty) empty.style.display = visibleCount === 0 ? 'flex' : 'none';

    saveGalleryThumbState();
}

function setGalleryStatus(status, btn) {
    galleryCurrentStatus = status;
    document.querySelectorAll('#galleryFilterPanel .status-pill').forEach((p) => p.classList.remove('active'));
    if (btn) btn.classList.add('active');
    filterGalleryThumbs(); // also saves state - see saveGalleryThumbState() call at its end
}

(function () {
    const grid = document.getElementById('galleryThumbGrid');
    const searchBox = document.getElementById('galleryThumbSearch');
    if (!grid || !searchBox) return; // not on the Level 2 page

    let saved;
    try {
        saved = JSON.parse(sessionStorage.getItem('gtm_catalog_state_v1'));
    } catch (e) {
        saved = null;
    }

    searchBox.value = (saved && saved.search) ? saved.search : '';
    galleryCurrentStatus = (saved && saved.status) ? saved.status : 'ALL';

    document.querySelectorAll('#galleryFilterPanel .status-pill').forEach((p) => p.classList.remove('active'));
    const statusBtn = Array.from(document.querySelectorAll('#galleryFilterPanel .status-pill'))
        .find((b) => b.getAttribute('onclick') === "setGalleryStatus('" + galleryCurrentStatus + "', this)");
    (statusBtn || document.querySelector('#galleryFilterPanel .status-pill:last-child')).classList.add('active');

    filterGalleryThumbs();
})();

// --------------------------------------
// Level 3: full-screen swipe viewer
// --------------------------------------
(function () {
    const grid = document.getElementById('galleryThumbGrid');
    const dataEl = document.getElementById('galleryProductsData');
    const viewer = document.getElementById('galleryViewer');
    if (!grid || !dataEl || !viewer) return; // not on the Level 2 page

    let products = [];
    try {
        products = JSON.parse(dataEl.textContent) || [];
    } catch (e) {
        products = [];
    }

    const track = document.getElementById('galleryViewerTrack');
    const closeBtn = document.getElementById('galleryViewerClose');
    const prevBtn = document.getElementById('galleryViewerPrev');
    const nextBtn = document.getElementById('galleryViewerNext');

    let visibleList = [];   // products currently shown in the grid, in order
    let currentSlide = 0;
    let viewerOpenViaHistory = false;

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str === null || str === undefined ? '' : String(str);
        return div.innerHTML;
    }

    function formatKs(value) {
        return value > 0 ? Number(value).toLocaleString() + ' Ks' : '-';
    }

    function buildVisibleList() {
        const tiles = Array.from(grid.querySelectorAll('.gallery-thumb-tile'));
        visibleList = tiles
            .filter((t) => t.style.display !== 'none')
            .map((t) => products[parseInt(t.getAttribute('data-index'), 10)])
            .filter(Boolean);
    }

    function renderTrack() {
        track.innerHTML = visibleList.map((p) => {
            const image = p.image_path
                ? '<img src="/static/' + p.image_path + '" alt="">'
                : '<div class="gallery-viewer-slide-placeholder"><i class="bi bi-image"></i></div>';

            const isOos = (p['Status'] || '').toLowerCase() === 'out of stock';
            const statusBadge = '<span class="gallery-viewer-slide-status ' + (isOos ? 'is-oos' : 'is-instock') + '">' +
                '<span class="gallery-viewer-slide-status-dot"></span>' + escapeHtml(p['Status']) +
                '</span>';

            return (
                '<div class="gallery-viewer-slide">' +
                    '<div class="gallery-viewer-slide-media">' + image + '</div>' +
                    '<div class="gallery-viewer-slide-info">' +
                        '<div class="gallery-viewer-slide-id">' + escapeHtml(p['Product ID']) + '</div>' +
                        '<div class="gallery-viewer-slide-name">' + escapeHtml(p['Product Name']) + '</div>' +
                        '<div class="gallery-viewer-slide-unit">' + escapeHtml(p['Unit']) + '</div>' +
                        '<div class="gallery-viewer-slide-prices">' +
                            '<span>Retail: ' + formatKs(p['Retail']) + '</span>' +
                            '<span>Wholesale: ' + formatKs(p['Wholesale']) + '</span>' +
                        '</div>' +
                        statusBadge +
                    '</div>' +
                '</div>'
            );
        }).join('');

        goToSlide(currentSlide, false);
    }

    function goToSlide(index, animate) {
        if (index < 0 || index >= visibleList.length) return;
        currentSlide = index;
        track.style.transition = animate === false ? 'none' : '';
        track.style.transform = 'translateX(-' + (currentSlide * 100) + '%)';

        if (prevBtn) prevBtn.style.visibility = currentSlide === 0 ? 'hidden' : '';
        if (nextBtn) nextBtn.style.visibility = currentSlide === visibleList.length - 1 ? 'hidden' : '';
    }

    function openViewer(startIndex) {
        buildVisibleList();
        if (visibleList.length === 0) return;

        const startProduct = products[startIndex];
        let startSlide = visibleList.indexOf(startProduct);
        if (startSlide === -1) startSlide = 0;

        renderTrack();
        goToSlide(startSlide, false);

        viewer.classList.add('open');
        viewer.setAttribute('aria-hidden', 'false');

        // Pushes a history entry so the device back gesture/button closes
        // just the viewer, not the whole gallery page. No sessionStorage
        // scroll dance needed here - the grid underneath never navigates,
        // so its scroll position is simply never disturbed.
        history.pushState({ galleryViewer: true }, '');
        viewerOpenViaHistory = true;
    }

    function closeViewer(fromPopstate) {
        viewer.classList.remove('open');
        viewer.setAttribute('aria-hidden', 'true');
        track.innerHTML = '';

        if (viewerOpenViaHistory && !fromPopstate) {
            history.back();
        }
        viewerOpenViaHistory = false;
    }

    grid.querySelectorAll('.gallery-thumb-tile').forEach((tile) => {
        tile.addEventListener('click', () => {
            openViewer(parseInt(tile.getAttribute('data-index'), 10));
        });
    });

    if (closeBtn) closeBtn.addEventListener('click', () => closeViewer(false));
    if (prevBtn) prevBtn.addEventListener('click', () => goToSlide(currentSlide - 1));
    if (nextBtn) nextBtn.addEventListener('click', () => goToSlide(currentSlide + 1));

    window.addEventListener('popstate', () => {
        if (viewer.classList.contains('open')) {
            closeViewer(true);
        }
    });

    // Swipe gesture: reps exclusively open this app via "Add to Home
    // Screen" (confirmed) - standalone display mode has no browser chrome
    // and therefore no edge-swipe-back gesture to conflict with, so no
    // defensive swipe-zone logic is needed here.
    let touchStartX = 0;
    let touchDeltaX = 0;
    let touching = false;

    track.addEventListener('touchstart', (e) => {
        touching = true;
        touchStartX = e.touches[0].clientX;
        touchDeltaX = 0;
        track.style.transition = 'none';
    }, { passive: true });

    track.addEventListener('touchmove', (e) => {
        if (!touching) return;
        touchDeltaX = e.touches[0].clientX - touchStartX;
        const basePercent = -(currentSlide * 100);
        const dragPercent = (touchDeltaX / track.clientWidth) * 100;
        track.style.transform = 'translateX(' + (basePercent + dragPercent) + '%)';
    }, { passive: true });

    track.addEventListener('touchend', () => {
        if (!touching) return;
        touching = false;
        track.style.transition = '';

        const threshold = track.clientWidth * 0.2;
        if (touchDeltaX < -threshold && currentSlide < visibleList.length - 1) {
            goToSlide(currentSlide + 1);
        } else if (touchDeltaX > threshold && currentSlide > 0) {
            goToSlide(currentSlide - 1);
        } else {
            goToSlide(currentSlide); // snap back
        }
        touchDeltaX = 0;
    });
})();
