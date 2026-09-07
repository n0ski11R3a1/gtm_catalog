# GTM Product & Price Catalog — Project Handoff

**This file is regenerated in full every time something changes.** Paste
this plus your current files into a new chat and say "here's my current
app, continue from here" — it should be enough to pick up with zero
missing context.

Last regenerated after: the **Updates & Status feature** shipped -
notification bell (new products + price changes, with a red "unread"
dot), live app version display via service worker messaging, and a
hard-refresh button that respects the product-pages cache instead of
nuking it. Two real bugs caught in testing and fixed before shipping -
one genuinely serious (`add_product()` silently returning the wrong id).
Full details in §14. `sw.js` bumped to v21.

---

## 1. What this app is

A Flask web app (PythonAnywhere) with three faces:

1. **Public catalog** (`/`) — mobile-friendly, installable (PWA) price
   list. Search, filter by category/status, sort by price. Sales reps
   also build an order ("cart") here and submit it — no login needed.
   Each card links to a **product detail page** (`/product/<id>`, also
   public) showing an image, description, and the same pricing/order
   actions.
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
├── backup_db.py                  Automated database backup (NEW, see
│                                   §12) - safe live-DB snapshot via
│                                   sqlite3's .backup() API, 14-day
│                                   rotation. Run manually or via a
│                                   PythonAnywhere Scheduled Task.
│
├── gtm_catalog.db                 SQLite database (auto-created) - NOT
│                                    tracked in git anymore (see §13)
├── backups/                       Rotating DB snapshots from
│                                    backup_db.py - NOT tracked in git
│
├── templates/
│   ├── base.html                    Shared layout: navbar (admin pages
│   │                                  only - see §5, deliberately hidden
│   │                                  on the catalog page now), flash
│   │                                  messages, PWA meta, fonts
│   ├── index.html                   Public catalog + order/cart UI.
│   │                                  Header is a single-row glass topbar
│   │                                  (brand + search + filter toggle)
│   │                                  over category chips; primary nav is
│   │                                  a bottom glass tab bar
│   │                                  (Catalog/Cart/Admin) that shrinks on
│   │                                  scroll-down, expands on scroll-up
│   │                                  (mobile only). Each card has an eye
│   │                                  icon linking to that product's
│   │                                  detail page. Loads precache.js.
│   ├── product_detail.html          Product detail page (NEW). Image (or
│   │                                  placeholder), description, same
│   │                                  price/order UI as a catalog card -
│   │                                  reuses order.js/app.js unmodified
│   │                                  via matching data-* attributes.
│   ├── product_not_found.html       404-style page for an unmatched
│   │                                  product slug (NEW)
│   ├── _glass_tabbar.html           Bottom tab bar, extracted into a
│   │                                  shared partial (NEW) - included
│   │                                  from both index.html and
│   │                                  product_detail.html so they can't
│   │                                  drift out of sync
│   ├── _order_modal.html            Order/cart modal, same reasoning -
│   │                                  shared partial (NEW)
│   ├── login.html                   Admin login (yours, untouched by me)
│   ├── admin.html                   Admin dashboard (yours, untouched)
│   ├── admin_products.html          Manage Products: list/search/edit/delete
│   ├── product_form.html            Add/Edit Product + Price History tab
│   │                                  + Description textarea (admin-
│   │                                  editable, optional)
│   ├── admin_orders.html            All orders: date filter, summary
│   │                                  stats, per-rep breakdown, list
│   ├── admin_order_detail.html      One order's line items + status
│   ├── admin_reps.html              Sales team list (add/edit/delete)
│   └── rep_form.html                Add/edit a single sales rep
│
├── static/
│   ├── css/
│   │   ├── style.css                Public catalog + product detail
│   │   │                              page. Ocean/Leaf/Aqua/Mint palette,
│   │   │                              Space Grotesk/Plus Jakarta
│   │   │                              Sans/JetBrains Mono fonts,
│   │   │                              price-tag product cards, "Liquid
│   │   │                              Glass" (backdrop-filter blur) on
│   │   │                              the topbar + bottom tab bar
│   │   └── admin.css                Admin panel styling (unchanged
│   │                                  visual style from before the
│   │                                  catalog redesign, on purpose)
│   ├── js/
│   │   ├── app.js                   Catalog search/filter/sort/copy-price
│   │   │                              (filterCatalog() no-ops safely on
│   │   │                              pages without a #catalogGrid, e.g.
│   │   │                              the product detail page)
│   │   ├── admin.js                 Excel upload drag-and-drop UX
│   │   ├── order.js                 Sales rep cart: localStorage cart,
│   │   │                              idempotency key, submit flow -
│   │   │                              works unmodified on the product
│   │   │                              detail page too (see §5)
│   │   ├── precache.js              Background offline-precache for
│   │   │                              every product page, with a visible
│   │   │                              progress indicator. Also writes the
│   │   │                              "last sync" timestamp status.js
│   │   │                              reads (localStorage key
│   │   │                              gtm_last_sync_v1) - even on a
│   │   │                              "nothing missing" run.
│   │   └── status.js                 Updates & Status panel (NEW): live
│   │                                   app version via a MessageChannel
│   │                                   round-trip to the active service
│   │                                   worker, last offline sync display,
│   │                                   the notification bell's unread
│   │                                   check + activity feed rendering,
│   │                                   and the hard-refresh flow - see §5
│   ├── product-images/              Product photos (NEW folder, not in
│   │                                  the database) - named to match the
│   │                                  business Product ID with spaces
│   │                                  stripped, e.g. GTM-0001.jpg. No
│   │                                  file = clean placeholder shown
│   │                                  instead, not a broken image.
│   ├── manifest.json                 PWA manifest
│   └── sw.js                        Service worker - YOU own/maintain
│                                       this file. Currently v21.
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
| `description` | TEXT, optional, defaults to `''`. Free text (Burmese or any Unicode needs zero special handling). Admin-editable via `product_form.html`; optional column on Excel upload - **a sheet without this column never wipes existing descriptions**, only a sheet that actually includes it updates them. Shown on the public product detail page. |

