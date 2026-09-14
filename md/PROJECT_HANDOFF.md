# GTM Product & Price Catalog — Project Handoff

**Paste this whole file into a new chat and say "here's my current app,
continue from here."** It is written to be sufficient on its own — no
source files need to be re-uploaded just to explain what the app does,
how it's structured, or why past decisions were made. Only upload actual
source files when you want the AI to *edit* something specific.

This version was regenerated from a **full read of every source file**
(not just the previous handoff doc), on 2026-09-03, and corrects drift
found between the old handoff doc and the real files (see §0).

---

## 0. Corrections from the previous handoff doc

The last handoff doc claimed `sw.js` was "currently v21." The actual
file is **v27**. Six versions of real fixes had shipped without the doc
being regenerated:
- v22: product images were landing in the wrong (versioned) cache bucket
  and vanishing on every routine CSS/JS deploy — fixed with a dedicated
  `PRODUCT_IMAGES_CACHE`.
- v23: Gallery pages (`/gallery`, `/gallery/<category>`) were falling
  into the "no offline fallback" branch meant for admin/login pages —
  fixed, now cached like `/product/` pages.
- v24: category names containing `&` (e.g. "Food & Snacks") cached under
  two different keys because browsers and `encodeURIComponent()` escape
  `&` differently — fixed with a canonicalizing helper.
- v26: added `push`/`notificationclick` handlers for real OS-level Web
  Push (tray notifications, not just the in-app bell).
- v27: **perf bug fix** — `/product/` and `/gallery` pages were
  documented as cache-first but the code actually did network-first,
  so every tap on an already-cached page still waited on a live round
  trip. This was the reported "2 second lag on the eye icon." Now
  genuinely cache-first with a background revalidate.

**Lesson baked into this doc's own existence:** `sw.js` specifically is
owned/versioned outside of chat sessions and drifts fast — always treat
an uploaded copy as ground truth over any doc's claimed version number.

---

## 1. What this app is

A Flask web app (Python), deployed on **PythonAnywhere**, with three
faces:

1. **Public catalog** (`/`) — mobile-friendly, installable PWA price
   list for a distribution business (GTM). Search, filter by
   category/status, sort by price. Sales reps build an order ("cart")
   here and submit it — **no rep login**, they pick their name from an
   admin-managed dropdown instead. Each card links to a product detail
   page (`/product/<id>`, also public) with image, description, and the
   same pricing/order actions.
2. **Gallery mode** (`/gallery`) — a separate, browsing-only Photos-app-
   style view: category cards → thumbnail grid → full-screen swipeable
   viewer. Same underlying product data as the main catalog. **No cart,
   no add-to-order** — deliberately out of scope here.
3. **Admin panel** (`/admin`, login required) — bulk Excel upload,
   manual product add/edit/delete, price-change history with a chart,
   sales team management, and a full order review workflow (list,
   filter by date, per-rep breakdown, status changes).

**Data lives in SQLite** (`gtm_catalog.db`). Excel upload is a bulk
import/refresh (**upsert keyed on business Product ID**), not the live
data source — the database is authoritative; Excel is just how bulk
changes get in.

---

## 2. File structure

