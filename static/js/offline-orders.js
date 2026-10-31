// Authenticated field-order queue. The non-extractable AES key and
// ciphertext are stored separately in IndexedDB; this protects saved data
// at rest but does not protect against a compromised device or same-origin
// script execution.
const OFFLINE_DB_NAME = 'gtm-field-orders';
const OFFLINE_DB_VERSION = 1;
const OFFLINE_KEY_ID = 'device-key';
let offlineAuth = null;
let offlineBusy = false;
let offlineOutlets = [];
let offlineOutletId = null;
let offlineOutletMatches = [];

function openOfflineDatabase() {
    return new Promise((resolve, reject) => {
        if (!('indexedDB' in window)) {
            reject(new Error('This browser does not support secure offline storage.'));
            return;
        }

        const request = indexedDB.open(OFFLINE_DB_NAME, OFFLINE_DB_VERSION);
        request.onupgradeneeded = () => {
            const database = request.result;
            if (!database.objectStoreNames.contains('keys')) {
                database.createObjectStore('keys');
            }
            if (!database.objectStoreNames.contains('vault')) {
                database.createObjectStore('vault');
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error('Could not open offline storage.'));
        request.onblocked = () => reject(new Error('Offline storage is blocked by another open app tab.'));
    });
}

function offlineStoreRequest(storeName, mode, action) {
    return openOfflineDatabase().then(database => new Promise((resolve, reject) => {
        const transaction = database.transaction(storeName, mode);
        const request = action(transaction.objectStore(storeName));
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error('Could not access offline storage.'));
        transaction.oncomplete = () => database.close();
        transaction.onerror = () => {
            database.close();
            reject(transaction.error || new Error('Offline storage transaction failed.'));
        };
        transaction.onabort = () => {
            database.close();
            reject(transaction.error || new Error('Offline storage transaction was aborted.'));
        };
    }));
}

async function getOfflineCryptoKey() {
    if (!window.isSecureContext || !window.crypto || !window.crypto.subtle) {
        throw new Error('Encrypted offline orders require a secure HTTPS connection.');
    }

    let key = await offlineStoreRequest('keys', 'readonly', store => store.get(OFFLINE_KEY_ID));
    if (key) return key;

    key = await crypto.subtle.generateKey(
        { name: 'AES-GCM', length: 256 },
        false,
        ['encrypt', 'decrypt']
    );
    await offlineStoreRequest('keys', 'readwrite', store => store.put(key, OFFLINE_KEY_ID));
    return key;
}

async function readOfflineValue(id) {
    const record = await offlineStoreRequest('vault', 'readonly', store => store.get(id));
    if (!record) return null;
    const key = await getOfflineCryptoKey();
    const cleartext = await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: record.iv },
        key,
        record.ciphertext
    );
    return JSON.parse(new TextDecoder().decode(cleartext));
}

async function writeOfflineValue(id, value) {
    const key = await getOfflineCryptoKey();
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const cleartext = new TextEncoder().encode(JSON.stringify(value));
    const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, cleartext);
    await offlineStoreRequest('vault', 'readwrite', store =>
        store.put({ iv, ciphertext }, id)
    );
}

async function removeOfflineValue(id) {
    await offlineStoreRequest('vault', 'readwrite', store => store.delete(id));
}

function normalizeOutletName(value) {
    return String(value || '').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
}

function newOfflineClientId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function getCurrentVisitLocation() {
    if (!navigator.geolocation) {
        return Promise.reject(new Error('This browser does not support device location.'));
    }
    return new Promise((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(
            position => resolve({
                latitude: position.coords.latitude,
                longitude: position.coords.longitude,
                accuracy: position.coords.accuracy,
            }),
            error => reject(new Error(
                error.code === error.PERMISSION_DENIED
                    ? 'Location permission was denied. Uncheck location capture or allow location and retry.'
                    : 'Could not get device location. Uncheck location capture or try again.'
            )),
            { enableHighAccuracy: true, maximumAge: 60000, timeout: 20000 }
        );
    });
}

function renderKnownOutlets(outlets) {
    offlineOutlets = Array.isArray(outlets) ? outlets : [];
    const datalist = document.getElementById('knownOutlets');
    if (datalist) {
        datalist.replaceChildren();
        for (const outlet of offlineOutlets) {
            const label = [outlet.name, outlet.area].filter(Boolean).join(' · ');
            for (const name of [outlet.name, ...(outlet.aliases || [])]) {
                if (!name) continue;
                const option = document.createElement('option');
                option.value = name;
                option.label = label;
                datalist.appendChild(option);
            }
        }
    }

    const input = document.getElementById('outletNameInput');
    if (input) resolveKnownOutlet(input.value);
}