### `price_history`
Append-only log of retail/wholesale changes, keyed on the **business**
`product_id` (survives product deletion/recreation). `changed_at` is a
SQLite timestamp **string** (`'2026-07-23 09:12:01'`) - not a Unix
timestamp. `source` is `"manual"` or `"excel_upload"`. Only logged when a
price actually changes.

### `activity_log`
Powers the notification bell. Unified feed of two event types -
`product_added` and `price_changed` - so the UI only queries one table.
| Column | Notes |
|---|---|
| `event_type` | `"product_added"` or `"price_changed"` |
| `product_id` | Business key (survives delete/re-add, same reasoning as `price_history`) |
| `product_name`, `details` | `details` is a short human-readable summary, e.g. `"Retail: 5,000 → 5,500 Ks • Wholesale: 4,500 → 4,800 Ks"` |
| `created_at` | SQLite timestamp string |

Logged automatically from `_log_price_change()` (reuses its existing
"did retail/wholesale actually change" check - one source of truth for
both `price_history` and this table) and from `add_product()` /
`import_excel_into_db()`'s new-row branch. The one-time bootstrap import
on a fresh database passes `log_activity=False` deliberately - without
it, a brand new install would flood the feed with 200+ "product added"
entries on day one instead of starting clean.

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
| `/product/<product_id_slug>` | GET | Product detail page. Slug is the business Product ID with spaces stripped (`GTM-0001`) - matched against the DB's `GTM - 0001` form space-insensitively. 404s to `product_not_found.html` if no match. |
| `/api/prices` | GET | JSON feed, same field names as the original Excel-only version so Excel's "Get Data from Web"/Power Query keeps working unchanged. Now also includes `"Description"` per product (additive - existing Power Query column mappings shouldn't break, but flagging the shape change). |
| `/api/price-history` | GET | JSON feed of price changes, optional `?product_id=` |
| `/api/activity` | GET | JSON feed backing the notification bell: `{"latest_id": N, "events": [...]}`. `?limit=` (default 30, capped 1-100). `latest_id` is always the true max regardless of `limit`, so the client can cheaply check "anything new since I looked" with `?limit=1` instead of pulling the whole feed. |
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