```
gtm_catalog/
├── app.py                        Flask routes - all business logic
├── db.py                         ALL database access - the only file
│                                    that talks to SQLite directly
├── config.py                     Secrets/paths - NEVER share this
│                                    file's real contents outside your
│                                    own deploy (holds ADMIN_PASSWORD_HASH,
│                                    SECRET_KEY, VAPID keys)
├── push.py                       Web Push sending (pywebpush). Kept
│                                    separate from db.py (which "only
│                                    talks to SQLite") and app.py (routes
│                                    only). Handles WNS-specific header
│                                    quirk (see §6.15) and logs response
│                                    headers on failure (WNS puts the
│                                    real error there, not the body).
├── generate_vapid_keys.py        One-time setup script for the VAPID
│                                    keypair Web Push needs. Run once;
│                                    NEVER regenerate live keys - it
│                                    silently invalidates every existing
│                                    subscription.
├── requirement.txt               Flask, pandas, openpyxl, Werkzeug,
│                                    gunicorn (note: filename is singular
│                                    "requirement.txt", not the usual
│                                    "requirements.txt")
├── backup_db.py                  Automated database backup (see §12) -
│                                    safe live-DB snapshot via sqlite3's
│                                    .backup() API, 14-day rotation. Run
│                                    manually or via a PythonAnywhere
│                                    Scheduled Task.
│
├── gtm_catalog.db                 SQLite database (auto-created) - NOT
│                                    tracked in git (see §12)
├── app.log                        Runtime log (werkzeug + push errors)
├── backups/                       Rotating DB snapshots from
│                                    backup_db.py - NOT tracked in git
│
├── templates/
│   ├── base.html                    Shared layout: navbar (admin pages
│   │                                  only, deliberately hidden on the
│   │                                  catalog page - see §5), flash
│   │                                  messages, PWA meta tags, fonts,
│   │                                  service-worker registration
│   ├── index.html                   Public catalog + order/cart UI.
│   │                                  Single-row glass topbar (brand +
│   │                                  search + filter toggle) over
│   │                                  category chips; bottom glass tab
│   │                                  bar (List/Gallery/Cart/Admin) that
│   │                                  shrinks on scroll-down, expands on
│   │                                  scroll-up (mobile only). Loads
│   │                                  precache.js + status.js.
│   ├── product_detail.html          Product detail page. Image (or
│   │                                  placeholder), description, same
│   │                                  price/order UI as a catalog card -
│   │                                  reuses order.js/app.js unmodified
│   │                                  via matching data-* attributes
│   ├── product_not_found.html       404-style page for an unmatched
│   │                                  product slug
│   ├── gallery.html                 Gallery Level 1: category cards,
│   │                                  each with a 4-photo collage cover
│   │                                  and an OOS-count tag
│   ├── gallery_category.html        Gallery Level 2: thumbnail grid +
│   │                                  Level 3 full-screen swipe viewer
│   │                                  (same page, viewer is a JS overlay)
│   ├── _glass_tabbar.html           Bottom tab bar, shared partial -
│   │                                  included from index.html,
│   │                                  product_detail.html, gallery.html,
│   │                                  gallery_category.html so it can't
│   │                                  drift out of sync across pages
│   ├── _order_modal.html            Order/cart modal, same reasoning -
│   │                                  shared partial
│   ├── login.html                   Admin login
│   ├── admin.html                   Admin dashboard: stats cards,
│   │                                  current-catalog info, Excel
│   │                                  upload box (drag-and-drop),
│   │                                  export buttons (prices, price
│   │                                  history)
│   ├── admin_products.html          Manage Products: searchable list,
│   │                                  edit/delete actions
│   ├── product_form.html            Add/Edit Product. Edit mode has a
│   │                                  tab for Price History with a
│   │                                  Chart.js line chart. Category
│   │                                  picker is custom JS chips (see
│   │                                  §5), not <datalist>
│   ├── admin_orders.html            All orders: date filter, summary
│   │                                  stats, per-rep breakdown, list
│   ├── admin_order_detail.html      One order's line items + status
│   │                                  change form
│   ├── admin_reps.html              Sales team list (add/edit/delete)
│   └── rep_form.html                Add/edit a single sales rep
│
├── static/
│   ├── css/
│   │   ├── style.css                 Public catalog + product detail +
│   │   │                               gallery pages (~1400 lines).
│   │   │                               Ocean/Leaf/Aqua/Mint palette,
│   │   │                               Space Grotesk/Plus Jakarta Sans/
│   │   │                               JetBrains Mono fonts, price-tag
│   │   │                               product cards, "Liquid Glass"
│   │   │                               (backdrop-filter blur) on the
│   │   │                               topbar + bottom tab bar only
│   │   └── admin.css                 Admin panel styling (~350 lines,
│   │                                   intentionally unchanged visual
│   │                                   style from before the catalog
│   │                                   redesign)
│   ├── js/
│   │   ├── app.js                    Catalog search/filter/sort/copy-
│   │   │                               price + sessionStorage state
│   │   │                               restore (search/sort/filter/
│   │   │                               scroll position survive a trip
│   │   │                               to product detail and back).
│   │   │                               filterCatalog() no-ops safely on
│   │   │                               pages without #catalogGrid.
│   │   ├── admin.js                  Excel upload drag-and-drop UX,
│   │   │                               .xlsx extension guard, upload-
│   │   │                               in-progress spinner
│   │   ├── order.js                  Sales rep cart: localStorage cart,
│   │   │                               idempotency key generation/reuse/
│   │   │                               clear, submit flow with
│   │   │                               error/success states. Works
│   │   │                               unmodified on the product detail
│   │   │                               page too.
│   │   ├── gallery.js                 List/Gallery mode preference
│   │   │                               (localStorage redirect on '/'),
│   │   │                               Level 1 category search, Level 2
│   │   │                               thumbnail search+status filter
│   │   │                               (shares the same sessionStorage
│   │   │                               key as app.js so filters carry
│   │   │                               across List<->Gallery), Level 3
│   │   │                               full-screen swipe viewer with
│   │   │                               history.pushState so back-
│   │   │                               button/gesture closes just the
│   │   │                               viewer
│   │   ├── precache.js               Background offline-precache: every
│   │   │                               product page, every product
│   │   │                               image that actually exists,
│   │   │                               every gallery page - with a
│   │   │                               visible progress banner, 5-
│   │   │                               concurrent worker pool. Writes
│   │   │                               the "last sync" timestamp
│   │   │                               status.js reads.
│   │   └── status.js                  Updates & Status panel: live app
│   │                                    version via MessageChannel round
│   │                                    trip to the service worker,
│   │                                    last-offline-sync display,
│   │                                    notification bell unread-check +
│   │                                    activity feed (grouped by
│   │                                    Today/Yesterday/date, paginated
│   │                                    via "See more" cursor), hard-
│   │                                    refresh flow, Web Push
│   │                                    subscribe/unsubscribe toggle
│   ├── product-images/               Product photos - NOT a database
│   │                                    column. Named to match the
│   │                                    business Product ID with spaces
│   │                                    stripped (e.g. GTM-0001.jpg/
│   │                                    .jpeg/.png/.webp). No file =
│   │                                    clean placeholder, never a
│   │                                    broken-image icon.
│   ├── icon-192.png, icon-512.png    PWA icons
│   ├── manifest.json                 PWA manifest (name, icons, theme
│   │                                    color #4f46e5, standalone
│   │                                    display, portrait orientation)
│   └── sw.js                        Service worker - YOU own/maintain
│                                       this file directly, independent
│                                       of chat sessions. Currently v27
│                                       (see §0, §7).
│
└── uploads/
    └── sale_price_catalog.xlsx      Last uploaded Excel (backup/import
                                        source, NOT the live data)
```

