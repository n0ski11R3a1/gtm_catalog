// =====================================================
// GTM CATALOG - Updates & Status (version, activity feed, hard refresh)
// =====================================================

// Same key precache.js writes to - read-only from this file.
const STATUS_LAST_SYNC_KEY = 'gtm_last_sync_v1';

// Tracks the highest activity_log id the person has actually seen (by
// opening the panel), so the red dot only shows for things that
// happened since their last look - client-side only, no login/per-user
// concept exists for the public catalog.
const LAST_SEEN_ACTIVITY_KEY = 'gtm_last_seen_activity_id_v1';

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str === null || str === undefined ? '' : String(str);
    return div.innerHTML;
}

// SQLite's CURRENT_TIMESTAMP is UTC but has no timezone marker
// ('2026-07-28 14:15:00') - browsers can't be trusted to guess that's
// UTC and not local time. Converting to real ISO 8601 first
// ('2026-07-28T14:15:00Z') makes every browser parse it the same way.
function parseSqliteUtc(sqliteString) {
    if (!sqliteString) return null;
    const iso = sqliteString.replace(' ', 'T') + 'Z';
    const date = new Date(iso);
    return isNaN(date.getTime()) ? null : date;
}

function formatRelativeTime(date) {
    if (!date) return 'unknown';

    const diffMs = Date.now() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);

    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return diffMin + 'm ago';

    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return diffHr + 'h ago';

    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 7) return diffDay + 'd ago';

    return date.toLocaleDateString();
}

// --------------------------------------
// Service worker version
// --------------------------------------

function getSwVersion() {
    return new Promise((resolve) => {
        if (!('serviceWorker' in navigator) || !navigator.serviceWorker.controller) {
            resolve(null);
            return;
        }

        const channel = new MessageChannel();
        const timeout = setTimeout(() => resolve(null), 1500);

        channel.port1.onmessage = (event) => {
            clearTimeout(timeout);
            resolve(event.data && event.data.version ? event.data.version : null);
        };

        navigator.serviceWorker.controller.postMessage({ type: 'GET_VERSION' }, [channel.port2]);
    });
}

// --------------------------------------
// Bell dot (unread indicator)
// --------------------------------------

function showBellDot() {
    const dot = document.getElementById('bellDot');
    if (dot) dot.style.display = '';
}

function hideBellDot() {
    const dot = document.getElementById('bellDot');
    if (dot) dot.style.display = 'none';
}

async function checkForUnreadActivity() {
    try {
        const res = await fetch('/api/activity?limit=1');
        const data = await res.json();
        const lastSeen = parseInt(localStorage.getItem(LAST_SEEN_ACTIVITY_KEY) || '0', 10);

        if (data.latest_id && data.latest_id > lastSeen) {
            showBellDot();
        }
    } catch (e) {
        // offline or request failed - leave the dot's current state alone
    }
}

// --------------------------------------
// Activity feed rendering
// --------------------------------------

function activityIconFor(eventType) {
    switch (eventType) {
        case 'product_added':
            return 'bi-plus-circle-fill';
        case 'back_in_stock':
            return 'bi-check-circle-fill';
        case 'out_of_stock':
            return 'bi-x-circle-fill';
        case 'price_changed':
        default:
            return 'bi-graph-up-arrow';
    }
}

// Matches the exact normalization app.py uses server-side (find_product_image
// and the /product/<slug> lookup): strip spaces from the business product_id
// ("GTM - 0042" -> "GTM-0042"). Keeping this in one place here means the
// notification bell always builds the same URL the catalog's eye icon does.
function slugifyProductId(productId) {
    return (productId || '').replace(/\s+/g, '');
}