- **The admin navbar is now scoped away from the catalog page on
  purpose:** `base.html`'s dark navbar rendered on *every* page before,
  which meant a logged-in admin saw it stacked directly above the
  catalog's own header - two brands, two menus, doing overlapping jobs.
  Fixed with one condition: `{% if session.get("admin") and
  request.endpoint != 'home' %}`. The catalog page's own bottom glass tab
  bar (Catalog/Cart/Admin) is now the only navigation surface there; the
  full admin navbar still renders normally on every actual `/admin/*`
  page.

- **Bottom tab bar over top nav, for the catalog page specifically:**
  chosen because this is a tool reps reach for one-handed, repeatedly, in
  the field - primary nav (Catalog/Cart/Admin) belongs in the thumb zone
  at the bottom, not the top. Search + category filter are the only
  things that stay up top. Desktop (≥768px) drops the fixed/floating
  bottom-bar behavior entirely and renders it as a normal static bar
  instead - a bottom-pinned pill makes no sense on a wide window.

- **"Liquid Glass" (`backdrop-filter: blur() saturate()`) on the topbar
  and bottom tab bar only** - product cards stay fully opaque on
  purpose. Glass-on-glass gets muddy fast and this app's actual job is
  making price numbers easy to read at a glance; the frosted effect adds
  polish on chrome, not on data.

