# GTM Product & Price Catalog — Project Handoff

**This file is regenerated in full every time something changes.** Paste
this plus your current files into a new chat and say "here's my current
app, continue from here" — it should be enough to pick up with zero
missing context.

Last regenerated after: adding sales rep management, order/outlet
tracking, duplicate-submission guard, the redesigned sticky header, and
three bugs found/fixed by the project owner (see §8).

---

## 1. What this app is

A Flask web app (PythonAnywhere) with three faces:

1. **Public catalog** (`/`) — mobile-friendly, installable (PWA) price
   list. Search, filter by category/status, sort by price. Sales reps
   also build an order ("cart") here and submit it — no login needed.
2. **Admin panel** (`/admin`, login required) — upload bulk Excel,
   manually add/edit/delete products, view price-change history, manage
   the sales team, and review every order submitted from the field.
3. **Sales rep order workflow** — a rep picks their name from a locked
   dropdown (you manage the list, they can't type one), enters the
   outlet name, adds products with quantities, and submits. You see every
   order broken down by date/rep/outlet in the admin Orders page.

Data lives in **SQLite** (`gtm_catalog.db`). Excel upload is a bulk
import/refresh (upsert keyed on Product ID), not the live data source.

---

## 2. File structure

```
gtm_catalog/
├── app.py                        Flask routes - all business logic
├── db.py                         All database access - only file that
│                                   talks to SQLite directly
├── config.py                     Secrets/paths - NEVER share this file's
│                                   real contents outside your own deploy
├── requirements.txt               Flask, pandas, openpyxl, Werkzeug
│
├── gtm_catalog.db                 SQLite database (auto-created)
│
├── templates/
│   ├── base.html                    Shared layout: navbar (admin only),
│   │                                  flash messages, PWA meta, fonts
│   ├── index.html                   Public catalog + order/cart UI +
│   │                                  sticky search bar
│   ├── login.html                   Admin login (yours, untouched by me)
│   ├── admin.html                   Admin dashboard (yours, untouched)
│   ├── admin_products.html          Manage Products: list/search/edit/delete
│   ├── product_form.html            Add/Edit Product + Price History tab
│   ├── admin_orders.html            All orders: date filter, summary
│   │                                  stats, per-rep breakdown, list
│   ├── admin_order_detail.html      One order's line items + status
│   ├── admin_reps.html              Sales team list (add/edit/delete)
│   └── rep_form.html                Add/edit a single sales rep
│
├── static/
│   ├── css/
│   │   ├── style.css                Public catalog ("Market Tag" design:
│   │   │                              Ocean/Leaf/Aqua/Mint palette,
│   │   │                              Space Grotesk/Plus Jakarta
│   │   │                              Sans/JetBrains Mono fonts)
│   │   └── admin.css                Admin panel styling (unchanged
│   │                                  visual style from before the
│   │                                  catalog redesign, on purpose)
│   ├── js/
│   │   ├── app.js                   Catalog search/filter/sort/copy-price
│   │   ├── admin.js                 Excel upload drag-and-drop UX
│   │   └── order.js                 Sales rep cart: localStorage cart,
│   │                                  idempotency key, submit flow
│   ├── manifest.json                 PWA manifest
│   └── sw.js                        Service worker - YOU own/maintain
│                                       this file. Currently v9.
│
└── uploads/
    └── sale_price_catalog.xlsx      Last uploaded Excel (backup/import
                                        source, not the live data)
```

---

## 3. Database schema (SQLite, via `db.py`)

### `products`
| Column | Notes |
|---|---|
| `id` | INTEGER PK - internal id, used in admin URLs |
| `product_id` | TEXT - business ID, format **`GTM - ####`** (uppercase, spaced, e.g. `GTM - 0226`). Auto-generated on manual Add; free text preserved on Excel import |
| `product_name`, `upc`, `unit`, `retail`, `wholesale`, `category`, `status` | `status` is `"In Stock"` / `"Out Of Stock"` |

### `price_history`
Append-only log of retail/wholesale changes, keyed on the **business**
`product_id` (survives product deletion/recreation). `changed_at` is a
SQLite timestamp **string** (`'2026-07-23 09:12:01'`) - not a Unix
timestamp. `source` is `"manual"` or `"excel_upload"`. Only logged when a
price actually changes.

### `sales_reps`
Managed list — reps pick from this at order time, **never type a name**.
| Column | Notes |
|---|---|
| `code`, `name` | e.g. `"T-1"`, `"Myu Latt Aung"` |
| `active` | 1/0. Inactive reps disappear from the order-form picker but past orders keep their label unaffected (snapshot, not a live FK) |

`db.rep_label(rep)` builds the exact display/storage string: `"T-1 (Myu Latt Aung)"`.

### `orders`
| Column | Notes |
|---|---|
| `rep_name` | Exact snapshot string, e.g. `"T-1 (Myu Latt Aung)"` |
| `outlet_name` | Free text (not a managed list - explicitly decided against for now) |
| `status` | `"New"` / `"Fulfilled"` / `"Cancelled"` |
| `total_retail`, `total_wholesale` | Computed server-side at submit time |
| `created_at` | SQLite timestamp string |
| `idempotency_key` | Nullable. See §5 - dedup guard against retry/double-tap |

### `order_items`
Line items. **Snapshots** product name/price at order time - editing a
product's price later never rewrites what was actually ordered.
`product_id`, `product_name`, `unit_retail`, `unit_wholesale`, `quantity`,
`line_retail`, `line_wholesale`.

---

## 4. Routes (`app.py`)

**Public (no login):**
| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Catalog page |
| `/api/prices` | GET | JSON feed, same field names as the original Excel-only version so Excel's "Get Data from Web"/Power Query keeps working unchanged |
| `/api/price-history` | GET | JSON feed of price changes, optional `?product_id=` |
| `/order/submit` | POST | Rep submits their cart. JSON body: `rep_name`, `outlet_name`, `idempotency_key`, `items: [{product_pk, quantity}]` |
| `/sw.js` | GET | Serves the service worker with the right header |

**Admin (all require `session["admin"] == True`):**
| Route | Method | Purpose |
|---|---|---|
| `/login`, `/logout` | GET/POST, GET | Auth |
| `/admin` | GET | Dashboard |
| `/admin/products` | GET | List/search products |
| `/admin/products/add` | GET/POST | Add product (auto `GTM - ####` ID) |
| `/admin/products/<id>/edit` | GET/POST | Edit product + view price history |
| `/admin/products/<id>/delete` | POST | Delete product |
| `/admin/reps` | GET | List sales reps |
| `/admin/reps/add` | GET/POST | Add a rep |
| `/admin/reps/<id>/edit` | GET/POST | Edit a rep (code/name/active) |
| `/admin/reps/<id>/delete` | POST | Delete a rep (past orders unaffected) |
| `/admin/orders` | GET | All orders. `?date=YYYY-MM-DD` filters to one day; shows summary stats + per-rep breakdown |
| `/admin/orders/<id>` | GET | One order's detail |
| `/admin/orders/<id>/status` | POST | Change order status |
| `/admin/orders/<id>/delete` | POST | Delete an order |
| `/upload` | POST | Bulk Excel import (upsert) |

---

## 5. Key design decisions worth knowing

- **Auto-generated Product IDs:** `GTM - ####`, 4-digit padded, grows past
  9999 naturally (`GTM - 10000`...). Computed server-side at submit time -
  client can't override it even if it tries (tested). Editing an existing
  product leaves its ID freely editable (legacy/imported IDs may not
  follow the pattern).

- **Excel upload is an upsert, not a wipe:** matches rows by business
  `product_id`. A product missing from a new upload is marked
  `"Out Of Stock"`, **not deleted** - so manually-added products and
  price history both survive re-uploads.

- **Sales reps are a managed list, not free text.** Reps pick from a
  dropdown populated from `db.get_active_reps()`. The server
  independently re-validates the submitted `rep_name` against that same
  active list before creating an order - a tampered/fake rep name sent
  directly to the API is rejected (tested), not just prevented by the UI.

- **Order pricing is never trusted from the browser.** The cart only
  holds `{product_pk, quantity}`. At submit, the server looks up each
  product's *current real price* - a tampered request claiming a fake
  price is silently ignored (tested).

- **Duplicate-submission guard:** the browser generates a unique
  `idempotency_key` per order-in-progress (persisted in localStorage,
  reused across retries of the same submission, replaced once an order
  actually completes). If the same key hits `/order/submit` twice, the
  server returns the original order instead of creating a second one
  (tested: same key -> same order_id; different key -> genuinely new
  order; missing key -> still works, backward compatible).

- **Numeric form fields are `<input type="text">`, not `type="number"`**
  - deliberate fix for an unusable number-spinner UI. Client-side
    validation is a courtesy; the server is the real gatekeeper.

- **Category picker is a custom JS chip list, not `<datalist>`** -
  native datalist has unreliable mobile-browser support (notably older
  Android). Chips render in normal document flow (no floating overlay),
  filter live as you type, and free typing still works for new categories.

---

## 6. Known gotchas / things that have bitten us before

1. **Two different datetime filters exist on purpose:**
   - `datetimeformat` expects a **Unix timestamp** (Excel file's `os.path.getmtime()`).
   - `sqlitedatetime` expects a **SQLite timestamp string** (`orders.created_at`, `price_history.changed_at`).
   Using the wrong one on the wrong field crashes the page.

2. **Flask caches compiled templates in memory** - uploading a new
   `.html` file does nothing until the process restarts (PythonAnywhere:
   Web tab -> Reload).

3. **`config.py` holds your real `ADMIN_PASSWORD_HASH`.** Never re-paste
   a version of this file from a chat that has the placeholder hash in
   it - locks you out of login.

4. **`product_form.html` and its route in `app.py` must stay in sync** -
   the template expects `categories`, `next_product_id` (Add mode), and
   `price_history` (Edit mode) to be passed in. A mismatch here caused a
   real "No filter named cleannum" crash once, from the template and
   route drifting out of sync across upload rounds.

5. **SQLite migrations on a live database need explicit `ALTER TABLE`,
   not just `CREATE TABLE IF NOT EXISTS`.** The latter does nothing for
   a table that already exists. Both `outlet_name` and `idempotency_key`
   were added to the already-live `orders` table via a `PRAGMA
   table_info` check + conditional `ALTER TABLE ADD COLUMN` in
   `init_db()` - tested against a simulated copy of the real
   pre-migration database each time, specifically to make sure existing
   orders are never touched or lost.

---

## 7. Service worker (`sw.js`) — owned/maintained by the project owner

Currently **v9**. History:
- v1->v2: stopped caching HTML pages (was causing a login redirect loop
  from stale cached admin/login pages).
- v8->v9: `/` (catalog page only, never `/admin`/`/login`) is now cached
  on every successful online fetch, so there's a real fallback when
  offline. Before this, offline navigation always failed silently because
  nothing was ever cached for it to fall back to.

**Rule going forward: bump `CACHE_NAME` any time CSS/JS/`STATIC_ASSETS`
changes**, or browsers that visited before keep serving old files.

`STATIC_ASSETS` currently includes `style.css`, `admin.css`, `app.js`,
`admin.js`, `order.js`, `manifest.json`.

---

## 8. Bug history (found and fixed by the project owner directly)

Full detail in the project owner's own `BUG-LOG.md`, summarized here for
continuity:

1. **Service worker not registering** -> root cause was loading the site
   over `http://` instead of `https://`. Service workers require a
   secure context; `navigator.serviceWorker` doesn't exist at all on an
   insecure origin, so registration silently never runs. Not a code bug.
   Fix: always use the `https://` URL. **Follow-up still worth doing:**
   a server-side HTTP->HTTPS redirect so this can't silently recur for
   anyone landing on a bare `http://` link.

2. **Sticky top bar not sticking** -> `overflow-x: hidden` had been added
   to `body` (my fix, for an earlier mobile zoom-out bug). Any non-visible
   `overflow` value on an ancestor breaks `position: sticky` for
   everything inside it - completely silently, no console warning. Fix:
   moved `overflow-x: hidden` to `html` only; `body` keeps `max-width:
   100%` but no `overflow` rule. Applied to my working copy of
   `style.css` too.

3. **Offline mode never actually worked, even before recent changes** ->
   the navigation fallback pointed at `caches.match('/')`, but `/` was
   never being cached anywhere (`STATIC_ASSETS` only ever listed CSS/JS).
   This was a side effect of the v1->v2 login-loop fix, present since that
   fix shipped - not a recent regression. Fixed in `sw.js` v9: `/`
   specifically (never `/admin`/`/login`) gets cached on every successful
   online fetch.

---

## 9. iOS offline issue — current status (unresolved, in progress)

**Symptom:** service worker / offline mode requires an internet
connection on iOS devices, even after the v9 fix.

**What's confirmed from current documentation:** this matches a
well-documented, current Safari/iOS platform limitation, not obviously a
code bug:
- iOS enforces a much smaller Cache Storage quota (~50MB) than
  Android/desktop.
- iOS aggressively evicts cached data (Cache Storage/IndexedDB) after
  periods of inactivity - multiple real-world reports describe offline
  mode working right after an online visit, then failing again later
  with no code change in between.
- Real developer reports describe this exact symptom: offline works for
  pages already visited online, fails for anything not yet cached.

**Diagnostic steps to isolate before assuming it's fixable in code:**
1. Confirm the site was added to the iOS Home Screen (Share -> Add to
   Home Screen), not just used as a regular Safari tab or bookmark - iOS's
   caching/eviction behavior is less reliable outside standalone mode.
2. Confirm a fresh online visit happened after the v9 deploy (bumping
   `CACHE_NAME` invalidates old cache entries - a stale test done before
   a fresh online reload wouldn't have anything real cached yet).
3. Test immediately after that online visit (enable airplane mode within
   a minute or two) to separate "never worked" from "worked, then got
   evicted over time."
4. Confirm Private Browsing wasn't used - Service Workers are fully
   disabled in Safari Private Browsing.
5. If it still fails immediately after a fresh online visit + Home
   Screen install, that points at something more fundamental and would
   need a Mac + cable + Safari's Develop menu to inspect the iPhone's
   Service Worker/Cache Storage state directly - iOS Safari itself has no
   on-device equivalent of desktop DevTools' Application tab.

**Suggested code hardening (not yet applied - proposed, pending
confirmation):** precache `/` during the `install` step itself (in
addition to the existing "cache `/` on every successful navigate"
runtime approach), wrapped so a failure to fetch `/` during install
doesn't block the rest of `STATIC_ASSETS` from precaching:
```js
self.addEventListener('install', (event) => {
    event.waitUntil(
        Promise.all([
            caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)),
            caches.open(CACHE_NAME).then((cache) => cache.add('/').catch(() => {})),
        ])
    );
    self.skipWaiting();
});
```
This closes the gap where a device installs the PWA while online but
never actually completes a full page navigation before going offline -
currently that device would have nothing cached for `/` yet.

**Bottom line:** iOS may never be as reliable for full offline access as
Android, per Apple's own current, documented platform restrictions. The
above steps separate "something we can still fix" from "inherent iOS
ceiling" - worth working through in that order before investing more time
in code changes alone.

---

## 10. What's shelved / not built yet

- Visual/UX redesign pass 2 (bottom tab nav, skeleton loaders,
  pull-to-refresh, dark mode) - deprioritized in favor of the order/cart
  feature and reporting. Still on the table.
- Product images - flagged as probably the single biggest visual upgrade
  available, not started.
- Outlet master list (managed, like sales reps) - explicitly decided
  against; outlet stays free text for now.
- Stock-on-hand quantity tracking - explicitly decided against for now.
- Push notifications, multi-language toggle, GPS visit tracking,
  beat/route planning, approval workflows - discussed as bigger-effort
  ideas common in commercial SFA/DMS tools, explicitly deprioritized as
  premature for current team size.

---

## 11. If you're picking this up in a new chat

Paste this file plus:
- The current `app.py`, `db.py`, and any templates/CSS you're unsure are
  in sync (file-version mismatches across upload rounds have caused real
  bugs before - see §6.4).
- The specific feature or bug you want to tackle next.
- Don't paste an old `config.py` over your live one, and don't paste an
  old `sw.js` over your live one (you maintain that file yourself now).