function renderActivityList(events) {
    const listEl = document.getElementById('activityList');
    const emptyEl = document.getElementById('activityEmpty');
    if (!listEl) return;

    listEl.innerHTML = '';

    if (!events.length) {
        if (emptyEl) emptyEl.style.display = '';
        return;
    }

    if (emptyEl) emptyEl.style.display = 'none';

    events.forEach((ev) => {
        const row = document.createElement('div');
        row.className = 'activity-row';

        const when = formatRelativeTime(parseSqliteUtc(ev.created_at));
        const slug = slugifyProductId(ev.product_id);

        row.innerHTML =
            '<i class="bi ' + activityIconFor(ev.event_type) + ' activity-icon activity-icon-' + escapeHtml(ev.event_type) + '"></i>' +
            '<div class="activity-text">' +
                '<div class="activity-title">' + escapeHtml(ev.product_name) + '</div>' +
                '<div class="activity-meta">' + escapeHtml(ev.details) + '</div>' +
            '</div>' +
            '<div class="activity-time">' + escapeHtml(when) + '</div>' +
            (slug ? '<i class="bi bi-chevron-right activity-chevron" aria-hidden="true"></i>' : '');

        // Only wire up navigation when there's actually a product_id to go
        // to - a row without one (shouldn't happen per the current schema,
        // but cheap to guard) just stays a plain read-only row instead of
        // silently linking to "/product/".
        if (slug) {
            row.classList.add('activity-row-clickable');
            row.style.cursor = 'pointer';
            row.setAttribute('role', 'button');
            row.setAttribute('tabindex', '0');
            row.setAttribute('aria-label', 'View ' + ev.product_name);

            const goToProduct = () => {
                // Close the modal first so the click doesn't feel like it
                // "hung" while the browser navigates.
                const modalEl = document.getElementById('statusModal');
                if (modalEl && typeof bootstrap !== 'undefined') {
                    const modal = bootstrap.Modal.getInstance(modalEl);
                    if (modal) modal.hide();
                }
                window.location.href = '/product/' + encodeURIComponent(slug);
            };

            row.addEventListener('click', goToProduct);
            row.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    goToProduct();
                }
            });
        }

        listEl.appendChild(row);
    });
}

// --------------------------------------
// Panel open: refresh everything, mark as read
// --------------------------------------

async function updateStatusPanelContent() {
    const versionEl = document.getElementById('statusVersion');
    const syncEl = document.getElementById('statusLastSync');

    getSwVersion().then((version) => {
        if (versionEl) versionEl.textContent = version || 'Unknown';
    });

    if (syncEl) {
        const stored = localStorage.getItem(STATUS_LAST_SYNC_KEY);
        syncEl.textContent = stored ? formatRelativeTime(new Date(parseInt(stored, 10))) : 'Never';
    }

    try {
        const res = await fetch('/api/activity?limit=30');
        const data = await res.json();

        renderActivityList(data.events || []);

        if (data.latest_id) {
            localStorage.setItem(LAST_SEEN_ACTIVITY_KEY, String(data.latest_id));
        }
        hideBellDot();
    } catch (e) {
        // offline - leave whatever was rendered before, fail quietly
    }
}

function openStatusPanel() {
    updateStatusPanelContent();

    const modalEl = document.getElementById('statusModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
}

// --------------------------------------
// Hard refresh
// --------------------------------------

async function hardRefresh() {
    const btn = document.getElementById('hardRefreshBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Refreshing...';
    }

    try {
        if ('serviceWorker' in navigator) {
            const registration = await navigator.serviceWorker.getRegistration();
            if (registration) {
                await registration.update(); // force a real check for a new sw.js right now
            }
        }

        // ALSO explicitly clear the main static-asset cache bucket
        // directly, covering the case where sw.js's own CACHE_NAME
        // wasn't bumped but CSS/JS changed anyway (has happened more
        // than once in this project's history). Deliberately does NOT
        // touch gtm-product-pages-v1 - that's downloaded catalog data,
        // not app code, and wiping it here would force re-downloading
        // 200+ pages just to pick up a style tweak.
        if ('caches' in window) {
            const keys = await caches.keys();
            await Promise.all(
                keys
                    .filter((key) => key !== 'gtm-product-pages-v1')
                    .map((key) => caches.delete(key))
            );
        }
    } catch (e) {
        // best effort - reload regardless of what failed above
    }

    window.location.reload();
}

// --------------------------------------
// Init: check once per page load, not continuous polling - this is a
// tool people reopen often throughout the day, so a fresh check on each
// visit is enough without adding background-interval complexity.
// --------------------------------------

window.addEventListener('load', () => {
    setTimeout(checkForUnreadActivity, 800);
});