- **Glass-bar animations use `transform`/`opacity` only, never
  `max-width`/`padding`/`grid-template-rows` where avoidable.** The first
  version of the shrink/expand animations animated layout-triggering
  properties (forces a full reflow every single frame of a transition).
  `transform`/`opacity` run on the compositor instead - the browser can
  animate them without recalculating page layout at all. The bottom tab
  bar's collapse was rewritten from `max-width`+`padding` to `transform:
  scale()` for exactly this reason. `backdrop-filter` blur radius was
  also cut roughly in half on both bars (20px->10px, 22px->10px) since
  it's recomputed continuously during scroll, not just once - it was the
  single largest contributor to scroll jank of everything in the glass
  system.

- **Product images are matched by filename, not stored as a database
  column.** No upload UI to build, no path to validate/sanitize - you
  just drop a correctly-named file into `static/product-images/` and it
  appears. Filename = business Product ID with spaces stripped
  (`GTM-0001.jpg`), checked at *render time* against a small list of
  extensions (`.jpg`/`.jpeg`/`.png`/`.webp`). No match = a clean
  placeholder, never a broken-image icon.

- **Product detail pages are keyed by business Product ID in the URL
  (`/product/GTM-0001`), not the internal `id`.** Chosen for
  shareability/readability over matching the admin routes' internal-ID
  convention. The DB lookup normalizes spaces on both sides
  (`REPLACE(product_id, ' ', '') = ? COLLATE NOCASE`) so `GTM-0001` and
  the canonical stored `GTM - 0001` both resolve to the same product.

- **The product detail page reuses `order.js`/`app.js` completely
  unmodified** - Add to Order, the qty stepper, and Copy Price all work
  identically to a catalog card, because the detail page's wrapper
  carries the exact same `class="card product-card"` and `data-*`
  attributes (`data-pk`, `data-fullname`, `data-retail`,
  `data-wholesale`, `data-unit`) that those scripts already expect. No
  JS had to be touched or duplicated for this to work.

- **The bottom tab bar and order modal were extracted into shared
  partials** (`_glass_tabbar.html`, `_order_modal.html`), included from
  both `index.html` and `product_detail.html`, instead of being
  duplicated. Both have a lot of ids/behavior that has to stay in sync
  (`cartBadge`, `submitOrderBtn`, the rep dropdown, etc.) - exactly the
  kind of thing that's already caused real bugs in this project from
  template/route drift (§6.4). One shared source instead of two
  copies that can quietly diverge.

- **Offline precaching of product pages runs from the page itself, not
  from the service worker's `install` step.** A service worker can't
  easily report live progress back to a page mid-install (would need a
  `postMessage`/`BroadcastChannel` round trip). Instead, `precache.js`
  runs after the catalog page loads, checks what's actually missing from
  cache, and only shows a progress banner if there's real work to do -
  most repeat visits show nothing at all since everything's already
  cached. Uses a small worker-pool pattern (5 concurrent fetches) rather
  than fetching 225+ pages one at a time.

- **Product pages live in their own cache bucket
  (`gtm-product-pages-v1`), separate from the main versioned
  `CACHE_NAME`.** The main cache gets fully wiped and rebuilt on every
  version bump (routine CSS/JS deploys) - with 225+ product pages
  potentially precached, re-downloading all of them on every unrelated
  style tweak would be wasteful and slow, especially on a rep's mobile
  data. The product-pages bucket persists across normal version bumps
  and is only cleared if its own name changes. `sw.js`'s `activate`
  handler was updated to whitelist both cache names instead of just one.

- **Hard Refresh clears the main cache bucket AND forces a real SW
  version check - but deliberately never touches the product-pages
  cache.** A naive "clear every cache and reload" button would force
  re-downloading 200+ offline product pages just to pick up a CSS tweak,
  undoing the whole point of splitting the caches in the first place
  (above). It does two things instead: `registration.update()` to force
  a genuine check for a new `sw.js` right now (bypassing the browser's
  normal throttled check interval), AND directly deletes any cache key
  that isn't `gtm-product-pages-v1` - covering the case where `sw.js`'s
  own version string wasn't bumped but static assets changed anyway
  (forgetting to bump `CACHE_NAME` has been a recurring theme across
  this project's history - see §6, §7's version log).

- **App version is fetched live from the actual running service worker,
  not hardcoded anywhere.** `status.js` opens a `MessageChannel`, sends
  `{type: 'GET_VERSION'}` to `navigator.serviceWorker.controller`, and
  `sw.js` replies with its own `CACHE_NAME` on the reply port. This
  can't drift from reality the way a hardcoded "v21" string in a
  template could.

- **The notification bell's "unread" state is tracked client-side only,
  in localStorage** (`gtm_last_seen_activity_id_v1`) - there's no login
  or per-user concept on the public catalog, so "have I seen this" is
  necessarily a per-device thing, not a per-person one. Checked once on
  page load (not continuous background polling) since this is a tool
  people reopen throughout the day anyway - a fresh check each visit is
  enough without the added complexity of interval management and
  pause-when-hidden logic.

- **`activity_log` is one unified table for two different event types**
  (`product_added`, `price_changed`) rather than two separate tables or
  a UNION query at read time. Simpler read path for the bell (one query,
  one "is there anything new" comparison), and reuses
  `_log_price_change()`'s existing diff-check for what counts as a real
  price change rather than duplicating that logic anywhere.

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

6. **A large CSS/HTML rewrite can silently orphan a selector.** During
   the glass-header rewrite, one edit's "replace this block" boundary
   landed mid-rule instead of on a clean rule boundary - it swapped out
   everything up through `.search-wrap {` but the replacement didn't
   re-open that selector. The result: `position: relative; width: 100%;
   }` was left dangling with no selector owning it. Browsers silently
   discard malformed CSS like that - no console error, no warning - so
   `.search-wrap` had zero positioning rule applied. Its absolutely
   positioned child (`.search-icon`) then anchored to the next real
   positioned ancestor up the tree instead (`.glass-topbar`), landing
   nowhere near the actual search input. Only surfaced visually, in a
   screenshot, well after the fact. **Lesson: after any large
   find/replace across CSS or HTML, verify brace/tag balance
   programmatically (`content.count('{') == content.count('}')`, same
   for `<div>`/`</div>`) - don't rely on visual review alone to catch a
   silently-dropped selector.**

7. **Never assume you're holding the current `sw.js`.** You own that
   file directly (see §7) and have iterated on it independently outside
   any given chat session. Reusing an old local copy as the base for a
   version bump silently threw away real fixes you'd already made
   (`order.js` missing from `STATIC_ASSETS`, the iOS install-time
   precache hardening from §9 - both already live in your v10, both
   absent from a stale local v9). **Always paste your current `sw.js` in
   before it gets touched again**, don't let it be reconstructed from
   memory.

8. **Not every property is safe to `transition`.** `max-width`,
   `padding`, and `grid-template-rows` all force the browser to
   recalculate page layout on every single animation frame - that's the
   actual, measurable cause of noticeable jank, not something vague.
   `transform` and `opacity` don't - they run on the compositor. Any new
   scroll-linked or frequently-toggled animation should default to
   `transform`/`opacity` first, and reach for a layout property only if
   there's genuinely no other way to express the effect.

9. **`request.mode === 'navigate'` only catches real browser navigations
   - not `fetch()` calls from your own page scripts.** `precache.js`
   downloads product pages with a plain `fetch()`, which is NOT
   navigate-mode. The first version of the product-page caching logic in
   `sw.js` lived entirely inside the `navigate` branch and would have
   silently missed every request `precache.js` makes, letting them fall
   through to the generic static-asset handler and get cached in the
   wrong bucket (`CACHE_NAME` instead of `PRODUCT_PAGES_CACHE`). Fixed by
   checking `/product/` by **path**, before the navigate-mode check, so
   it catches both a real page load and a background fetch identically.

10. **`precache.js` and `sw.js` share a cache-name string
    (`PRODUCT_PAGES_CACHE = 'gtm-product-pages-v1'`) that has to match
    EXACTLY, by hand, in both files.** They're separate files that can't
    import a shared constant (a service worker and a page script are
    different execution contexts). If you ever rename one, rename the
    other to match, or the two will silently stop agreeing on where
    product pages live - `precache.js` would download and cache pages
    into a bucket `sw.js`'s fetch handler never looks in.

11. **The Burmese placeholder text in `product_detail.html`
    (`ဤကုန်ပစ္စည်းအတွက် အသေးစိတ်ဖော်ပြချက် မရှိသေးပါ။`, shown when a
    product has no description yet) has not been reviewed by a native
    speaker.** Worth reading over and correcting the wording if anything
    sounds off - it wasn't invented casually, but it also wasn't
    verified against a fluent speaker's judgment.

12. **`copyPrice()` on the product detail page is an unverified
    assumption, not a tested fact.** It relies on `app.js`'s
    `copyPrice(btn)` reading `data-fullname`/`data-unit`/`data-retail`/
    `data-wholesale` off the nearest `.card` - confirmed true for the
    *original* `app.js` from early in this project, but `app.js` itself
    wasn't part of the upload batch when the detail page was built, so
    that behavior was never re-verified against your actual current
    file. If Copy Price misbehaves specifically on the detail page (and
    nowhere else), `app.js` is the first place to check.

13. **`/api/prices` now includes a `"Description"` field for every
    product** (additive - `_row_to_dict()` in `db.py` always includes it
    now). Shouldn't break existing Power Query column mappings since
    it's a new column, not a changed one, but flagging the shape change
    since that endpoint is explicitly relied on to stay stable.

14. **`last_insert_rowid()` reflects whichever INSERT happened most
    recently on that connection - not "the INSERT I meant."** Caused a
    real bug: `add_product()` did `INSERT INTO products` then, before
    capturing `last_insert_rowid()`, ran `_log_activity()` which does
    its own `INSERT INTO activity_log` - so the captured id silently
    became the activity log's row id instead of the product's. Any
    function that captures `last_insert_rowid()` after adding a
    side-effect write (logging, auditing, etc.) needs that capture to
    happen **immediately** after the INSERT it actually cares about,
    before any other INSERT touches the same connection. Caught in
    testing before shipping, not in production - see §8.7.

---

## 7. Service worker (`sw.js`) — owned/maintained by the project owner

Currently **v21**. History:
- v1->v2: stopped caching HTML pages (was causing a login redirect loop
  from stale cached admin/login pages).
- v8->v9: `/` (catalog page only, never `/admin`/`/login`) is now cached
  on every successful online fetch, so there's a real fallback when
  offline. Before this, offline navigation always failed silently because
  nothing was ever cached for it to fall back to.
- v9->v10: also precache `/` during the `install` step itself, not only
  on the next successful navigation - closes an iOS-specific gap where a
  device adds the PWA to its Home Screen while online but never completes
  a full page navigation before going offline (see §9). `order.js` also
  added to `STATIC_ASSETS`.
- v10->v11: header/nav redesign (glass topbar + bottom glass tab bar) -
  `style.css`/`index.html` both changed significantly, bumped so cached
  CSS doesn't linger on devices that visited before the deploy.
- v11->v12: self-applied by the project owner (fixed `.search-wrap`
  orphaned-selector bug in `style.css`, see §6.6) - never independently
  verified in a chat, hence the jump straight to v13 next rather than
  risk building on an unconfirmed base.
- v11->v13: top bar redesign (hero + category strip now collapse on
  scroll, matching the bottom tab bar's behavior) - built from the last
  independently-verified v11, skipping the unverified v12.
- v13->v14: animation performance pass (see §5/§6.8) - `style.css`
  changed again, bumped so cached CSS doesn't linger.
- v19->v20 (v14-v19 happened outside a verified session - see §6.7):
  product detail pages (`/product/<id>`) can now be cached for offline
  use, in a new separate stable cache bucket (`PRODUCT_PAGES_CACHE`, see
  §5) rather than the main versioned cache. `precache.js` added to
  `STATIC_ASSETS`. Fetch handler restructured so `/product/` is matched
  by path before the navigate-mode check (see §6.9).
- v20->v21: added a `message` event listener so a page can ask "what
  version are you running" via `MessageChannel` - backs the Updates &
  Status panel's live version display (see §5).

**Rule going forward: bump `CACHE_NAME` any time CSS/JS/`STATIC_ASSETS`
changes**, or browsers that visited before keep serving old files.

`STATIC_ASSETS` currently includes `style.css`, `admin.css`, `app.js`,
`admin.js`, `order.js`, `manifest.json`.

---

## 8. Bug history

Bugs 1-3 found and fixed by the project owner directly - full detail in
the project owner's own `BUG-LOG.md`, summarized here for continuity.
Bugs 4-5 found via a screenshot and fixed in the same chat session that
introduced them (both were regressions from the header/nav redesign).

1. **Service worker not registering** -> root cause was loading the site
   over `http://` instead of `https://`. Service workers require a
   secure context; `navigator.serviceWorker` doesn't exist at all on an
   insecure origin, so registration silently never runs. Not a code bug.
   Fix: always use the `https://` URL. **Follow-up still worth doing:**
   a server-side HTTP->HTTPS redirect so this can't silently recur for
   anyone landing on a bare `http://` link.