function resolveKnownOutlet(value) {
    const normalized = normalizeOutletName(value);
    const matchesById = new Map();
    if (normalized) {
        for (const outlet of offlineOutlets) {
            const names = [outlet.name, ...(outlet.aliases || [])];
            if (names.some(name => normalizeOutletName(name) === normalized)) {
                matchesById.set(outlet.id, outlet);
            }
        }
    }

    offlineOutletMatches = Array.from(matchesById.values());
    const select = document.getElementById('outletMatchSelect');
    const label = document.getElementById('outletMatchLabel');
    offlineOutletId = null;
    if (!select || !label) return;

    select.replaceChildren(new Option('Select an outlet', ''));
    if (offlineOutletMatches.length < 2) {
        select.style.display = 'none';
        label.style.display = 'none';
        if (offlineOutletMatches.length === 1) offlineOutletId = offlineOutletMatches[0].id;
        return;
    }

    for (const outlet of offlineOutletMatches) {
        const option = new Option(
            [outlet.name, outlet.area, outlet.note].filter(Boolean).join(' · '),
            String(outlet.id)
        );
        select.add(option);
    }
    select.value = '';
    select.style.display = '';
    label.style.display = '';
}

function showOfflineMessage(message, isError = false) {
    const element = document.getElementById('offlineOrdersMessage');
    if (!element) return;
    element.textContent = message;
    element.className = `small mt-2 ${isError ? 'text-danger' : 'text-success'}`;
    element.style.display = '';
}

async function requestPersistentBrowserStorage() {
    if (!navigator.storage || typeof navigator.storage.persist !== 'function') return false;
    try {
        return await navigator.storage.persist();
    } catch (error) {
        showOfflineMessage(`Could not request persistent browser storage: ${error.message}`, true);
        return false;
    }
}

function showOfflineRep(auth) {
    const login = document.getElementById('offlineRepLogin');
    const status = document.getElementById('offlineRepStatus');
    const logout = document.getElementById('offlineLogoutBtn');
    const repSelect = document.getElementById('repNameSelect');
    if (!status) return;

    if (!auth) {
        if (login) login.style.display = '';
        if (logout) logout.style.display = 'none';
        status.textContent = 'Not signed in for offline orders. Online ordering remains available.';
        if (repSelect) repSelect.disabled = false;
        return;
    }

    const repLabel = `${auth.rep.code} (${auth.rep.name})`;
    if (login) login.style.display = 'none';
    if (logout) logout.style.display = '';
    status.textContent = `Signed in as ${repLabel}.`;
    if (repSelect) {
        const option = Array.from(repSelect.options).find(item => item.value === repLabel);
        if (option) repSelect.value = repLabel;
        repSelect.disabled = true;
    }
}

async function renderOfflineQueue() {
    const list = document.getElementById('offlineQueueList');
    const sync = document.getElementById('offlineSyncBtn');
    if (!list) return;

    try {
        const queue = await readOfflineValue('queue') || [];
        const visits = await readOfflineValue('visits') || [];
        const deliveries = await readOfflineValue('delivery_queue') || [];
        const handovers = await readOfflineValue('handover_queue') || [];
        const matchingOrders = offlineAuth
            ? queue.filter(order => order.rep_id === offlineAuth.rep.id)
            : queue;
        const matchingVisits = offlineAuth
            ? visits.filter(visit => visit.rep_id === offlineAuth.rep.id)
            : visits;
        const matchingDeliveries = offlineAuth
            ? deliveries.filter(attempt => attempt.rep_id === offlineAuth.rep.id)
            : deliveries;
        const matchingHandovers = offlineAuth
            ? handovers.filter(handover => handover.rep_id === offlineAuth.rep.id)
            : handovers;
        list.replaceChildren();
        const summary = document.createElement('div');
        summary.textContent = `${matchingOrders.length} order${matchingOrders.length === 1 ? '' : 's'}, ${matchingVisits.length} visit${matchingVisits.length === 1 ? '' : 's'}, ${matchingDeliveries.length} delivery${matchingDeliveries.length === 1 ? '' : 'ies'}, and ${matchingHandovers.length} handover${matchingHandovers.length === 1 ? '' : 's'} waiting to sync.`;
        list.appendChild(summary);
        for (const order of matchingOrders) {
            if (!order.last_error) continue;
            const failure = document.createElement('div');
            failure.className = 'text-danger';
            failure.textContent = `Order ${order.client_order_id.slice(0, 8)}: ${order.last_error}`;
            list.appendChild(failure);
        }
        for (const visit of matchingVisits) {
            if (!visit.last_error) continue;
            const failure = document.createElement('div');
            failure.className = 'text-danger';
            failure.textContent = `Visit ${visit.client_visit_id.slice(0, 8)}: ${visit.last_error}`;
            list.appendChild(failure);
        }
        for (const attempt of matchingDeliveries) {
            if (!attempt.last_error) continue;
            const failure = document.createElement('div');
            failure.className = 'text-danger';
            failure.textContent = `Delivery for order #${attempt.order_id}: ${attempt.last_error}`;
            list.appendChild(failure);
        }
        for (const handover of matchingHandovers) {
            if (!handover.last_error) continue;
            const failure = document.createElement('div');
            failure.className = 'text-danger';
            failure.textContent = `Cash handover: ${handover.last_error}`;
            list.appendChild(failure);
        }
        if (
            queue.some(order => offlineAuth && order.rep_id !== offlineAuth.rep.id)
            || visits.some(visit => offlineAuth && visit.rep_id !== offlineAuth.rep.id)
            || deliveries.some(attempt => offlineAuth && attempt.rep_id !== offlineAuth.rep.id)
            || handovers.some(handover => offlineAuth && handover.rep_id !== offlineAuth.rep.id)
        ) {
            const otherRep = document.createElement('div');
            otherRep.textContent = 'Records from another rep account remain encrypted here; sign in as that rep to sync them.';
            list.appendChild(otherRep);
        }
        if (sync) {
            sync.disabled = !offlineAuth
                || !(matchingOrders.length || matchingVisits.length || matchingDeliveries.length || matchingHandovers.length)
                || !navigator.onLine
                || offlineBusy;
        }
    } catch (error) {
        list.textContent = `Secure storage unavailable: ${error.message}`;
        if (sync) sync.disabled = true;
    }
}

