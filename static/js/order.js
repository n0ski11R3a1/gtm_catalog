// =====================================================
// GTM CATALOG - Sales Rep Order List ("Cart")
// =====================================================
// Cart lives in localStorage so it survives page reloads/tab switches
// in the field. Only the internal product id (pk) + quantity are stored -
// prices are always re-checked server-side at submit time, never trusted
// from here.

const CART_KEY = 'gtm_order_cart_v1';

// Duplicate-submission guard: one token per order-in-progress. Reused
// across retries of the SAME submission (so the server can recognize a
// repeat and avoid creating a second order), but replaced once the order
// actually finishes (success or manual clear) so the next order gets a
// fresh token.
const IDEMPOTENCY_KEY_STORAGE = 'gtm_order_idempotency_key_v1';

function getOrCreateIdempotencyKey() {
    let key = localStorage.getItem(IDEMPOTENCY_KEY_STORAGE);
    if (!key) {
        key = 'idem-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
        try {
            localStorage.setItem(IDEMPOTENCY_KEY_STORAGE, key);
        } catch (e) {
            // Storage unavailable - key just won't persist across a page
            // reload, but still works for retries within this page load.
        }
    }
    return key;
}

function clearIdempotencyKey() {
    try {
        localStorage.removeItem(IDEMPOTENCY_KEY_STORAGE);
    } catch (e) {
        // ignore
    }
}

function loadCart() {
    try {
        const raw = localStorage.getItem(CART_KEY);
        return raw ? JSON.parse(raw) : [];
    } catch (e) {
        return [];
    }
}

function saveCart(cart) {
    try {
        localStorage.setItem(CART_KEY, JSON.stringify(cart));
    } catch (e) {
        // Storage unavailable (private browsing, etc.) - cart just won't persist.
    }
}

function updateCartBadge() {
    const badge = document.getElementById('cartBadge');
    if (!badge) return;

    const cart = loadCart();
    const totalQty = cart.reduce((sum, item) => sum + item.quantity, 0);

    if (totalQty > 0) {
        badge.textContent = totalQty;
        badge.style.display = '';
    } else {
        badge.style.display = 'none';
    }
}

// --------------------------------------
// Per-card quantity stepper (before adding to order)
// --------------------------------------

function adjustQtyInput(btn, delta) {
    const stepper = btn.closest('.qty-stepper');
    if (!stepper) return;

    const input = stepper.querySelector('.qty-input');
    let value = parseInt(input.value, 10);

    if (isNaN(value) || value < 1) value = 1;

    value += delta;
    if (value < 1) value = 1;

    input.value = value;
}

// --------------------------------------
// Add to order
// --------------------------------------

function addToOrder(btn) {
    const card = btn.closest('.product-card');
    if (!card) return;

    const pk = parseInt(card.getAttribute('data-pk'), 10);
    const name = card.getAttribute('data-fullname');
    const retail = parseFloat(card.getAttribute('data-retail')) || 0;
    const wholesale = parseFloat(card.getAttribute('data-wholesale')) || 0;

    const qtyInput = card.querySelector('.qty-input');
    let qty = parseInt(qtyInput.value, 10);
    if (isNaN(qty) || qty < 1) qty = 1;

    const cart = loadCart();
    const existing = cart.find(item => item.product_pk === pk);

    if (existing) {
        existing.quantity += qty;
    } else {
        cart.push({
            product_pk: pk,
            product_name: name,
            unit_retail: retail,
            unit_wholesale: wholesale,
            quantity: qty,
        });
    }

    saveCart(cart);
    updateCartBadge();

    // Small visual confirmation on the button itself
    const originalText = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-check-lg"></i> Added';
    btn.classList.add('added');
    setTimeout(() => {
        btn.innerHTML = originalText;
        btn.classList.remove('added');
    }, 1200);

    // Reset the card's qty stepper back to 1
    qtyInput.value = 1;
}

// --------------------------------------
// Order panel (modal)
// --------------------------------------