2. **Sticky top bar not sticking** -> `overflow-x: hidden` had been added
   to `body` (an earlier fix, for a mobile zoom-out bug). Any non-visible
   `overflow` value on an ancestor breaks `position: sticky` for
   everything inside it - completely silently, no console warning. Fix:
   moved `overflow-x: hidden` to `html` only; `body` keeps `max-width:
   100%` but no `overflow` rule.

3. **Offline mode never actually worked, even before recent changes** ->
   the navigation fallback pointed at `caches.match('/')`, but `/` was
   never being cached anywhere (`STATIC_ASSETS` only ever listed CSS/JS).
   This was a side effect of the v1->v2 login-loop fix, present since that
   fix shipped - not a recent regression. Fixed in `sw.js` v9: `/`
   specifically (never `/admin`/`/login`) gets cached on every successful
   online fetch.

4. **Search icon floating disconnected above the search input** ->
   introduced by the header/nav glass redesign itself: an edit orphaned
   the `.search-wrap { position: relative; ... }` rule (see §6.6 for the
   full mechanism). Fixed by restoring the selector in `style.css`
   (shipped as part of the v11->v12 bump).

5. **Category chips left one lone chip stranded, centered, on its own
   row on desktop** -> pre-existing intentional behavior (`flex-wrap:
   wrap; justify-content: center` above 768px, so chips wouldn't need
   horizontal scrolling on wide screens), but with 8 categories it left
   the last one visually orphaned in the middle. Fix: `justify-content:
   center` -> `flex-start`, so a wrapped row left-aligns instead of
   floating a single leftover chip in empty space.