async function renderDeliveryAssignments() {
    const container = document.getElementById('deliveryAssignments');
    const handoverAmount = document.getElementById('deliveryHandoverAmount');
    const handoverDate = document.getElementById('deliveryHandoverDate');
    const handoverButton = document.getElementById('queueHandoverBtn');
    if (!container) return;
    container.replaceChildren();
    if (!offlineAuth) {
        container.textContent = 'Sign in as a rep to load delivery assignments.';
        if (handoverButton) handoverButton.disabled = true;
        return;
    }

    try {
        const saved = await readOfflineValue('delivery_assignments');
        const orders = saved && Array.isArray(saved.orders) ? saved.orders : [];
        const queue = await readOfflineValue('delivery_queue') || [];
        const handoverQueue = await readOfflineValue('handover_queue') || [];
        const queuedOrderIds = new Set(queue
            .filter(attempt => attempt.rep_id === offlineAuth.rep.id)
            .map(attempt => Number(attempt.order_id)));
        const available = orders.filter(order => !queuedOrderIds.has(Number(order.id)));
        if (!saved) {
            container.textContent = 'Connect and refresh to download assigned deliveries for offline use.';
        } else if (!available.length) {
            container.textContent = 'No unrecorded delivery assignments in the saved list.';
        }

        for (const order of available) {
            const card = document.createElement('div');
            card.className = 'border rounded p-2 mt-2';
            const heading = document.createElement('div');
            heading.className = 'fw-semibold';
            heading.textContent = `Order #${order.id} · ${order.outlet_name}${order.area ? ` · ${order.area}` : ''}`;
            card.appendChild(heading);
            const deliveredInputs = [];
            for (const item of order.items || []) {
                const line = document.createElement('label');
                line.className = 'form-label small mt-2 mb-1 d-block';
                line.textContent = `${item.product_name} · remaining ${item.quantity}`;
                const quantity = document.createElement('input');
                quantity.className = 'form-control form-control-sm';
                quantity.type = 'number';
                quantity.min = '0';
                quantity.max = String(item.quantity);
                quantity.step = '1';
                quantity.value = String(item.quantity);
                line.appendChild(quantity);
                card.appendChild(line);
                deliveredInputs.push({
                    input: quantity,
                    itemId: item.id,
                    unitRetail: Number(item.unit_retail || 0),
                });
            }
            const total = document.createElement('div');
            total.className = 'small mt-1';
            total.textContent = `Retail total: ${Number(order.total_retail).toLocaleString()} Ks`;
            card.appendChild(total);

            const cashLabel = document.createElement('label');
            cashLabel.className = 'form-label small mt-2 mb-1';
            cashLabel.textContent = 'Cash collected (Ks)';
            const cash = document.createElement('input');
            cash.className = 'form-control form-control-sm';
            cash.type = 'number';
            cash.min = '0';
            cash.step = '0.01';
            const deliveredSubtotal = () => deliveredInputs.reduce(
                (total, line) => total + Math.max(0, Number(line.input.value) || 0) * line.unitRetail,
                0
            );
            const remainingValue = (order.items || []).reduce(
                (total, item) => total + Number(item.quantity) * Number(item.unit_retail || 0),
                0
            );
            const previousOutstanding = Math.max(
                0,
                Number(order.cash_collectable || 0) - remainingValue
            );
            cash.value = String(order.cash_collectable);
            cash.max = String(order.cash_collectable);
            cashLabel.appendChild(cash);
            card.appendChild(cashLabel);
            for (const line of deliveredInputs) {
                line.input.addEventListener('input', () => {
                    const maxCash = previousOutstanding + deliveredSubtotal();
                    cash.max = String(maxCash);
                    if (Number(cash.value) > maxCash) cash.value = String(maxCash);
                });
            }

            const note = document.createElement('input');
            note.className = 'form-control form-control-sm mt-2';
            note.maxLength = 500;
            note.placeholder = 'Delivery note (optional)';
            card.appendChild(note);

            const actions = document.createElement('div');
            actions.className = 'd-flex gap-2 mt-2';
            const delivered = document.createElement('button');
            delivered.type = 'button';
            delivered.className = 'btn btn-sm btn-success';
            delivered.textContent = 'Record delivered quantities';
            delivered.addEventListener('click', () => {
                const lines = deliveredInputs
                    .map(line => ({
                        order_item_id: line.itemId,
                        quantity: Number(line.input.value),
                    }))
                    .filter(line => line.quantity > 0);
                queueDeliveryAttempt(order, 'delivered', cash.value, '', note.value, lines);
            });
            actions.appendChild(delivered);
            card.appendChild(actions);

            const failReason = document.createElement('input');
            failReason.className = 'form-control form-control-sm mt-2';
            failReason.maxLength = 200;
            failReason.placeholder = 'Reason required if delivery failed';
            card.appendChild(failReason);
            const failed = document.createElement('button');
            failed.type = 'button';
            failed.className = 'btn btn-sm btn-outline-danger mt-2';
            failed.textContent = 'Record failed delivery';
            failed.addEventListener('click', () =>
                queueDeliveryAttempt(order, 'failed', 0, failReason.value, note.value)
            );
            card.appendChild(failed);
            container.appendChild(card);
        }

        const allPending = saved && Array.isArray(saved.pending_cash) ? saved.pending_cash : [];
        const handoverDates = Array.from(new Set(
            allPending.map(item => String(item.created_at).slice(0, 10))
        )).sort().reverse();
        const previousDate = handoverDate ? handoverDate.value : '';
        if (handoverDate) {
            handoverDate.replaceChildren();
            for (const date of handoverDates) handoverDate.add(new Option(date, date));
            handoverDate.value = handoverDates.includes(previousDate)
                ? previousDate
                : (handoverDates[0] || '');
        }
        const selectedDate = handoverDate ? handoverDate.value : '';
        const alreadyQueued = new Set(handoverQueue
            .filter(handover => handover.rep_id === offlineAuth.rep.id)
            .flatMap(handover => handover.attempt_ids || []));
        const pending = allPending.filter(item =>
            String(item.created_at).slice(0, 10) === selectedDate
            && !alreadyQueued.has(item.id)
        );
        const pendingTotal = pending.reduce((total, item) => total + Number(item.cash_collected || 0), 0);
        if (handoverAmount && document.activeElement !== handoverAmount) {
            handoverAmount.value = pendingTotal.toFixed(2);
        }
        if (handoverButton) handoverButton.disabled = !pending.length;
        if (pending.length) {
            const notice = document.createElement('div');
            notice.className = 'small text-muted mt-2';
            notice.textContent = `${pending.length} unhanded cash collection(s) from ${selectedDate}: ${pendingTotal.toLocaleString()} Ks`;
            container.appendChild(notice);
        }
    } catch (error) {
        container.textContent = `Could not read delivery storage: ${error.message}`;
        if (handoverButton) handoverButton.disabled = true;
    }
}