---

## 3. Database schema (SQLite, via `db.py`)

### `products`
| Column | Notes |
|---|---|
| `id` | INTEGER PK, autoincrement - internal id, used in admin URLs |
| `product_id` | TEXT, UNIQUE (partial index, blanks exempt) - business ID, canonical format **`GTM - ####`** (uppercase, spaced, 4-digit zero-padded, e.g. `GTM - 0226`). Auto-generated on manual Add via a monotonic counter; free text preserved on Excel import |
| `product_name`, `upc`, `unit`, `retail`, `wholesale`, `category`, `status` | `status` is `"In Stock"` / `"Out Of Stock"`. `retail`/`wholesale` are REAL |
| `description` | TEXT, optional, default `''`. Free text (Burmese/any Unicode needs no special handling). Shown on the public product detail page. Optional column on Excel upload - **a sheet without this column never wipes existing descriptions** |
| `supplier` | TEXT, optional, default `''`. **Internal only, never shown on the public catalog.** Exposed via `/api/prices` for inventory automation. Same "optional column, never bulk-wiped" treatment as description |

**No pricing-margin fields exist yet** (no Base Price, B2C, or margin %
columns) — see §14 for the open item on this.

### `price_history`
Append-only log of retail/wholesale changes, keyed on the **business**
`product_id` (survives product deletion/recreation, unlike the internal
`id`). `changed_at` is a SQLite **timestamp string**
(`'2026-07-23 09:12:01'`), not a Unix timestamp. `source` is `"manual"`
or `"excel_upload"`. Only logged when retail or wholesale **actually
changes** (diff-checked, not logged on every write).

### `activity_log`
Powers the notification bell. Unified feed of event types —
`product_added`, `price_changed`, `back_in_stock`, `out_of_stock` — so
the UI only queries one table.
| Column | Notes |
|---|---|
| `event_type` | one of the four above |
| `product_id` | Business key (survives delete/re-add) |
| `product_name`, `details` | `details` is a short human-readable summary, e.g. `"Retail: 5,000 → 5,500 Ks • Wholesale: 4,500 → 4,800 Ks"` or `"Out Of Stock → In Stock"` |
| `created_at` | SQLite timestamp string |

Logged automatically from `_log_price_change()` / `_log_status_change()`
(each has its own "did this actually change" guard — one source of truth
reused everywhere a write could trigger a notification) and from
`add_product()` / `import_excel_into_db()`'s new-row branch. The
one-time bootstrap import on a fresh database passes
`log_activity=False` deliberately, so a brand-new install doesn't flood
the feed with 200+ "product added" entries on day one.

### `id_counter`
Single-row table backing auto-generated Product IDs. **Monotonic —
only ever increases**, even across deletes. Replaced an earlier
"scan products for the current max GTM-#### and add one" approach that
had a real, confirmed incident: `GTM - 0245`, `GTM - 0255`, `GTM - 0256`
were each reused by a second, unrelated product after the first was
deleted, polluting `price_history`/`activity_log` with mixed history.
Seeded (on migration, or if the row is ever lost) from the highest
`GTM - ####` number that has **ever** appeared anywhere — products,
price_history, AND activity_log, not just current products.