6. **Scroll animations felt laggy** -> multiple compounding causes, all
   in `style.css`: `backdrop-filter: blur()` on both glass bars was
   continuously recomputed on every scroll frame (unavoidable cost of
   the effect itself, but the blur radius was higher than it needed to
   be); the bottom tab bar's shrink animated `max-width` and `padding`,
   both of which force a full page layout recalculation on every frame
   of the transition; the top bar had a similar `padding` transition.
   Fix: blur radius roughly halved on both bars (20px->10px,
   22px->10px); tab bar shrink rewritten to use `transform: scale()` +
   `opacity` only (compositor-only, no reflow); removed the top bar's
   redundant `padding` transition; added `contain: layout paint` to both
   bars to scope any remaining layout cost to the bar itself instead of
   the whole page; scroll handler (`index.html`) now tracks last-known
   collapsed state and only touches the DOM when it actually changes,
   instead of writing every scroll frame regardless.

7. **`add_product()` silently returned the wrong id** -> caught in
   testing, before shipping, not in production. Root cause:
   `last_insert_rowid()` reflects whichever INSERT happened most
   recently on that database connection. When `_log_activity()`'s own
   INSERT (into `activity_log`) was added into `add_product()` *after*
   the products INSERT but *before* `last_insert_rowid()` was queried,
   the function started returning the activity log row's id instead of
   the new product's id. Nothing in `app.py` currently uses
   `add_product()`'s return value, so this hadn't caused a visible
   symptom yet - but it was a landmine for any future code that
   reasonably expects the function to return what it says it returns.
   Fix: capture `last_insert_rowid()` immediately after the products
   INSERT, before any other INSERT on that connection can happen.
   Reverified with the exact failing scenario (add a product, confirm
   the returned id actually points at it) after the fix.

