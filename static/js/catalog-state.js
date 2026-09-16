// =====================================================
// GTM CATALOG - persisted list state
// =====================================================

const CATALOG_STATE_KEY = 'gtm_catalog_state_v1';

function saveCatalogState() {
    const searchBox = document.getElementById('searchBox');
    const sortSelect = document.getElementById('sortSelect');
    const grid = document.getElementById('catalogGrid');
    if (!searchBox || !sortSelect || !grid) return;

    try {
        sessionStorage.setItem(CATALOG_STATE_KEY, JSON.stringify({
            search: searchBox.value,
            sort: sortSelect.value,
            category: currentCategory,
            status: currentStatus,
            scrollY: window.scrollY
        }));
    } catch (e) {
        // sessionStorage unavailable - state just won't persist in that browser
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
    currentStatus = saved.status || 'In Stock';

    document.querySelectorAll('.cat-pill').forEach((p) => p.classList.remove('active'));
    const catBtn = Array.from(document.querySelectorAll('.cat-pill'))
        .find((b) => b.getAttribute('data-category') === currentCategory);
    if (catBtn) catBtn.classList.add('active');
    else if (document.querySelector('.cat-all')) document.querySelector('.cat-all').classList.add('active');

    document.querySelectorAll('.status-pill').forEach((p) => p.classList.remove('active'));
    const statusBtn = Array.from(document.querySelectorAll('.status-pill'))
        .find((b) => b.getAttribute('data-status') === currentStatus);
    if (statusBtn) statusBtn.classList.add('active');

    filterCatalog();

    if (typeof saved.scrollY === 'number') {
        requestAnimationFrame(() => window.scrollTo(0, saved.scrollY));
    }

    return true;
}

window.saveCatalogState = saveCatalogState;
window.restoreCatalogState = restoreCatalogState;
