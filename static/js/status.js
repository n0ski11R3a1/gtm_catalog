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

// Pagination state for the "See more" button. loadedEvents accumulates
// every page fetched so far (re-rendered in full each time, date
// headers included) rather than appending raw DOM nodes - simplest way
// to keep date-group headings correct as more, older rows come in.
let loadedEvents = [];
let activityHasMore = false;
let loadingMoreActivity = false;

// Groups activity rows under "Today" / "Yesterday" / a short date, same
// idea as most notification-tray UIs, so a rep skimming the expanded
// list can tell at a glance which batch of changes happened when.
function dateHeadingFor(date) {
    if (!date) return 'Unknown date';

    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const startOfEvent = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const diffDays = Math.round((startOfToday - startOfEvent) / 86400000);

    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';

    return date.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: startOfEvent.getFullYear() !== now.getFullYear() ? 'numeric' : undefined,
    });
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
    const seeMoreBtn = document.getElementById('activitySeeMoreBtn');
    if (!listEl) return;

    listEl.innerHTML = '';

    if (!events.length) {
        if (emptyEl) emptyEl.style.display = '';
        if (seeMoreBtn) seeMoreBtn.style.display = 'none';
        return;
    }

    if (emptyEl) emptyEl.style.display = 'none';

    let lastHeading = null;

    events.forEach((ev) => {
        const evDate = parseSqliteUtc(ev.created_at);
        const heading = dateHeadingFor(evDate);

        // events arrive newest-first, so a new heading only needs to be
        // inserted when the heading actually changes from the row before it.
        if (heading !== lastHeading) {
            const headingEl = document.createElement('div');
            headingEl.className = 'activity-date-heading';
            headingEl.textContent = heading;
            listEl.appendChild(headingEl);
            lastHeading = heading;
        }

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

    if (seeMoreBtn) {
        seeMoreBtn.style.display = activityHasMore ? '' : 'none';
        seeMoreBtn.disabled = false;
        seeMoreBtn.textContent = 'See more';
    }
}

// --------------------------------------
// "See more": fetches the next page (cursor = oldest id already loaded)
// and re-renders the full accumulated list, so date headings stay
// correct across the boundary between pages.
// --------------------------------------

async function loadMoreActivity() {
    if (loadingMoreActivity || !loadedEvents.length) return;

    const seeMoreBtn = document.getElementById('activitySeeMoreBtn');
    loadingMoreActivity = true;
    if (seeMoreBtn) {
        seeMoreBtn.disabled = true;
        seeMoreBtn.textContent = 'Loading...';
    }

    try {
        const oldestId = loadedEvents[loadedEvents.length - 1].id;
        const res = await fetch('/api/activity?limit=30&before_id=' + oldestId);
        const data = await res.json();

        loadedEvents = loadedEvents.concat(data.events || []);
        activityHasMore = !!data.has_more;

        renderActivityList(loadedEvents);
    } catch (e) {
        // offline or request failed - just re-enable the button so the
        // person can try again, rather than leaving it stuck disabled
        if (seeMoreBtn) {
            seeMoreBtn.disabled = false;
            seeMoreBtn.textContent = 'See more';
        }
    } finally {
        loadingMoreActivity = false;
    }
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

        loadedEvents = data.events || [];
        activityHasMore = !!data.has_more;
        renderActivityList(loadedEvents);

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
    refreshPushToggleUI();

    const modalEl = document.getElementById('statusModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
}

// --------------------------------------
// Push notifications (phone/desktop notification tray)
// --------------------------------------

// PushManager.subscribe() needs the VAPID public key as a Uint8Array,
// not the base64url string the server hands back - this is the
// standard conversion (browsers don't do it for you).
function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; i++) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}

function pushSupported() {
    return 'serviceWorker' in navigator && 'PushManager' in window;
}

async function getExistingPushSubscription() {
    if (!pushSupported()) return null;
    const registration = await navigator.serviceWorker.getRegistration();
    if (!registration) return null;
    return registration.pushManager.getSubscription();
}

// Reflects actual subscription state in the toggle UI - checked
// silently (no permission prompt), both on page load and whenever the
// panel opens, so the toggle never lies about whether this device is
// really subscribed (e.g. after the person cleared site data elsewhere).
async function refreshPushToggleUI() {
    const toggle = document.getElementById('pushToggle');
    if (!toggle) return;

    if (!pushSupported() || Notification.permission === 'denied') {
        toggle.disabled = true;
        toggle.checked = false;
        return;
    }

    toggle.disabled = false;
    const existing = await getExistingPushSubscription();
    toggle.checked = !!existing;
}

async function subscribeToPush() {
    const registration = await navigator.serviceWorker.getRegistration();
    if (!registration) return false;

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') return false;

    const keyRes = await fetch('/api/push/public-key');
    const keyData = await keyRes.json();

    const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyData.publicKey),
    });

    const raw = subscription.toJSON();
    await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: raw.endpoint, keys: raw.keys }),
    });

    return true;
}

async function unsubscribeFromPush() {
    const existing = await getExistingPushSubscription();
    if (!existing) return;

    const endpoint = existing.endpoint;
    await existing.unsubscribe();

    // Best-effort - if this fails (offline), the endpoint will just get
    // pruned server-side the next time a push to it 404s/410s.
    try {
        await fetch('/api/push/unsubscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ endpoint }),
        });
    } catch (e) {
        // ignore - see comment above
    }
}

async function onPushToggleChange(event) {
    const toggle = event.target;
    toggle.disabled = true;

    try {
        if (toggle.checked) {
            const ok = await subscribeToPush();
            if (!ok) toggle.checked = false; // permission denied/dismissed
        } else {
            await unsubscribeFromPush();
        }
    } catch (e) {
        // subscribe/unsubscribe failed (offline, browser quirk, etc.) -
        // put the toggle back to whatever's actually true rather than
        // trusting the click
        await refreshPushToggleUI();
    } finally {
        toggle.disabled = false;
    }
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
    refreshPushToggleUI();

    const seeMoreBtn = document.getElementById('activitySeeMoreBtn');
    if (seeMoreBtn) {
        seeMoreBtn.addEventListener('click', loadMoreActivity);
    }

    const pushToggle = document.getElementById('pushToggle');
    if (pushToggle) {
        pushToggle.addEventListener('change', onPushToggleChange);
    }
});
