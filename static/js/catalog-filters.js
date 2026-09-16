// =====================================================
// GTM CATALOG - filtering + catalog state control
// =====================================================

let currentCategory = 'ALL';
let currentStatus = 'In Stock';

function filterCatalog() {
    const searchBox = document.getElementById('searchBox');
    const sortSelect = document.getElementById('sortSelect');
    const grid = document.getElementById('catalogGrid');

    if (!searchBox || !sortSelect || !grid) return;

    const query = searchBox.value.toLowerCase().trim();
    const sort = sortSelect.value;
    const cards = Array.from(document.getElementsByClassName('product-card'));

    const visibleCards = cards.filter((card) => {
        const nameData = (card.getAttribute('data-name') || '').toLowerCase();
        const catData = card.getAttribute('data-cat') || '';
        const statusData = (card.getAttribute('data-status') || '').toLowerCase();

        const matchesSearch = !query || nameData.includes(query);
        const matchesCategory = (currentCategory === 'ALL' || catData === currentCategory);
        const matchesStatus = (currentStatus === 'ALL' || statusData === currentStatus.toLowerCase());

        return matchesSearch && matchesCategory && matchesStatus;
    });

    if (sort === 'recent') {
        visibleCards.sort((a, b) => {
            const ta = a.getAttribute('data-last-change') || '';
            const tb = b.getAttribute('data-last-change') || '';
            if (!ta && !tb) return 0;
            if (!ta) return 1;
            if (!tb) return -1;
            return tb.localeCompare(ta);
        });
    } else if (sort === 'price_asc') {
        visibleCards.sort((a, b) => (parseFloat(a.getAttribute('data-retail')) || 0) - (parseFloat(b.getAttribute('data-retail')) || 0));
    } else if (sort === 'price_desc') {
        visibleCards.sort((a, b) => (parseFloat(b.getAttribute('data-retail')) || 0) - (parseFloat(a.getAttribute('data-retail')) || 0));
    } else if (sort === 'name_asc') {
        visibleCards.sort((a, b) => (a.getAttribute('data-fullname') || '').localeCompare(b.getAttribute('data-fullname') || ''));
    }

    cards.forEach((card) => {
        card.style.display = 'none';
    });

    visibleCards.forEach((card) => {
        card.style.display = '';
        grid.appendChild(card);
    });

    saveCatalogState();
}

function setCategory(cat, btn) {
    currentCategory = cat;
    document.querySelectorAll('.cat-pill').forEach((p) => p.classList.remove('active'));
    if (btn) btn.classList.add('active');
    filterCatalog();
}

function setStatus(status, btn) {
    currentStatus = status;
    document.querySelectorAll('.status-pill').forEach((p) => p.classList.remove('active'));
    if (btn) btn.classList.add('active');
    filterCatalog();
}

function copyPrice(btn) {
    if (!btn) return;
    const card = btn.closest('.card');
    if (!card) return;

    const name = card.getAttribute('data-fullname') || '';
    const unit = card.getAttribute('data-unit') || '';
    const retail = card.getAttribute('data-retail') || '';
    const wholesale = card.getAttribute('data-wholesale') || '';

    const text = name + (unit && unit !== '-' ? ' (' + unit + ')' : '') + '\n' +
        'Retail: ' + retail + ' Ks\n' +
        'Wholesale: ' + wholesale + ' Ks';

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            btn.classList.add('copied');
            btn.textContent = 'Copied!';
            setTimeout(() => {
                btn.classList.remove('copied');
                btn.textContent = 'Copy Price';
            }, 2000);
        });
    }
}

function bindCatalogInteractions() {
    document.querySelectorAll('.cat-pill').forEach((btn) => {
        btn.addEventListener('click', () => setCategory(btn.getAttribute('data-category') || 'ALL', btn));
    });

    document.querySelectorAll('.status-pill').forEach((btn) => {
        btn.addEventListener('click', () => setStatus(btn.getAttribute('data-status') || 'ALL', btn));
    });

    const filterToggleBtn = document.getElementById('filterToggleBtn');
    if (filterToggleBtn) {
        filterToggleBtn.addEventListener('click', toggleFilters);
    }

    const statusPanelBtn = document.getElementById('statusPanelBtn');
    if (statusPanelBtn) {
        statusPanelBtn.addEventListener('click', openStatusPanel);
    }

    const hardRefreshBtn = document.getElementById('hardRefreshBtn');
    if (hardRefreshBtn) {
        hardRefreshBtn.addEventListener('click', hardRefresh);
    }

    document.querySelectorAll('.copy-btn').forEach((btn) => {
        btn.addEventListener('click', () => copyPrice(btn));
    });

    const desktopOrderOpenBtn = document.getElementById('desktopOrderOpenBtn');
    if (desktopOrderOpenBtn) {
        desktopOrderOpenBtn.addEventListener('click', openOrderPanel);
    }

    const desktopOrderReviewBtn = document.getElementById('desktopOrderReviewBtn');
    if (desktopOrderReviewBtn) {
        desktopOrderReviewBtn.addEventListener('click', openOrderPanel);
    }

    document.querySelectorAll('.qty-btn-dec').forEach((btn) => {
        btn.addEventListener('click', () => adjustQtyInput(btn, -1));
    });

    document.querySelectorAll('.qty-btn-inc').forEach((btn) => {
        btn.addEventListener('click', () => adjustQtyInput(btn, 1));
    });

    document.querySelectorAll('.add-order-btn').forEach((btn) => {
        btn.addEventListener('click', () => addToOrder(btn));
    });
}

(function () {
    function initializeCatalogUi() {
        if (!document.getElementById('catalogGrid')) return;
        if (!restoreCatalogState()) {
            filterCatalog();
        }
        bindCatalogInteractions();

        window.addEventListener('pagehide', saveCatalogState);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                saveCatalogState();
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeCatalogUi, { once: true });
    } else {
        initializeCatalogUi();
    }
})();

window.setCategory = setCategory;
window.setStatus = setStatus;
window.filterCatalog = filterCatalog;
window.copyPrice = copyPrice;
