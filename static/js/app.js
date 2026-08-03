// =====================================================
// GTM PRODUCT & PRICE CATALOG
// (extracted from original inline <script> block)
// =====================================================

let currentCategory = 'ALL';
let currentStatus = 'ALL';

// Remembers search/sort/filter/scroll state across a trip to the product
// detail page and back, so "Back to Catalog" restores where you were
// instead of resetting to a fresh top-of-page catalog. sessionStorage
// (not localStorage) is intentional - this is "where was I this
// session," not a permanent preference, and it clears itself when the
// tab/app is fully closed.
const CATALOG_STATE_KEY = 'gtm_catalog_state_v1';

function saveCatalogState() {
    const searchBox = document.getElementById('searchBox');
    const sortSelect = document.getElementById('sortSelect');
    const grid = document.getElementById('catalogGrid');
    if (!searchBox || !sortSelect || !grid) return; // not on the catalog page

    try {
        sessionStorage.setItem(CATALOG_STATE_KEY, JSON.stringify({
            search: searchBox.value,
            sort: sortSelect.value,
            category: currentCategory,
            status: currentStatus,
            scrollY: window.scrollY
        }));
    } catch (e) {
        // sessionStorage unavailable (e.g. private browsing) - state just
        // won't persist, same as today's behavior
    }
}

function restoreCatalogState() {
    const searchBox = document.getElementById('searchBox');
    const sortSelect = document.getElementById('sortSelect');
    const grid = document.getElementById('catalogGrid');
    if (!searchBox || !sortSelect || !grid) return false;

    let saved;
    try {
        saved = JSON.parse(sessionStorage.getItem(CATALOG_STATE_KEY));
    } catch (e) {
        return false;
    }
    if (!saved) return false;

    searchBox.value = saved.search || '';
    sortSelect.value = saved.sort || 'default';
    currentCategory = saved.category || 'ALL';
    currentStatus = saved.status || 'ALL';

    // filterCatalog() only filters/sorts the cards - it doesn't touch pill
    // highlighting, so re-sync that separately from the restored state.
    document.querySelectorAll('.cat-pill').forEach(p => p.classList.remove('active'));
    const catBtn = Array.from(document.querySelectorAll('.cat-pill'))
        .find(b => b.getAttribute('onclick') === "setCategory('" + currentCategory + "', this)");
    (catBtn || document.querySelector('.cat-all')).classList.add('active');

    document.querySelectorAll('.status-pill').forEach(p => p.classList.remove('active'));
    const statusBtn = Array.from(document.querySelectorAll('.status-pill'))
        .find(b => b.getAttribute('onclick') === "setStatus('" + currentStatus + "', this)");
    if (statusBtn) statusBtn.classList.add('active');

    filterCatalog();

    if (typeof saved.scrollY === 'number') {
        // Wait a frame so the grid has finished re-populating before
        // jumping - scrolling immediately can land short if the browser
        // hasn't reflowed the filtered/sorted layout yet.
        requestAnimationFrame(() => window.scrollTo(0, saved.scrollY));
    }

    return true;
}

function filterCatalog() {
    let searchBox = document.getElementById('searchBox');
    let sortSelect = document.getElementById('sortSelect');
    let grid = document.getElementById('catalogGrid');

    // Guard: these elements only exist on the catalog page (index.html).
    // app.js is loaded on every page via base.html, so bail out quietly
    // on login/admin pages instead of throwing.
    if (!searchBox || !sortSelect || !grid) {
        return;
    }

    let query = searchBox.value.toLowerCase().trim();
    let sort = sortSelect.value;
    let cards = Array.from(document.getElementsByClassName('product-card'));

    let visibleCards = cards.filter(card => {
        let nameData = card.getAttribute('data-name') || '';
        let catData = card.getAttribute('data-cat') || '';
        let statusData = card.getAttribute('data-status') || '';

        let matchesSearch = !query || nameData.includes(query);
        let matchesCategory = (currentCategory === 'ALL' || catData === currentCategory);
        let matchesStatus = (currentStatus === 'ALL' || statusData.toLowerCase() === currentStatus.toLowerCase());

        return matchesSearch && matchesCategory && matchesStatus;
    });

    if (sort === 'price_asc') {
        visibleCards.sort((a, b) => (parseFloat(a.getAttribute('data-retail')) || 0) - (parseFloat(b.getAttribute('data-retail')) || 0));
    } else if (sort === 'price_desc') {
        visibleCards.sort((a, b) => (parseFloat(b.getAttribute('data-retail')) || 0) - (parseFloat(a.getAttribute('data-retail')) || 0));
    } else if (sort === 'name_asc') {
        visibleCards.sort((a, b) => a.getAttribute('data-fullname').localeCompare(b.getAttribute('data-fullname')));
    }

    cards.forEach(c => c.style.display = 'none');

    visibleCards.forEach(card => {
        card.style.display = '';
        grid.appendChild(card);
    });

    saveCatalogState();
}

function setCategory(cat, btn) {
    currentCategory = cat;
    document.querySelectorAll('.cat-pill').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    filterCatalog(); // also saves state - see saveCatalogState() call at its end
}

function setStatus(status, btn) {
    currentStatus = status;
    document.querySelectorAll('.status-pill').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    filterCatalog(); // also saves state - see saveCatalogState() call at its end
}

function copyPrice(btn) {
    let card = btn.closest('.card');
    let name = card.getAttribute('data-fullname');
    let unit = card.getAttribute('data-unit');
    let retail = card.getAttribute('data-retail');
    let wholesale = card.getAttribute('data-wholesale');

    let text = name + (unit && unit !== '-' ? ' (' + unit + ')' : '') + '\n' +
               'Retail: ' + retail + ' Ks\n' +
               'Wholesale: ' + wholesale + ' Ks';

    navigator.clipboard.writeText(text).then(() => {
        btn.classList.add('copied');
        btn.textContent = 'Copied!';
        setTimeout(() => {
            btn.classList.remove('copied');
            btn.textContent = 'Copy Price';
        }, 2000);
    });
}

// On load: restore whatever search/filter/sort/scroll state was saved
// right before navigating away (e.g. tapping the eye icon to a product
// detail page), so "Back to Catalog" lands back where you were instead
// of a fresh top-of-page catalog. Falls back to a normal fresh filter
// when there's nothing saved (first visit this session, cleared, etc.).
// filterCatalog() no-ops safely if this isn't the catalog page either way.
if (!restoreCatalogState()) {
    filterCatalog();
}

// scrollY changes constantly but isn't tied to any filterCatalog() call,
// so it needs its own capture point. pagehide fires right as the page is
// being navigated away from (including into bfcache) - the last safe
// moment to snapshot it. visibilitychange is a backup for cases where
// pagehide doesn't fire reliably (some mobile/PWA scenarios).
window.addEventListener('pagehide', saveCatalogState);
document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
        saveCatalogState();
    }
});
