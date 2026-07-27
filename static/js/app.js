// =====================================================
// GTM PRODUCT & PRICE CATALOG
// (extracted from original inline <script> block)
// =====================================================

let currentCategory = 'ALL';
let currentStatus = 'In Stock';

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
}

function setCategory(cat, btn) {
    currentCategory = cat;
    document.querySelectorAll('.cat-pill').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    filterCatalog();
}

function setStatus(status, btn) {
    currentStatus = status;
    document.querySelectorAll('.status-pill').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    filterCatalog();
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

// Run initial filter on page load (no-ops safely if catalog isn't on this page)
filterCatalog();