`peek_next_product_id()` (read-only preview, doesn't reserve) vs.
`get_next_product_id()` (reserves and consumes) are **deliberately
separate functions** — see §6.4 gotcha.

### `sales_reps`
Managed list — reps pick from this at order time, **never type a
name**.
| Column | Notes |
|---|---|
| `code`, `name` | e.g. `"T-1"`, `"Myu Latt Aung"` |
| `active` | 1/0. Inactive reps disappear from the order-form picker; past orders keep their label unaffected (it's a snapshot string, not a live FK) |

`db.rep_label(rep)` builds the exact display/storage string:
`"T-1 (Myu Latt Aung)"`.

### `orders`
| Column | Notes |
|---|---|
| `rep_name` | Exact snapshot string, e.g. `"T-1 (Myu Latt Aung)"` |
| `outlet_name` | Free text (explicitly decided against a managed list, for now) |
| `status` | `"New"` / `"Fulfilled"` / `"Cancelled"` |
| `total_retail`, `total_wholesale` | Computed server-side at submit time, never trusted from the client |
| `created_at` | SQLite timestamp string |
| `idempotency_key` | Nullable, indexed. Dedup guard against retry/double-tap — same key submitted twice returns the original order, not a duplicate |

### `order_items`
Line items. **Snapshots** product name/price at order time — editing a
product's price later never rewrites what was actually ordered/sold.
Columns: `product_id`, `product_name`, `unit_retail`, `unit_wholesale`,
`quantity`, `line_retail`, `line_wholesale`.

### `push_subscriptions`
One row per device opted into Web Push. `endpoint` (unique), `p256dh`,
`auth`, `created_at`. Upserted on `endpoint` (a browser re-subscribing
with rotated keys but the same endpoint overwrites, doesn't duplicate).
Pruned automatically when a push to that endpoint returns 404/410
(dead subscription).

---

## 4. Routes (`app.py`)

**Public (no login):**
| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Catalog page |
| `/product/<product_id_slug>` | GET | Product detail page. Slug = business Product ID with spaces stripped (`GTM-0001`), matched space-insensitively (`REPLACE(product_id,' ','') = ? COLLATE NOCASE`). 404s to `product_not_found.html` on no match |
| `/gallery` | GET | Gallery Level 1: category cards with 4-photo collage covers |
| `/gallery/<category>` | GET | Gallery Level 2: thumbnail grid for one category. Redirects with a flash warning if the category doesn't exist |
| `/api/prices` | GET | JSON feed, same field names as the original Excel-only version, so Excel's "Get Data from Web"/Power Query keeps working. Includes `"Description"` and internal `"id"` per product |
| `/api/product-images` | GET | JSON list of `{product_id, path}` for products with a real resolvable image file — used by `precache.js` to avoid guessing extensions client-side |
| `/api/price-history` | GET | JSON feed of price changes, optional `?product_id=` filter |
| `/api/activity` | GET | JSON feed backing the notification bell: `{"latest_id": N, "events": [...], "has_more": bool}`. `?limit=` (default 30, capped 1–100), `?before_id=` cursor for "See more" |
| `/api/push/public-key` | GET | Returns the VAPID public key for `PushManager.subscribe()` |
| `/api/push/subscribe` | POST | Registers a device's push subscription |
| `/api/push/unsubscribe` | POST | Removes a device's push subscription |
| `/order/submit` | POST | Rep submits their cart. JSON body: `rep_name`, `outlet_name`, `idempotency_key`, `items: [{product_pk, quantity}]`. Server re-validates `rep_name` against active reps and re-looks-up every price — nothing from the client is trusted |
| `/sw.js` | GET | Serves the service worker with `Service-Worker-Allowed: /` header |

**Admin (all require `session["admin"] == True`, redirect to `/login` otherwise):**
| Route | Method | Purpose |
|---|---|---|
| `/login`, `/logout` | GET/POST, GET | Auth (username/password, scrypt hash) |
| `/admin` | GET | Dashboard: stats, current catalog file info, upload box, export links |
| `/admin/products` | GET | List/search products (client-side JS filter) |
| `/admin/products/add` | GET/POST | Add product. Product ID always server-generated at submit — client can't override it even though it's shown read-only in the form |
| `/admin/products/<id>/edit` | GET/POST | Edit product + view price history (with Chart.js graph) |
| `/admin/products/<id>/delete` | POST | Delete product |
| `/admin/reps` | GET | List sales reps |
| `/admin/reps/add` | GET/POST | Add a rep |
| `/admin/reps/<id>/edit` | GET/POST | Edit a rep (code/name/active) |
| `/admin/reps/<id>/delete` | POST | Delete a rep (past orders unaffected) |
| `/admin/orders` | GET | All orders. `?date=YYYY-MM-DD` filters to one day; shows summary stats + per-rep breakdown |
| `/admin/orders/<id>` | GET | One order's detail |
| `/admin/orders/<id>/status` | POST | Change order status |
| `/admin/orders/<id>/delete` | POST | Delete an order |
| `/admin/export/prices.xlsx` | GET | Download current DB products as `.xlsx` (sheet `"Product Catalog"`) |
| `/admin/export/price-history.xlsx` | GET | Download full price-change log as `.xlsx` |
| `/upload` | POST | Bulk Excel import (validated, then upserted) |

---

## 5. The Excel upload/import contract (important — this is the
"single source of truth" mechanism)

`config.REQUIRED_COLUMNS` (the actual validation contract):
```python
["Product ID", "Product Name", "UPC", "Unit", "Retail", "Wholesale", "Category", "Status"]
```
`Description` and `Supplier` are **accepted but optional** — a sheet
missing them never wipes existing DB values for those fields.

**`app.validate_excel(path)`**: reads sheet named exactly
`"Product Catalog"` via pandas, checks all `REQUIRED_COLUMNS` are
present. Rejects with a flash message if not — the upload never even
reaches the database layer on a bad file.

**`db.import_excel_into_db(path, replace=True, source="excel_upload", log_activity=True)`**
is an **UPSERT keyed on business Product ID**, not a wipe-and-reinsert:
- Existing products **keep their internal `id`** across uploads, so
  admin edit links and price history stay valid.
- Any row whose Retail/Wholesale differs from the current DB value gets
  a `price_history` entry logged, before being overwritten.
- New Product IDs are inserted as new rows. A **blank** Product ID in
  the sheet means "auto-assign one" (same signal as the web Add Product
  form's blank-ID convention) — a real ID is reserved via
  `get_next_product_id()`, never left as a literal blank (which could
  otherwise collide with another blank row).
- `replace=True` (the default, matches historic behavior): any product
  currently in the DB whose Product ID is **not** present in this
  upload gets marked `Out Of Stock` — never deleted, so it doesn't
  disappear from price history or activity feed. `replace=False` skips
  this entirely (pure merge-in).
- Guards against a re-typed/reused Product ID silently inheriting a
  **deleted** product's old history — collects a warning (doesn't
  block the upload) so an admin can confirm it was intentional.
- `log_activity=False` is used exactly once: the one-time bootstrap
  import on a brand-new database (`init_db()`), so a fresh install
  doesn't flood the activity feed with 200+ "product added" rows.

**Currently, no B2C/Base Price/margin-% columns exist anywhere in this
contract** — see §14.

---

## 6. Key design decisions worth knowing

- **Auto-generated Product IDs:** `GTM - ####`, 4-digit zero-padded,
  grows past 9999 naturally (`GTM - 10000`...). Computed **server-side
  at submit time** — a tampered client request can't override it
  (tested). Editing an *existing* product leaves its ID freely
  editable (legacy/imported IDs may not follow the pattern).

- **Excel upload is an upsert, not a wipe.** See §5.

- **Sales reps are a managed list, not free text.** The server
  independently re-validates the submitted `rep_name` against the
  active-reps list before creating an order — a tampered/fake rep name
  sent directly to the API is rejected (tested), not just prevented by
  the UI dropdown.

- **Order pricing is never trusted from the browser.** The cart only
  holds `{product_pk, quantity}`. At submit, the server looks up each
  product's *current real price* server-side — a tampered request
  claiming a fake price is silently ignored (tested).

- **Duplicate-submission guard (idempotency key):** the browser
  generates a unique key per order-in-progress (persisted in
  localStorage, reused across retries of the same submission, replaced
  once an order actually completes). Same key hitting `/order/submit`
  twice returns the original order instead of creating a second one
  (tested). Missing key still works (backward compatible).

- **Numeric form fields are `<input type="text">`, not
  `type="number"`** — deliberate fix for an unusable mobile
  number-spinner UI. Client-side validation is a courtesy; the server
  is the real gatekeeper.

- **Category picker is a custom JS chip list, not `<datalist>`** —
  native datalist has unreliable mobile-browser support (notably older
  Android). Chips render in normal document flow (no floating overlay),
  filter live as you type, free typing still works for a brand-new
  category.

- **The admin navbar is scoped away from the catalog page on purpose:**
  `{% if session.get("admin") and request.endpoint != 'home' %}` in
  `base.html` — avoids a logged-in admin seeing two stacked nav bars
  (the admin navbar + the catalog's own glass topbar/tabbar) doing
  overlapping jobs. Full admin navbar still renders on every real
  `/admin/*` page.

- **Bottom tab bar over top nav, for the catalog/gallery pages
  specifically:** reps reach for this one-handed, repeatedly, in the
  field — primary nav (List/Gallery/Cart/Admin) belongs in the thumb
  zone at the bottom. Desktop (≥768px) drops the fixed/floating
  behavior and renders it as a normal static row instead.

- **"Liquid Glass" (`backdrop-filter: blur() saturate()`) on the
  topbar and bottom tab bar only** — product cards stay fully opaque.
  Glass-on-glass gets muddy fast; the frosted effect is reserved for
  chrome, not data legibility.

- **Glass-bar animations use `transform`/`opacity` only**, never
  `max-width`/`padding`/`grid-template-rows` — those force a full
  layout reflow on every animation frame (measured, real cause of
  scroll jank, not a vague concern). `backdrop-filter` blur radius was
  also roughly halved on both bars (recomputed continuously during
  scroll, was the single largest jank contributor of anything in the
  glass system).

- **Product images are matched by filename, not stored as a database
  column.** Drop a correctly-named file into `static/product-images/`
  and it appears — filename = business Product ID with spaces
  stripped (`GTM-0001.jpg`), checked at render time against
  `.jpg/.jpeg/.png/.webp`. No match = clean placeholder, never a
  broken-image icon.

- **Product detail pages are keyed by business Product ID in the URL**
  (`/product/GTM-0001`), not the internal `id` — chosen for
  shareability/readability. Lookup normalizes spaces + case on both
  sides so `GTM-0001` and the canonical stored `GTM - 0001` both
  resolve.

- **The product detail page reuses `order.js`/`app.js` completely
  unmodified** — its wrapper carries the exact same `class="card
  product-card"` and `data-*` attributes those scripts already expect
  (`data-pk`, `data-fullname`, `data-retail`, `data-wholesale`,
  `data-unit`). No JS duplication needed.

- **Gallery mode is deliberately browsing-only** — no add-to-order
  anywhere in `gallery.js`, no interaction with the cart or
  `/order/submit`. A separate concern from the main catalog on purpose.

- **The bottom tab bar and order modal are shared partials**
  (`_glass_tabbar.html`, `_order_modal.html`), included from every page
  that needs them instead of duplicated — both have IDs/behavior
  (`cartBadge`, `submitOrderBtn`, the rep dropdown) that must stay in
  sync, and template/route drift has already caused real bugs in this
  project (see §6.4 below).

- **Offline precaching runs from the page itself (`precache.js`), not
  from the service worker's `install` step.** A service worker can't
  easily report live progress back to a page mid-install without a
  `postMessage`/`BroadcastChannel` round trip. `precache.js` checks
  what's actually missing and only shows a progress banner if there's
  real work — most repeat visits are silent. 5-concurrent worker-pool
  pattern, not sequential fetches (200+ sequential cache lookups was
  measurably slow even for the "nothing missing" case).

- **Product/gallery pages live in their own persistent cache buckets**
  (`PRODUCT_PAGES_CACHE`, `PRODUCT_IMAGES_CACHE`, `GALLERY_PAGES_CACHE`),
  separate from the main versioned `CACHE_NAME`. The main cache is
  fully wiped and rebuilt on every version bump (routine CSS/JS
  deploys) — with 225+ product pages potentially precached, forcing a
  full re-download on every unrelated style tweak would be wasteful,
  especially on a rep's mobile data.

- **Hard Refresh clears only the main cache bucket and forces a real
  SW update check — deliberately never touches the persistent product/
  gallery/image buckets.** A naive "clear everything" button would
  undo the entire point of splitting the caches.

- **App version is fetched live from the actual running service
  worker** via a `MessageChannel` round trip (`status.js` asks,
  `sw.js` replies with its own `CACHE_NAME`) — can't drift the way a
  hardcoded version string in a template could.

- **The notification bell's "unread" state is tracked client-side
  only**, in localStorage (`gtm_last_seen_activity_id_v1`) — there's no
  login/per-user concept on the public catalog, so "have I seen this"
  is necessarily per-device. Checked once on page load, not continuous
  polling.

- **`activity_log` is one unified table for four event types** rather
  than separate tables or a UNION at read time — simpler read path for
  the bell, reuses the existing diff-check logic for what counts as a
  real change.

- **Web Push uses VAPID + `pywebpush`**, with a documented WNS
  (Windows/Edge push service) quirk: WNS requires an
  `x-wns-cache-policy` header on every request that `pywebpush` itself
  never sets (confirmed against the installed version — a known,
  unfixed gap in that library). `push.py` always sends
  `"no-cache"` since this app always uses `ttl=0`. WNS also returns 400
  errors with an **empty body**, putting the real reason in response
  **headers** instead — `push.py` logs headers explicitly for exactly
  this reason, not just the body.

---

## 7. Known gotchas / things that have bitten this project before

1. **Two different datetime filters exist on purpose, don't mix them
   up:** `datetimeformat` expects a **Unix timestamp** (used for the
   Excel file's `os.path.getmtime()`). `sqlitedatetime` expects a
   **SQLite timestamp string** (`orders.created_at`,
   `price_history.changed_at`). Wrong one on the wrong field crashes
   the page.

2. **Flask caches compiled templates in memory** — uploading a new
   `.html` file does nothing until the process restarts (PythonAnywhere:
   Web tab → Reload).

3. **`config.py` holds the real `ADMIN_PASSWORD_HASH`.** Never re-paste
   a version of this file from a chat that has a placeholder hash in
   it — that locks out login.

4. **`product_form.html` and its route in `app.py` must stay in sync**
   — the template expects `categories`, `next_product_id` (Add mode),
   and `price_history` (Edit mode) to be passed in. A mismatch here
   caused a real "No filter named cleannum" crash once, from template
   and route drifting apart across upload rounds.

5. **SQLite migrations on a live database need explicit `ALTER TABLE`,
   not just `CREATE TABLE IF NOT EXISTS`** — the latter does nothing
   for a table that already exists. `db.init_db()` uses `PRAGMA
   table_info` checks + conditional `ALTER TABLE ADD COLUMN` for every
   migration (`outlet_name`, `idempotency_key`, `description`,
   `supplier` were all added this way to an already-live database,
   verified against a simulated copy each time).

6. **A large CSS/HTML find/replace can silently orphan a selector.**
   One past edit's replacement boundary landed mid-rule, leaving a
   dangling declaration block with no selector — browsers silently
   discard malformed CSS like that (no console error). The affected
   element then anchored to the wrong positioned ancestor, landing
   nowhere near where it should. Only surfaced visually, well after
   the fact. **Lesson: after any large find/replace across CSS or
   HTML, verify brace/tag balance programmatically
   (`content.count('{') == content.count('}')`) — don't rely on visual
   review alone.**

7. **Never assume you're holding the current `sw.js`.** You (the human
   owner) iterate on this file directly, outside chat sessions — see
   §0 for a real example of six versions of drift. Reusing a stale
   local copy as the base for a version bump has previously thrown
   away already-shipped fixes. **Always paste the current file before
   it gets touched again**, never reconstruct it from memory or an old
   handoff doc's description.

8. **Not every CSS property is safe to `transition`.** `max-width`,
   `padding`, and `grid-template-rows` force a full layout recalculation
   on every animation frame — `transform`/`opacity` don't (compositor-
   only). Any new scroll-linked or frequently-toggled animation should
   default to `transform`/`opacity` first.

9. **`request.mode === 'navigate'` only catches real browser
   navigations, not `fetch()` calls from your own page scripts.**
   `precache.js` downloads pages with a plain `fetch()`, which is NOT
   navigate-mode. `sw.js` checks `/product/`/`/gallery` **by path**,
   before the navigate-mode check, so it catches both a real page load
   and a background precache fetch identically.

10. **`precache.js` and `sw.js` share cache-name strings by hand** —
    they're separate execution contexts (page script vs. service
    worker) and can't import a shared constant. If you ever rename one,
    rename the other to match, or the two silently stop agreeing on
    where product/gallery/image data lives.

11. **The Burmese placeholder text in `product_detail.html`** (shown
    when a product has no description yet) has not been reviewed by a
    native speaker — worth double-checking if you have one available.

12. **`last_insert_rowid()` reflects whichever INSERT happened most
    recently on that connection — not "the INSERT I meant."** Caused a
    real, caught-before-shipping bug: `add_product()` did `INSERT INTO
    products` then, before capturing `last_insert_rowid()`, ran
    `_log_activity()` which does its own `INSERT INTO activity_log` —
    so the captured id silently became the activity log row's id
    instead of the product's. **Any function capturing
    `last_insert_rowid()` after a side-effect write (logging,
    auditing) needs that capture immediately after the INSERT it
    actually cares about, before any other INSERT touches the same
    connection.**

13. **`/api/prices` includes a `"Description"` and `"Supplier"` field
    for every product** (additive, always present via `_row_to_dict()`)
    — shouldn't break existing Power Query column mappings since
    they're new columns, but flagging the shape dependency since that
    endpoint is explicitly relied on to stay stable for external Excel
    consumers.

14. **A bootstrap import would have flooded the notification feed with
    200+ entries on day one** if `log_activity=False` weren't passed
    at the one bootstrap call site in `init_db()` — caught in testing
    (225 real products imported, then activity_log checked — found 225
    rows instead of the expected 0) before shipping.

15. **WNS (Windows push) returns 400 with an empty response body** —
    the real failure reason is in response **headers**
    (`X-WNS-ERROR-DESCRIPTION`, `X-WNS-STATUS`), not the body. Logging
    only the body (the naive approach) makes WNS failures completely
    undiagnosable.

---

## 8. Service worker (`sw.js`) — owned/maintained directly by the
project owner, outside chat sessions

**Currently v27.** Full version history is in the file's own header
comments (each bump documents exactly what changed and why) — see §0
for the six most recent, undocumented-in-the-old-handoff bumps (v22–v27).

**Rule: bump `CACHE_NAME` any time CSS/JS/`STATIC_ASSETS` changes**, or
browsers that visited before keep serving stale files.

`STATIC_ASSETS` currently includes: `style.css`, `admin.css`, `app.js`,
`admin.js`, `order.js`, `gallery.js`, `precache.js`, `status.js`,
`manifest.json`.

Persistent cache buckets (survive normal `CACHE_NAME` version bumps,
only cleared if their own name changes): `PRODUCT_PAGES_CACHE`
(`gtm-product-pages-v1`), `PRODUCT_IMAGES_CACHE`
(`gtm-product-images-v1`), `GALLERY_PAGES_CACHE`
(`gtm-gallery-pages-v1`).

---

## 9. iOS offline — hardening applied, real-world reliability unconfirmed

Matches a documented, current Safari/iOS platform limitation, not
obviously a code bug: iOS enforces a much smaller Cache Storage quota
(~50MB) than Android/desktop and aggressively evicts cached data after
inactivity. Real developer reports describe offline working right after
an online visit, then failing later with no code change in between.

**Hardening applied:** `/` is precached during the service worker's
`install` step itself (not just on next successful navigation), closing
a gap where a device adds the PWA to Home Screen while online but never
completes a full navigation before going offline.

**Still not confirmed:** whether this fully resolves the symptom on a
real device long-term, vs. being bounded by iOS's cache eviction over
time (a platform-level ceiling code can't fix). If still flaky, check:
site actually added via Share → Add to Home Screen (not just a Safari
tab/bookmark); a fresh online visit happened after the relevant deploy;
airplane-mode test happens soon after that online visit; Private
Browsing wasn't used (Service Workers are fully disabled there).

---

## 10. What's shelved / explicitly not built

- Skeleton loaders, pull-to-refresh, dark mode — deprioritized, still
  on the table.
- Outlet master list (managed, like sales reps) — explicitly decided
  against; outlet stays free text.
- Stock-on-hand quantity tracking — explicitly decided against for now.
- Multi-language toggle, GPS visit tracking, beat/route planning,
  approval workflows — discussed as bigger-effort ideas common in
  commercial SFA/DMS tools, explicitly deprioritized for current team
  size.
- HTTP→HTTPS server-side redirect — flagged as worth doing, not yet
  done (see bug history: service worker registration silently fails
  entirely on `http://` since Service Workers require a secure
  context).

---

## 11. Open item: "web/desktop view sucks"

Reported once, no specifics gathered — not diagnosed, not fixed. One
known real tradeoff that's a plausible part of it: the bottom tab bar's
`≥768px` override turns it into a plain static horizontal row sitting
right under the search bar — a "bottom nav" pattern awkwardly
repurposed for desktop rather than something actually designed for a
wide screen. Worth a screenshot before assuming this is *the* answer.

---

## 12. Incident: git tracking the database caused a near data-loss

`gtm_catalog.db`, `uploads/sale_price_catalog.xlsx`, and
`__pycache__/*.pyc` had all been committed to git early on — none of
these should ever be tracked (a live database changes on nearly every
request; `.pyc` files regenerate automatically). This caused `git pull`
to fail with "local changes would be overwritten" repeatedly. While
troubleshooting, `git reset --hard` was run against the **first
commit**, which rewound the entire local working copy including the
tracked `gtm_catalog.db`, putting a near-empty database in place on the
live server (`sqlite3.OperationalError: no such table: products`, the
catalog page 500ing in production).

**Recovered without permanent data loss** — GitHub's `origin/main`
still had all real commits (a local reset never touches the remote),
and the live server's actual `gtm_catalog.db` on disk hadn't been
touched by the reset itself, only by the *subsequent pull*. A manual
backup was taken before any further git operations, then restored.

**Root fixes applied:**
- `git rm --cached` + `.gitignore` for `gtm_catalog.db`, `uploads/`
  (whole folder), `__pycache__/` — git no longer has any opinion about
  their contents, so this specific failure mode can't recur.
- `backup_db.py` added: uses SQLite's built-in `.backup()` API (not a
  plain `shutil.copy()`, which risks grabbing the file mid-write and
  producing a corrupted backup). 14-day rotating history in `backups/`.

**What this protects against, and what it doesn't:** protects against
bad git operations / accidental overwrites / human error (a recent
backup can be copied back over the live DB). Does **not** protect
against losing the entire PythonAnywhere account/disk, since backups
and live data live on the same server. True off-server protection
(periodically pushing `backups/` to a separate git branch, never
`main`) was discussed, not yet built.

**GitHub auth note:** password auth for `git push` over HTTPS is
deprecated — a Personal Access Token (GitHub → Settings → Developer
settings → Tokens (classic), `repo` scope) is used as the password
instead. `git config --global credential.helper store` avoids retyping
it on every push.

---

## 13. Open item as of the last working session: pricing/margin columns

The business side wants to move to a single "master" spreadsheet
workflow: download the latest catalog export → the business owner adds
margin-tracking columns (Base Price / cost, plus Retail%, Wholesale%,
B2C% margins) and a B2C selling price → re-upload, so Excel stays the
single source of truth for pricing decisions even though SQLite is the
live backend.

**Confirmed decisions so far:**
- `Base Price` (cost to acquire from supplier) and `B2C` (a separate
  selling-price channel) are **manual entry fields** — not derived.
- The three margin percentages (**Retail %**, **Wholesale %**,
  **B2C %**) should be **formulas**: `(Selling Price − Base Price) /
  Base Price`, not hardcoded numbers, so editing Retail/Wholesale/B2C/
  Base Price auto-recalculates them.
- `Description` and `Supplier` must be preserved in any merged
  template (needed by the app / internal automation), even though the
  business owner's working file currently drops them.

**Not yet done:** none of this exists in the actual app yet — no
`base_price`, `b2c`, or margin-% columns in the `products` table
schema, and `config.REQUIRED_COLUMNS` doesn't reference them. A
prototype merged Excel **template** (not yet wired into the app) was
built as a first step: read-only product data (from the website
export) + blue editable pricing cells (Base Price/Retail/Wholesale/
B2C) + gray formula cells (the three margin %), with borders/number
formatting, recalculated via LibreOffice to bake in real formula
values. Next step (not started): decide whether these become real
`products` table columns (schema migration + `REQUIRED_COLUMNS` update
+ `import_excel_into_db()` changes) or stay a separate
business-side-only spreadsheet layer that never touches the database.

---

## 14. If you're picking this up in a new chat

- This document alone should be enough for planning, architecture
  discussion, and understanding *why* things are built the way they
  are — you generally don't need to re-upload files just to ask "how
  does X work" or "what would changing Y affect."
- **Do upload actual current source files when you want something
  edited** — paste this doc for context, plus whichever specific files
  the task touches. For anything involving `sw.js`, always get the
  current file fresh (see §0, §7 gotcha #7 — it drifts fast and
  independently of chat sessions).
- Don't paste an old `config.py` over the live one (placeholder hash
  risk, §6.3). Don't paste an old `sw.js` over the live one (§6.7).
- **Never suggest re-tracking `gtm_catalog.db`, `uploads/`, or
  `__pycache__/` in git** — see §12 for exactly why that's dangerous,
  not just inconvenient.
- If extending `db.py` with a new function that logs a side-effect
  (activity, audit, etc.) alongside a real INSERT, watch the
  `last_insert_rowid()` ordering — see §7 gotcha #12 for exactly how
  this bit the project once already.
- State the specific feature or bug to tackle next — this doc is
  context, not a task list.
/home/noskillreal/Downloads/MIGRATION_COMPLETE.md
