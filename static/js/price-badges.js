// =====================================================
// GTM CATALOG - price-change badge rendering
// =====================================================

function formatTimeAgo(ts) {
    const now = Date.now();
    const changed = Date.parse(ts.replace(' ', 'T') + 'Z');
    if (isNaN(changed)) return '';

    const seconds = Math.floor((now - changed) / 1000);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + (minutes === 1 ? ' minute' : ' minutes') + ' ago';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + (hours === 1 ? ' hour' : ' hours') + ' ago';
    const days = Math.floor(hours / 24);
    if (days < 7) return days + (days === 1 ? ' day' : ' days') + ' ago';
    const weeks = Math.floor(days / 7);
    return weeks + (weeks === 1 ? ' week' : ' weeks') + ' ago';
}

(function colorPriceChangeMarkers() {
    const badges = document.querySelectorAll('.price-change-badge');
    if (!badges.length) return;

    const MS_PER_DAY = 24 * 60 * 60 * 1000;
    const now = Date.now();

    badges.forEach((badge) => {
        const ts = badge.getAttribute('data-timestamp');
        if (!ts) return;

        const changed = Date.parse(ts.replace(' ', 'T') + 'Z');
        if (isNaN(changed)) return;

        const ageDays = (now - changed) / MS_PER_DAY;

        if (ageDays < 1) {
            badge.classList.add('price-change-badge--today');
        } else if (ageDays < 7) {
            badge.classList.add('price-change-badge--recent');
        } else {
            badge.classList.add('price-change-badge--older');
        }

        const direction = badge.getAttribute('data-direction') || 'none';
        const textEl = badge.querySelector('.price-change-text');
        if (textEl) {
            const timeAgo = formatTimeAgo(ts);
            const action = direction === 'up' ? 'increased' :
                direction === 'down' ? 'decreased' : 'changed';
            const arrow = direction === 'up' ? '↗' : direction === 'down' ? '↘' : '';
            const arrowClass = direction === 'up' ? 'price-change-arrow-up' :
                direction === 'down' ? 'price-change-arrow-down' : '';
            textEl.textContent = 'Price ' + action + ' ' + timeAgo;
            if (arrow) {
                const arrowEl = document.createElement('span');
                arrowEl.className = arrowClass;
                arrowEl.setAttribute('aria-hidden', 'true');
                arrowEl.textContent = ' ' + arrow;
                textEl.appendChild(arrowEl);
            }
        }
    });
})();

window.formatTimeAgo = formatTimeAgo;
window.colorPriceChangeMarkers = colorPriceChangeMarkers;