8. **Bootstrap import would have flooded the notification feed with
   200+ entries on day one** -> `import_excel_into_db()`'s
   `log_activity` parameter existed specifically to prevent this, but
   the one-time bootstrap call inside `init_db()` wasn't actually
   passing `log_activity=False`. Caught in testing (bootstrap-imported
   225 real products, then checked `activity_log` - found 225 rows
   instead of the expected 0) before it ever shipped. Fixed by passing
   the flag explicitly at that one call site.

---

## 9. iOS offline issue — hardening applied, awaiting real-world confirmation

**Symptom:** service worker / offline mode required an internet
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

**Code hardening applied in v10 (was "proposed, pending confirmation" in
the previous version of this doc - now live):** precache `/` during the
`install` step itself, in addition to the existing "cache `/` on every
successful navigate" runtime approach, wrapped so a failure to fetch `/`
during install doesn't block the rest of `STATIC_ASSETS` from precaching:
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
never actually completes a full page navigation before going offline.

**Still not confirmed:** whether this fully resolves the symptom on a
real iOS device, or whether it's still bounded by iOS's more aggressive
cache eviction over time (a separate, platform-level ceiling this code
change can't fix). Diagnostic steps if it's still flaky:
1. Confirm the site was added to the iOS Home Screen (Share -> Add to
   Home Screen), not just used as a regular Safari tab or bookmark.
2. Confirm a fresh online visit happened after the v10+ deploy (bumping
   `CACHE_NAME` invalidates old cache entries).
3. Test immediately after that online visit (airplane mode within a
   minute or two) to separate "never worked" from "worked, then got
   evicted over time."
4. Confirm Private Browsing wasn't used - Service Workers are fully
   disabled in Safari Private Browsing.
5. If it still fails immediately after a fresh online visit + Home
   Screen install, that points at something more fundamental and would
   need a Mac + cable + Safari's Develop menu to inspect the iPhone's
   Service Worker/Cache Storage state directly.

**Bottom line:** iOS may never be as reliable for full offline access as
Android, per Apple's own current, documented platform restrictions. The
above steps separate "something we can still fix" from "inherent iOS
ceiling."

---

## 10. What's shelved / not built yet

- Bottom tab nav is **done** (see §5/§2) - removed from this list.
- Product detail page + images is **done** (see §5/§2) - removed from
  this list.
- Skeleton loaders, pull-to-refresh, dark mode - still deprioritized,
  still on the table.
- Outlet master list (managed, like sales reps) - explicitly decided
  against; outlet stays free text for now.
- Stock-on-hand quantity tracking - explicitly decided against for now.
- Push notifications, multi-language toggle, GPS visit tracking,
  beat/route planning, approval workflows - discussed as bigger-effort
  ideas common in commercial SFA/DMS tools, explicitly deprioritized as
  premature for current team size.
- HTTP->HTTPS server-side redirect (see §8.1) - flagged as worth doing,
  not yet done.

---

## 11. Open item: "web/desktop view sucks"

Reported the same session as the animation fixes above, **no specifics
gathered yet** - not diagnosed, not fixed. Worth a screenshot next round
rather than guessing at a redesign blind.

One known, real tradeoff already flagged earlier (§5) that's a plausible
part of this: the bottom tab bar's `>=768px` override turns it into a
plain static horizontal row of 3 items (Catalog/Cart/Admin) sitting right
under the search bar - a "bottom nav" pattern awkwardly repurposed for
desktop rather than something actually designed for a wide screen. That's
a real candidate, but shouldn't be assumed as *the* answer without seeing
what's actually happening.

---

## 12. Incident: git tracking the database caused a near data-loss

**What happened, in order:**

1. `gtm_catalog.db`, `uploads/sale_price_catalog.xlsx`, and
   `__pycache__/*.pyc` had all been committed to git at some point
   early on - none of these should ever be tracked (a live database and
   compiled bytecode both change on nearly every request/run, and
   `.pyc` files are regenerated by Python automatically anyway).
2. This caused `git pull` to fail with "your local changes would be
   overwritten" essentially every time, since the live server's copies
   of those files had diverged from whatever was last committed.
3. While troubleshooting, a `git reset --hard` was run against the
   **first commit** - which predates the product detail feature - as
   an attempt to get unstuck. A hard reset rewinds the entire local
   working copy, including `gtm_catalog.db`, since it was tracked. This
   put a near-empty/mismatched database in place on the live
   PythonAnywhere server, causing `sqlite3.OperationalError: no such
   table: products` and the catalog page 500ing in production.
4. Recovered without permanent data loss: `git status` confirmed
   GitHub's `origin/main` still had all 3 real commits (a local reset
   never touches the remote), and the live server's actual
   `gtm_catalog.db` file on disk (238 products, all tables intact) had
   NOT been touched by the reset alone - only by the *subsequent pull*
   attempting to bring in the git-tracked (empty) version. A manual
   backup (`cp gtm_catalog.db gtm_catalog.db.backup`) was taken before
   any further git operations, then restored after pulling.