function renderOrderPanel() {
    const cart = loadCart();
    const container = document.getElementById('orderItemsContainer');
    const emptyMsg = document.getElementById('orderEmptyMsg');
    const totalRetailEl = document.getElementById('orderTotalRetail');
    const totalWholesaleEl = document.getElementById('orderTotalWholesale');
    const submitBtn = document.getElementById('submitOrderBtn');

    if (!container) return;

    container.innerHTML = '';

    if (cart.length === 0) {
        emptyMsg.style.display = '';
        totalRetailEl.textContent = '0 Ks';
        totalWholesaleEl.textContent = '0 Ks';
        if (submitBtn) submitBtn.disabled = true;
        return;
    }

    emptyMsg.style.display = 'none';
    if (submitBtn) submitBtn.disabled = false;

    let totalRetail = 0;
    let totalWholesale = 0;

    cart.forEach((item, index) => {
        const lineRetail = item.unit_retail * item.quantity;
        const lineWholesale = item.unit_wholesale * item.quantity;
        totalRetail += lineRetail;
        totalWholesale += lineWholesale;

        const row = document.createElement('div');
        row.className = 'order-item-row';
        row.innerHTML = `
            <div class="order-item-info">
                <div class="order-item-name">${item.product_name}</div>
                <div class="order-item-price">${item.unit_retail.toLocaleString()} Ks each</div>
            </div>
            <div class="order-item-controls">
                <button type="button" class="qty-btn" data-action="dec" data-index="${index}">-</button>
                <span class="order-item-qty">${item.quantity}</span>
                <button type="button" class="qty-btn" data-action="inc" data-index="${index}">+</button>
                <button type="button" class="order-item-remove" data-action="remove" data-index="${index}" aria-label="Remove">
                    <i class="bi bi-trash"></i>
                </button>
            </div>
            <div class="order-item-line-total">${lineRetail.toLocaleString()} Ks</div>
        `;
        container.appendChild(row);
    });

    totalRetailEl.textContent = totalRetail.toLocaleString() + ' Ks';
    totalWholesaleEl.textContent = totalWholesale.toLocaleString() + ' Ks';

    container.querySelectorAll('[data-action]').forEach(el => {
        el.addEventListener('click', function () {
            const action = this.getAttribute('data-action');
            const index = parseInt(this.getAttribute('data-index'), 10);
            handleOrderItemAction(action, index);
        });
    });
}

function handleOrderItemAction(action, index) {
    const cart = loadCart();
    if (index < 0 || index >= cart.length) return;

    if (action === 'inc') {
        cart[index].quantity += 1;
    } else if (action === 'dec') {
        cart[index].quantity -= 1;
        if (cart[index].quantity <= 0) {
            cart.splice(index, 1);
        }
    } else if (action === 'remove') {
        cart.splice(index, 1);
    }

    saveCart(cart);
    updateCartBadge();
    renderOrderPanel();
}

function openOrderPanel() {
    renderOrderPanel();

    const errorEl = document.getElementById('orderError');
    const successEl = document.getElementById('orderSuccess');
    if (errorEl) errorEl.style.display = 'none';
    if (successEl) successEl.style.display = 'none';

    const modalEl = document.getElementById('orderModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
}

function clearOrder() {
    if (loadCart().length === 0) return;

    if (!confirm('Clear the entire order list? This cannot be undone.')) {
        return;
    }

    saveCart([]);
    clearIdempotencyKey();
    updateCartBadge();
    renderOrderPanel();
}

// --------------------------------------
// Submit order
// --------------------------------------

function submitOrder() {
    const cart = loadCart();
    const repNameSelect = document.getElementById('repNameSelect');
    const outletNameInput = document.getElementById('outletNameInput');
    const errorEl = document.getElementById('orderError');
    const successEl = document.getElementById('orderSuccess');
    const submitBtn = document.getElementById('submitOrderBtn');

    if (errorEl) errorEl.style.display = 'none';
    if (successEl) successEl.style.display = 'none';

    const repName = repNameSelect ? repNameSelect.value : '';
    const outletName = outletNameInput ? outletNameInput.value.trim() : '';

    if (!repName) {
        if (errorEl) {
            errorEl.textContent = 'Please select your name.';
            errorEl.style.display = '';
        }
        if (repNameSelect) repNameSelect.focus();
        return;
    }

    if (!outletName) {
        if (errorEl) {
            errorEl.textContent = 'Please enter the outlet name.';
            errorEl.style.display = '';
        }
        if (outletNameInput) outletNameInput.focus();
        return;
    }

    if (cart.length === 0) {
        if (errorEl) {
            errorEl.textContent = 'Your order list is empty.';
            errorEl.style.display = '';
        }
        return;
    }

    const payload = {
        rep_name: repName,
        outlet_name: outletName,
        idempotency_key: getOrCreateIdempotencyKey(),
        items: cart.map(item => ({
            product_pk: item.product_pk,
            quantity: item.quantity,
        })),
    };

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Submitting...';
    }

    fetch('/order/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(response => response.json().then(data => ({ ok: response.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                throw new Error(data.error || 'Something went wrong.');
            }

            if (successEl) {
                successEl.textContent = `Order #${data.order_id} submitted - ${data.total_retail.toLocaleString()} Ks total.`;
                successEl.style.display = '';
            }

            saveCart([]);
            clearIdempotencyKey(); // this order is done - next one starts fresh
            updateCartBadge();
            renderOrderPanel();

            if (repNameSelect) repNameSelect.value = repName; // keep rep selected for the next order
            if (outletNameInput) outletNameInput.value = ''; // outlet changes more often, so clear it

            setTimeout(() => {
                const modalEl = document.getElementById('orderModal');
                if (modalEl && typeof bootstrap !== 'undefined') {
                    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
                    modal.hide();
                }
            }, 1500);
        })
        .catch(err => {
            if (errorEl) {
                errorEl.textContent = err.message || 'Could not submit order. Check your connection and try again.';
                errorEl.style.display = '';
            }
        })
        .finally(() => {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = 'Submit Order';
            }
        });
}

// --------------------------------------
// Init
// --------------------------------------

updateCartBadge();