async function refreshDeliveryAssignments({ silent = false } = {}) {
    if (!offlineAuth) {
        showOfflineMessage('Sign in before refreshing delivery assignments.', true);
        return;
    }
    if (!navigator.onLine) {
        showOfflineMessage('Connect to the server to refresh assignments.', true);
        return;
    }
    const button = document.getElementById('refreshDeliveriesBtn');
    if (button) button.disabled = true;
    try {
        const response = await fetch('/api/mobile/deliveries', {
            headers: { Authorization: `Bearer ${offlineAuth.access_token}` },
            cache: 'no-store',
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not refresh delivery assignments.');
        if (!Array.isArray(data.orders) || !Array.isArray(data.pending_cash)) {
            throw new Error('The server returned an invalid delivery list.');
        }
        await writeOfflineValue('delivery_assignments', {
            orders: data.orders,
            pending_cash: data.pending_cash,
            refreshed_at: new Date().toISOString(),
        });
        await renderDeliveryAssignments();
        if (!silent) showOfflineMessage(`${data.orders.length} delivery assignment(s) refreshed.`);
    } catch (error) {
        showOfflineMessage(error.message || 'Could not refresh delivery assignments.', true);
    } finally {
        if (button) button.disabled = false;
    }
}

async function queueDeliveryAttempt(order, result, cashCollected, failureReason, note, deliveredItems = null) {
    if (!offlineAuth) {
        showOfflineMessage('Sign in as a rep before recording a delivery.', true);
        return;
    }
    if (result === 'failed' && !String(failureReason || '').trim()) {
        showOfflineMessage('Enter a reason for the failed delivery.', true);
        return;
    }
    if (result === 'delivered') {
        const selected = Array.isArray(deliveredItems) ? deliveredItems : (order.items || []).map(item => ({
            order_item_id: item.id,
            quantity: Number(item.quantity),
        }));
        const itemById = new Map((order.items || []).map(item => [Number(item.id), item]));
        const selectedValue = selected.reduce((total, line) => {
            const item = itemById.get(Number(line.order_item_id));
            return total + (item ? Number(line.quantity) * Number(item.unit_retail || 0) : 0);
        }, 0);
        const remainingValue = (order.items || []).reduce(
            (total, item) => total + Number(item.quantity) * Number(item.unit_retail || 0),
            0
        );
        const previousOutstanding = Math.max(
            0,
            Number(order.cash_collectable || 0) - remainingValue
        );
        const cashLimit = previousOutstanding + selectedValue;
        if (!selected.length || selected.some(line => !Number.isFinite(Number(line.quantity)) || Number(line.quantity) <= 0)) {
            showOfflineMessage('Enter a positive quantity for at least one delivered item.', true);
            return;
        }
        if (
            !Number.isFinite(Number(cashCollected))
            || Number(cashCollected) < 0
            || Number(cashCollected) > cashLimit
        ) {
            showOfflineMessage('Collected cash cannot exceed the unpaid value of delivered items.', true);
            return;
        }
    }
    try {
        const captureLocation = document.getElementById('captureDeliveryLocation');
        const visitLocation = captureLocation && captureLocation.checked
            ? await getCurrentVisitLocation()
            : null;
        const queue = await readOfflineValue('delivery_queue') || [];
        if (queue.some(attempt =>
            attempt.rep_id === offlineAuth.rep.id && Number(attempt.order_id) === Number(order.id)
        )) {
            throw new Error('A delivery outcome for this order is already queued.');
        }
        queue.push({
            client_attempt_id: newOfflineClientId(),
            order_id: order.id,
            result,
            failure_reason: String(failureReason || '').trim(),
            note: String(note || '').trim(),
            cash_collected: result === 'delivered' ? Number(cashCollected) : 0,
            ...(result === 'delivered' ? { delivered_items: deliveredItems } : {}),
            ...(visitLocation ? { visit_location: visitLocation } : {}),
            rep_id: offlineAuth.rep.id,
            last_error: null,
        });
        await writeOfflineValue('delivery_queue', queue);
        if (captureLocation) captureLocation.checked = false;
        await renderDeliveryAssignments();
        await renderOfflineQueue();
        showOfflineMessage(
            navigator.onLine
                ? 'Delivery outcome saved securely. Sync it when ready.'
                : 'Delivery outcome saved securely on this device for later sync.'
        );
    } catch (error) {
        showOfflineMessage(error.message || 'Could not save the delivery outcome.', true);
    }
}

async function queueDeliveryHandover() {
    if (!offlineAuth) return;
    const saved = await readOfflineValue('delivery_assignments');
    const handoverDate = document.getElementById('deliveryHandoverDate').value;
    const allPending = saved && Array.isArray(saved.pending_cash)
        ? saved.pending_cash.filter(item => String(item.created_at).slice(0, 10) === handoverDate)
        : [];
    const queue = await readOfflineValue('handover_queue') || [];
    const alreadyQueued = new Set(queue
        .filter(handover => handover.rep_id === offlineAuth.rep.id)
        .flatMap(handover => handover.attempt_ids || []));
    const pending = allPending.filter(item => !alreadyQueued.has(item.id));
    if (!pending.length) {
        showOfflineMessage('Refresh delivery assignments to load unhanded cash collections.', true);
        return;
    }
    const declaredCash = Number(document.getElementById('deliveryHandoverAmount').value);
    const expected = pending.reduce((total, item) => total + Number(item.cash_collected || 0), 0);
    if (!Number.isFinite(declaredCash) || declaredCash < 0 || Math.abs(declaredCash - expected) > 0.01) {
        showOfflineMessage('Handover cash must match the selected unhanded collections.', true);
        return;
    }
    try {
        queue.push({
            client_handover_id: newOfflineClientId(),
            handover_date: handoverDate,
            attempt_ids: pending.map(item => item.id),
            declared_cash: declaredCash,
            rep_id: offlineAuth.rep.id,
            last_error: null,
        });
        await writeOfflineValue('handover_queue', queue);
        await renderOfflineQueue();
        await renderDeliveryAssignments();
        showOfflineMessage('Cash handover saved securely. Sync it for admin verification.');
    } catch (error) {
        showOfflineMessage(error.message || 'Could not save the cash handover.', true);
    }
}

async function loadOfflineIdentity() {
    try {
        offlineAuth = await readOfflineValue('auth');
    } catch (error) {
        offlineAuth = null;
        showOfflineMessage(`Could not unlock saved offline data: ${error.message}`, true);
    }
    showOfflineRep(offlineAuth);
    try {
        const catalog = await readOfflineValue('catalog');
        if (catalog) renderKnownOutlets(catalog.outlets || []);
    } catch (error) {
        showOfflineMessage(`Could not load saved catalog data: ${error.message}`, true);
    }
    await renderDeliveryAssignments();
    await renderOfflineQueue();
}

async function requestOfflineCatalog() {
    if (!offlineAuth) throw new Error('Sign in before refreshing the offline catalog.');
    if (!navigator.onLine) throw new Error('Connect to the server before refreshing the catalog.');

    const response = await fetch('/api/mobile/catalog', {
        headers: { Authorization: `Bearer ${offlineAuth.access_token}` },
        cache: 'no-store',
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not refresh the catalog.');
    if (!data.snapshot_id || !Array.isArray(data.products)) {
        throw new Error('The server returned an invalid catalog snapshot.');
    }

    data.refreshed_at = new Date().toISOString();
    await writeOfflineValue('catalog', data);
    renderKnownOutlets(data.outlets || []);
    showOfflineMessage(`Catalog snapshot refreshed with ${data.products.length} products.`);
}

async function loginOfflineRep(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = document.getElementById('offlineLoginBtn');
    const username = form.elements.username.value.trim();
    const password = form.elements.password.value;
    if (!username || !password) return;
    if (!window.isSecureContext || !window.crypto || !window.crypto.subtle) {
        showOfflineMessage('Secure offline orders require a supported browser and HTTPS.', true);
        return;
    }
    if (!navigator.onLine) {
        showOfflineMessage('Sign in while connected before leaving for the field.', true);
        return;
    }

    if (button) button.disabled = true;
    try {
        const persistentStorageGranted = await requestPersistentBrowserStorage();
        const response = await fetch('/api/mobile/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
            cache: 'no-store',
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Sign-in failed.');
        if (!data.access_token || !data.rep || !Number.isInteger(data.rep.id)) {
            throw new Error('The server returned an invalid sign-in response.');
        }

        offlineAuth = { access_token: data.access_token, rep: data.rep };
        await writeOfflineValue('auth', offlineAuth);
        showOfflineRep(offlineAuth);
        form.reset();
        await requestOfflineCatalog();
        if (!persistentStorageGranted) {
            showOfflineMessage('The browser did not grant protected persistent storage; it may clear queued data during storage cleanup.', true);
        }
        showOfflineRep(offlineAuth);
        await renderOfflineQueue();
    } catch (error) {
        showOfflineMessage(error.message || 'Could not sign in for offline orders.', true);
    } finally {
        if (button) button.disabled = false;
    }
}

async function syncOfflineOrders() {
    if (!offlineAuth || offlineBusy) return;
    offlineBusy = true;
    const sync = document.getElementById('offlineSyncBtn');
    if (sync) sync.disabled = true;

    try {
        if (!navigator.onLine) throw new Error('Connect to the server before syncing offline records.');
        const queue = await readOfflineValue('queue') || [];
        const visits = await readOfflineValue('visits') || [];
        const deliveryQueue = await readOfflineValue('delivery_queue') || [];
        const handoverQueue = await readOfflineValue('handover_queue') || [];
        const pendingOrders = queue.filter(order => order.rep_id === offlineAuth.rep.id);
        const pendingVisits = visits.filter(visit => visit.rep_id === offlineAuth.rep.id);
        const pendingDeliveries = deliveryQueue.filter(attempt => attempt.rep_id === offlineAuth.rep.id);
        const pendingHandovers = handoverQueue.filter(handover => handover.rep_id === offlineAuth.rep.id);
        if (!pendingOrders.length && !pendingVisits.length && !pendingDeliveries.length && !pendingHandovers.length) {
            showOfflineMessage('There are no queued records for this rep.');
            return;
        }

        const remainingOrders = queue.filter(order => order.rep_id !== offlineAuth.rep.id);
        const remainingVisits = visits.filter(visit => visit.rep_id !== offlineAuth.rep.id);
        const remainingDeliveries = deliveryQueue.filter(attempt => attempt.rep_id !== offlineAuth.rep.id);
        const remainingHandovers = handoverQueue.filter(handover => handover.rep_id !== offlineAuth.rep.id);
        let syncedOrders = 0;
        let syncedVisits = 0;
        let syncedDeliveries = 0;
        let syncedHandovers = 0;
        for (let offset = 0; offset < pendingOrders.length; offset += 50) {
            const batch = pendingOrders.slice(offset, offset + 50);
            const response = await fetch('/api/mobile/sync', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${offlineAuth.access_token}`,
                },
                body: JSON.stringify({ orders: batch }),
                cache: 'no-store',
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Order sync failed.');
            if (!Array.isArray(data.results)) throw new Error('The server returned an invalid sync response.');

            const resultsById = new Map(data.results.map(result => [result.client_order_id, result]));
            for (const order of batch) {
                const result = resultsById.get(order.client_order_id);
                if (result && result.ok) {
                    syncedOrders += 1;
                } else {
                    remainingOrders.push({
                        ...order,
                        last_error: result && result.error ? result.error : 'No result returned; safe to retry.',
                    });
                }
            }
        }

        for (let offset = 0; offset < pendingVisits.length; offset += 50) {
            const batch = pendingVisits.slice(offset, offset + 50);
            const response = await fetch('/api/mobile/visits/sync', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${offlineAuth.access_token}`,
                },
                body: JSON.stringify({ visits: batch }),
                cache: 'no-store',
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Visit sync failed.');
            if (!Array.isArray(data.results)) throw new Error('The server returned an invalid visit sync response.');

            const resultsById = new Map(data.results.map(result => [result.client_visit_id, result]));
            for (const visit of batch) {
                const result = resultsById.get(visit.client_visit_id);
                if (result && result.ok) {
                    syncedVisits += 1;
                } else {
                    remainingVisits.push({
                        ...visit,
                        last_error: result && result.error ? result.error : 'No result returned; safe to retry.',
                    });
                }
            }
        }

        for (let offset = 0; offset < pendingDeliveries.length; offset += 50) {
            const batch = pendingDeliveries.slice(offset, offset + 50);
            const response = await fetch('/api/mobile/deliveries/sync', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${offlineAuth.access_token}`,
                },
                body: JSON.stringify({ attempts: batch }),
                cache: 'no-store',
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Delivery sync failed.');
            if (!Array.isArray(data.results)) throw new Error('The server returned an invalid delivery sync response.');

            const resultsById = new Map(data.results.map(result => [result.client_attempt_id, result]));
            for (const attempt of batch) {
                const result = resultsById.get(attempt.client_attempt_id);
                if (result && result.ok) {
                    syncedDeliveries += 1;
                } else {
                    remainingDeliveries.push({
                        ...attempt,
                        last_error: result && result.error ? result.error : 'No result returned; safe to retry.',
                    });
                }
            }
        }

        for (let offset = 0; offset < pendingHandovers.length; offset += 20) {
            const batch = pendingHandovers.slice(offset, offset + 20);
            const response = await fetch('/api/mobile/handovers/sync', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${offlineAuth.access_token}`,
                },
                body: JSON.stringify({ handovers: batch }),
                cache: 'no-store',
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Cash handover sync failed.');
            if (!Array.isArray(data.results)) throw new Error('The server returned an invalid handover response.');

            const resultsById = new Map(data.results.map(result => [result.client_handover_id, result]));
            for (const handover of batch) {
                const result = resultsById.get(handover.client_handover_id);
                if (result && result.ok) {
                    syncedHandovers += 1;
                } else {
                    remainingHandovers.push({
                        ...handover,
                        last_error: result && result.error ? result.error : 'No result returned; safe to retry.',
                    });
                }
            }
        }

        await writeOfflineValue('queue', remainingOrders);
        await writeOfflineValue('visits', remainingVisits);
        await writeOfflineValue('delivery_queue', remainingDeliveries);
        await writeOfflineValue('handover_queue', remainingHandovers);
        const remainingCount = remainingOrders.length + remainingVisits.length
            + remainingDeliveries.length + remainingHandovers.length;
        const syncedCount = syncedOrders + syncedVisits + syncedDeliveries + syncedHandovers;
        const remainingSummary = remainingCount ? ` ${remainingCount} remain queued.` : '';
        showOfflineMessage(
            syncedCount
                ? `${syncedOrders} order(s), ${syncedVisits} visit(s), ${syncedDeliveries} delivery outcome(s), and ${syncedHandovers} cash handover(s) synced.${remainingSummary}`
                : 'No records synced. Failed records remain queued for retry.',
            syncedCount === 0
        );
        if (syncedDeliveries && navigator.onLine) {
            await refreshDeliveryAssignments({ silent: true });
        }
    } catch (error) {
        showOfflineMessage(error.message || 'Could not sync queued orders.', true);
    } finally {
        offlineBusy = false;
        await renderOfflineQueue();
        await renderDeliveryAssignments();
    }
}

async function recordStandaloneVisit() {
    const button = document.getElementById('recordVisitBtn');
    const outletInput = document.getElementById('outletNameInput');
    const typeInput = document.getElementById('visitTypeInput');
    const noteInput = document.getElementById('visitNoteInput');
    const captureLocation = document.getElementById('captureVisitLocation');
    if (!offlineAuth) {
        showOfflineMessage('Sign in as a rep before recording an offline visit.', true);
        return;
    }

    const outletName = outletInput ? outletInput.value.trim() : '';
    const visitType = typeInput ? typeInput.value : 'general';
    const note = noteInput ? noteInput.value.trim() : '';
    if (!outletName) {
        showOfflineMessage('Enter an outlet name before recording a visit.', true);
        return;
    }
    if (offlineOutletMatches.length > 1 && !offlineOutletId) {
        showOfflineMessage('Choose the correct outlet from the matching outlet list before recording this visit.', true);
        return;
    }

    if (button) button.disabled = true;
    try {
        const visitLocation = captureLocation && captureLocation.checked
            ? await getCurrentVisitLocation()
            : null;
        const queue = await readOfflineValue('visits') || [];
        queue.push({
            client_visit_id: newOfflineClientId(),
            outlet_name: outletName,
            visit_type: visitType,
            note,
            visited_at: new Date().toISOString(),
            ...(offlineOutletId ? { outlet_id: offlineOutletId } : {}),
            ...(visitLocation ? { visit_location: visitLocation } : {}),
            rep_id: offlineAuth.rep.id,
            last_error: null,
        });
        await writeOfflineValue('visits', queue);
        if (outletInput) outletInput.value = '';
        resolveKnownOutlet('');
        if (typeInput) typeInput.value = 'general';
        if (noteInput) noteInput.value = '';
        if (captureLocation) captureLocation.checked = false;
        offlineOutletId = null;
        offlineOutletMatches = [];
        showOfflineMessage(
            navigator.onLine
                ? 'Visit saved securely. Sync queued orders and visits when ready.'
                : 'Visit saved securely on this device. Sync it when you reconnect.'
        );
        await renderOfflineQueue();
    } catch (error) {
        showOfflineMessage(error.message || 'Could not save this visit securely.', true);
    } finally {
        if (button) button.disabled = false;
    }
}

async function signOutOfflineRep() {
    try {
        await removeOfflineValue('auth');
        offlineAuth = null;
        showOfflineRep(null);
        showOfflineMessage('Signed out. Queued orders remain encrypted on this device.');
        await renderOfflineQueue();
    } catch (error) {
        showOfflineMessage(`Could not sign out: ${error.message}`, true);
    }
}

async function submitOfflineCapableOrder(context) {
    if (!offlineAuth) return false;
    const {
        cart,
        outletName,
        submitBtn,
        errorEl,
        successEl,
        repNameSelect,
        outletNameInput,
    } = context;
    if (errorEl) errorEl.style.display = 'none';
    if (successEl) successEl.style.display = 'none';
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Saving order securely...';
    }

    try {
        const catalog = await readOfflineValue('catalog');
        if (!catalog || !catalog.snapshot_id || !Array.isArray(catalog.products)) {
            throw new Error('No saved catalog snapshot. Connect and refresh the catalog before collecting offline orders.');
        }
        const snapshotProducts = new Set(catalog.products.map(product => Number(product.id)));
        const items = cart.map(item => ({
            product_pk: Number(item.product_pk),
            quantity: Number(item.quantity),
        }));
        if (items.some(item => !snapshotProducts.has(item.product_pk))) {
            throw new Error('The order contains a product outside the saved catalog snapshot. Refresh the catalog while connected.');
        }
        if (offlineOutletMatches.length > 1 && !offlineOutletId) {
            throw new Error('Choose the correct outlet from the matching outlet list before saving this order.');
        }
        const captureLocation = document.getElementById('captureOrderLocation');
        const visitLocation = captureLocation && captureLocation.checked
            ? await getCurrentVisitLocation()
            : null;

        const queue = await readOfflineValue('queue') || [];
        const cartKey = getOrCreateIdempotencyKey();
        const alreadyQueued = queue.find(candidate =>
            candidate.rep_id === offlineAuth.rep.id && candidate.cart_key === cartKey
        );
        if (!alreadyQueued) {
            queue.push({
                client_order_id: newOfflineClientId(),
                cart_key: cartKey,
                catalog_snapshot_id: catalog.snapshot_id,
                outlet_name: outletName,
                collected_at: new Date().toISOString(),
                items,
                ...(offlineOutletId ? { outlet_id: offlineOutletId } : {}),
                ...(visitLocation ? { visit_location: visitLocation } : {}),
                rep_id: offlineAuth.rep.id,
                last_error: null,
            });
        }
        await writeOfflineValue('queue', queue);

        saveCart([]);
        clearIdempotencyKey();
        updateCartBadge();
        renderOrderPanel();
        if (repNameSelect) repNameSelect.value = `${offlineAuth.rep.code} (${offlineAuth.rep.name})`;
        if (outletNameInput) outletNameInput.value = '';
        resolveKnownOutlet('');
        if (captureLocation) captureLocation.checked = false;
        if (successEl) {
            successEl.textContent = navigator.onLine
                ? 'Order saved securely. Use “Sync queued orders” when you are ready to submit.'
                : 'Order saved securely on this device. Sync it when you reconnect.';
            successEl.style.display = '';
        }
        await renderOfflineQueue();
    } catch (error) {
        if (errorEl) {
            errorEl.textContent = error.message || 'Could not save this order securely.';
            errorEl.style.display = '';
        }
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Submit Order';
        }
    }
    return true;
}

function initializeOfflineOrders() {
    if (!document.getElementById('offlineRepLogin')) return;
    const form = document.getElementById('offlineRepLogin');
    const refresh = document.getElementById('offlineRefreshCatalogBtn');
    const sync = document.getElementById('offlineSyncBtn');
    const logout = document.getElementById('offlineLogoutBtn');
    const recordVisit = document.getElementById('recordVisitBtn');
    const refreshDeliveries = document.getElementById('refreshDeliveriesBtn');
    const handoverButton = document.getElementById('queueHandoverBtn');
    const handoverDate = document.getElementById('deliveryHandoverDate');
    const outletInput = document.getElementById('outletNameInput');
    const outletSelect = document.getElementById('outletMatchSelect');
    form.addEventListener('submit', loginOfflineRep);
    outletInput.addEventListener('input', () => resolveKnownOutlet(outletInput.value));
    outletSelect.addEventListener('change', () => {
        const selected = offlineOutletMatches.find(outlet => String(outlet.id) === outletSelect.value);
        offlineOutletId = selected ? selected.id : null;
        if (selected) outletInput.value = selected.name;
    });
    refresh.addEventListener('click', async () => {
        try {
            await requestOfflineCatalog();
            await renderOfflineQueue();
        } catch (error) {
            showOfflineMessage(error.message, true);
        }
    });
    sync.addEventListener('click', syncOfflineOrders);
    recordVisit.addEventListener('click', recordStandaloneVisit);
    refreshDeliveries.addEventListener('click', refreshDeliveryAssignments);
    handoverButton.addEventListener('click', queueDeliveryHandover);
    handoverDate.addEventListener('change', renderDeliveryAssignments);
    logout.addEventListener('click', signOutOfflineRep);
    window.addEventListener('online', renderOfflineQueue);
    window.addEventListener('offline', renderOfflineQueue);
    window.addEventListener('online', renderDeliveryAssignments);
    window.addEventListener('offline', renderDeliveryAssignments);
    loadOfflineIdentity();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeOfflineOrders);
} else {
    initializeOfflineOrders();
}