**Root fixes applied:**

- `git rm --cached` + `.gitignore` for all three: `gtm_catalog.db`,
  `uploads/` (whole folder), `__pycache__/`. None of them can cause a
  pull/reset/merge conflict again, because git no longer has any
  opinion about their contents.
- **`backup_db.py`** added and tested (see §2/§7-equivalent for backup)
  - uses `sqlite3`'s built-in `.backup()` API rather than a plain file
  copy, specifically because copying a live database file with
  `shutil.copy()` risks grabbing it mid-write (a half-finished order, a
  half-written price update) and producing a corrupted backup. Keeps a
  14-day rotating history in `backups/`. Verified: produces a genuinely
  valid, queryable database file (not just a byte copy), and pruning
  correctly removes only backups older than the retention window.

**What this protects against, and what it still doesn't:**

`backup_db.py`'s output lives on the same PythonAnywhere server as the
live app - it protects against exactly this incident (bad git
operations, accidental overwrites, human error), since a recent backup
can just be copied back over `gtm_catalog.db`. It does **not** protect
against the entire PythonAnywhere account/disk being lost, since
backups and live data would go down together. True off-server
protection would mean periodically pushing the `backups/` folder to a
**separate, dedicated git branch** (never `main`) - discussed, not yet
built. Needs `git config --global credential.helper store` set up
first, since a Scheduled Task can't interactively type a Personal
Access Token.

**GitHub auth note:** password auth for `git push` over HTTPS was
deprecated by GitHub - a Personal Access Token (GitHub -> Settings ->
Developer settings -> Tokens (classic), `repo` scope) has to be used as
the password instead. `credential.helper store` avoids retyping it on
every push from the same machine.

---

## 13. If you're picking this up in a new chat

Paste this file plus:
- The current `app.py`, `db.py`, `style.css`, `index.html`,
  `product_detail.html`, `precache.js`, and `status.js` - CSS/HTML drift
  across upload rounds has caused real bugs before (see §6.4, §6.6), and
  this project now has enough interconnected files (§2) that it's worth
  being thorough rather than assuming.
- Don't paste an old `config.py` over your live one, and don't paste an
  old `sw.js` over your live one (you maintain that file yourself now -
  see §6.7).
- **Never suggest re-tracking `gtm_catalog.db`, `uploads/`, or
  `__pycache__/` in git** - see §12 for exactly why that's dangerous,
  not just inconvenient.
- If you're extending `db.py` with a new function that logs a
  side-effect (activity, audit, etc.) alongside a real INSERT, watch the
  `last_insert_rowid()` ordering - see §6.14 for exactly how this bit
  the project once already.
- The specific feature or bug you want to tackle next.
