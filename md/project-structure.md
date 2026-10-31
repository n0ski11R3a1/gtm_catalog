# GTM Catalog — Project Structure and System Guide

This document describes the repository as it exists in the current
workspace. It is intended to give a new developer or AI assistant enough
context to understand the application, trace a feature through its layers,
and avoid breaking important data and offline behavior.

## 1. System at a glance

This is a Flask application for a product catalog, price lookup, product
image gallery, and field-sales order capture. SQLite is the operational
source of truth. Excel is an import/export format, not the database used
to serve requests.

```text
Browser / installed PWA
  ├── Flask/Jinja pages and JSON endpoints (app.py)
  ├── browser JavaScript (static/js/)
  ├── service worker and Cache Storage (static/sw.js)
  └── local/session storage for cart, filters, and notification read state
             │
             ├── SQLite database (db.py -> gtm_catalog.db)
             ├── Product image files (static/product-images/)
             └── Web Push provider (push.py -> pywebpush)
```

### Main user-facing areas

- **Catalog (`/`)**: searchable/filterable product list, price/status
  information, copy-price action, and order-cart controls.
- **Product detail (`/product/<slug>`)**: public product image,
  description, prices, and the same copy/order controls.
- **Gallery (`/gallery`, `/gallery/<category>`)**: public visual browser
  with category collages, image tiles, a full-screen swipe viewer, and
  offline page/image support. It is browsing-only; the List and detail
  pages are the order-entry surfaces.
- **Order submission (`/order/submit`)**: public endpoint used by sales
  reps using the browser/PWA cart; server validates the rep and obtains
  current prices from SQLite.
- **Offline field sales (`/api/mobile/*`)**: reps can sign in to the
  catalog PWA, download a price snapshot, save encrypted orders and
  standalone outlet visits while offline, and explicitly sync them when
  connected.
- **Outlets (`/admin/outlets`)**: admin-managed outlet profiles, areas,
  shop type, aliases, linked order history, and field visit history.
  Offline capture can match aliases and requires an explicit choice when
  a name maps to multiple outlets.
- **Sales report (`/admin/sales-report`)**: date- and rep-filtered order
  totals, status counts, per-product quantities, and daily activity.
- **Admin (`/admin/...`)**: login-protected catalog, image, order, sales
  rep, export, catalog-audit, and bulk-import management.
- **Updates & Status**: public catalog modal showing the activity feed,
  service-worker version, last offline sync, push subscription toggle,
  and a force-refresh action.

## 2. Important data and behavioral rules

1. **SQLite is live data.** `gtm_catalog.db` is the working database.
   The uploaded `uploads/sale_price_catalog.xlsx` is the most recent
   import file/template, not the source read on every page request.
2. **Product ID has two forms.** The stored/business ID is commonly
   `GTM - 0001`; public URLs and image filenames strip spaces to
   `GTM-0001`. Lookups are case-insensitive and space-insensitive.
3. **Internal product PK is not Product ID.** `products.id` is used in
   admin routes and in client cart rows. `product_id` is the external
   catalog identity.
4. **Generated IDs are monotonic.** `id_counter` reserves IDs and is
   repaired against IDs in products and retained history. Viewing the
   add form uses `peek_next_product_id()` (does not consume an ID);
   actual creation uses `get_next_product_id()`.
5. **Reusing an OOS Product ID is a new product generation.**
   `reuse_product_id()` increments `products.generation` and logs a
   replacement event. Activity and price history are intended to be
   scoped to `(product_id, generation)` for the current product.
6. **Order prices are server-authoritative.** The browser cart may keep
   price/name snapshots for display, but only sends internal product PK
   and quantity. `db.create_order()` reads current prices from SQLite.
7. **Order history is snapshot-based.** Orders store a rep label, the
   captured outlet name and, when resolved, an `outlet_id`. Order items
   store business ID, name, unit prices, quantity, and line totals at
   order time. Offline order prices use an immutable catalog snapshot.
8. **Product images are files, not blobs.** The canonical image file is
   `static/product-images/<NORMALIZED-ID>.webp`; generated gallery
   thumbnails live under `static/product-images/thumbnails/`. The DB
   `has_image` field is metadata; routes also resolve the file on disk.
9. **Rep authentication is separate from admin authentication.** Admin
   pages check `session["admin"] is True` through `require_admin()`. The
   legacy online browser cart remains public and validates the selected
   active rep. The offline-ready PWA requires the rep's configured mobile
   username/password and uses the authenticated `/api/mobile/*` APIs.
10. **The service worker must not cache authenticated data.** Product,
    gallery, and public catalog pages have offline support; login/admin
    navigations are sent to the network. Authenticated mobile API requests
    bypass Cache Storage. Static asset changes require coordinating the
    service-worker cache version and its precache list.

## 3. Repository map

### Python application

| File | Responsibility |
|---|---|
| `app.py` | Flask initialization, request validation, image route helpers, all page/API/admin routes, Excel upload/export, and Jinja filters. Calls `db.py` for SQLite access, `services.py` for reusable image operations, and `push.py` for notifications. |
| `db.py` | SQLite schema creation/migrations and every database read/write. Returns product dictionaries in the legacy title-case format expected by templates and `/api/prices`. |
| `services.py` | Shared product-form validation and reusable image validation, WebP conversion, persistence, deletion, and thumbnail generation. |
| `push.py` | Sends Web Push payloads to stored browser subscriptions; prunes malformed/expired subscriptions and batches multiple changes into one notification. |
| `config.py` | Resolves app paths, upload/image limits, database path, required Excel headers, and authentication/VAPID configuration. It reads `GTM_*` environment variables where supported. **Do not copy credential/key values into this document or source-control.** Configure secrets for the deployment environment. |
| `generate_vapid_keys.py` | One-time utility that prints a VAPID key pair for environment/deployment configuration. Rotating keys invalidates existing browser push subscriptions. |
| `conftest.py` | Adds the repository root to `sys.path` so pytest can import the top-level modules. |
| `requirement.txt` | Python dependencies: Flask/Gunicorn, pandas/openpyxl, Pillow, Web Push/VAPID/cryptography packages, Werkzeug, and pytest. The manifest is singular (`requirement.txt`). |
| `python_scripts/compress_img.py` | Standalone local image-batch compressor. Its source/output folder is hard-coded to a Windows path; it is not part of the Flask upload pipeline. |

There is no `backup_db.py` in the current repository inventory, despite
older handoff text describing one. Database backup files currently present
under `backups/` are local runtime artifacts.

### Templates

All HTML views extend `templates/base.html` unless otherwise noted.

| Template | Used for |
|---|---|
| `base.html` | Shared document shell, PWA metadata, Bootstrap/font/CSS includes, flash messages, admin sidebar layout, shared catalog JavaScript, and service-worker registration. Hides the admin workspace shell on the public catalog endpoint. |
| `index.html` | Public List/catalog view: category rail, search/sort/status filters, product cards, prices, price-change badges, copy action, per-card quantities, desktop order summary, tab bar, order modal, and Updates & Status modal. Loads order, precache, and status scripts. |
| `product_detail.html` | Public product detail card, optional image/placeholder, description, price/status, copy/order controls, shared tab bar and order modal. |
| `product_not_found.html` | Friendly 404 page for a product URL that no longer resolves. |
| `gallery.html` | Gallery Level 1: searchable category cards with image collages, product and OOS counts, shared tab bar/modal. |
| `gallery_category.html` | Gallery Level 2: paginated category image grid, search/status filters, full-screen swipe viewer markup, shared tab bar/modal. |
| `_glass_tabbar.html` | Shared public List/Gallery/Cart/Admin navigation. List and Gallery selection also drive the stored preferred landing mode. |
| `_order_modal.html` | Shared cart/order dialog, rep dropdown, outlet entry, totals, item controls, and status/error messages. |
| `login.html` | Admin sign-in form and password visibility toggle. |
| `admin.html` | Admin overview: today's order stats, catalog health, quick links, recent orders, source-file status, exports/audit/bulk-image actions, and thumbnail-cache clearing. |
| `admin_products.html` | Product table with client-side search/status filtering and Edit/Image/Delete actions. |
| `admin_images.html` | Image library table, image summary cards, search and has/missing-image filters. |
| `product_form.html` | Add/edit product fields, categories, optional image upload, Product ID generation/reuse picker, edit-only price history chart/table, and client-side numeric/category UI. |
| `product_image_form.html` | Focused per-product image replace/delete form. |
| `admin_upload.html` | Excel import instructions, required columns, audit/export links, and import-result/reused-ID warnings. |
| `admin_upload_images.html` | ZIP image upload rules and per-file processing results. |
| `admin_catalog_audit.html` | Duplicate Product ID checks for SQLite and an uploaded workbook, cleaned preview, and cleaned-workbook download. |
| `admin_reps.html` | Managed sales-rep list with active status, mobile-account configured state, and edit/delete actions. |
| `rep_form.html` | Add/edit sales rep code/name, active toggle, and optional mobile username/password. |
| `admin_orders.html` | Order list, date filter, summary statistics, per-rep totals, view/delete actions, and mobile price-review flags. |
| `admin_order_detail.html` | One order's rep/outlet/time/status, line snapshots/totals, mobile price-review metadata, and status update form. |
| `admin_outlets.html` | Outlet directory, order-derived sales/visit summary, and new outlet form. |
| `admin_outlet_detail.html` | Outlet profile editing, alias management, linked visit history, and recent orders. |
| `admin_sales_report.html` | Date/rep-filtered submitted/fulfilled totals, performance by rep, product activity, and daily order totals. |
| `admin_inventory.html` | Current product balances, manual receipt/issue form, and filtered append-only movement ledger. |
| `admin_stock_counts.html` | Draft/approved physical-count history and count creation. |
| `admin_stock_count_detail.html` | Snapshot quantities, physical-count entry, variance review, and final approval/reconciliation controls. |
| `admin_purchases.html` | Supplier purchase-order list, receipt progress, and estimated order values. |
| `admin_purchase_order_form.html` | Supplier details and multi-product order creation. |
| `admin_purchase_order_detail.html` | Ordered/received quantities, partial receipt entry, and unreceived-order cancellation. |

### Browser assets

| File | Responsibility |
|---|---|
| `static/js/app.js` | Compatibility shim only; the catalog behavior was split into other scripts. |
| `static/js/catalog-filters.js` | Catalog search/category/status filtering, sorting (recent/price/name), copy-price action, catalog event binding, and safe no-op behavior on pages without the catalog grid. Exposes functions used by inline template handlers. |
| `static/js/catalog-state.js` | Saves/restores catalog search, sort, category, status, and scroll position in `sessionStorage`. Gallery reuses the same key for search/status continuity. |
| `static/js/price-badges.js` | Converts price-change timestamps/direction into readable age text, arrows, and age color classes. |
| `static/js/order.js` | Persistent localStorage draft cart; quantity controls; cart modal and desktop summary; rep/outlet validation; idempotency key; online POST to `/order/submit`; dispatches signed-in reps to the encrypted offline-capable flow. |
| `static/js/offline-orders.js` | Authenticated rep sign-in; AES-GCM encrypted IndexedDB vault for bearer token, catalog snapshot, orders, standalone visits, delivery assignments/outcomes, and handovers; outlet matching and optional GPS; explicit retry-safe sync. HTTPS and Web Crypto are required. Encryption protects stored data at rest, not against same-origin script compromise or a compromised device. |
| `static/js/language.js` | Persistent English/Myanmar/Chinese selector and local UI translation for common catalog and admin actions; leaves marked user-entered content unchanged and translates dynamic UI additions. |
| `static/js/admin.js` | Excel upload box drag/drop, filename display/extension check, and submit spinner. It targets the upload-page form markup. |
| `static/js/gallery.js` | Stored List/Gallery preference and root redirect; category filtering; category-page search/status filtering and shared catalog-state persistence; viewer slide building, navigation, history/back-button close, and swipe gesture. |
| `static/js/precache.js` | After catalog load, fetches `/api/prices` and `/api/product-images`, then pre-caches missing product pages, image files, `/gallery`, and category pages into dedicated buckets with concurrency capped at five. Displays progress and records the last successful sync in localStorage. |
| `static/js/status.js` | Service-worker version request over `MessageChannel`; unread bell dot and last-seen marker; activity list/summary rendering and cursor pagination; push subscribe/unsubscribe UI; last-sync display; cache-clearing force refresh. |
| `static/sw.js` | PWA service worker, current static cache version `gtm-catalog-v41`, separate persistent product-page/image/gallery cache buckets, request routing/offline strategies, authenticated mobile API cache bypass, version messaging, and push notification display/deep links. |
| `static/manifest.json` | Installable PWA name, start URL/scope, display mode, theme/background, orientation, and icon references. |
| `static/css/style.css` | Legacy/general styles for the earlier catalog design and shared components. |
| `static/css/admin.css` | Legacy admin component/layout styles. |
| `static/css/ui-reboot.css` | Current final design-system layer: shared tokens/base, public catalog, gallery/product details, dialogs/status panels, admin workspace, responsive breakpoints, and reduced-motion handling. Loaded after legacy styles to override them. |
| `static/icon-192.png`, `static/icon-512.png` | PWA/app and push-notification icons. |

### Runtime and local data

- `gtm_catalog.db`: SQLite operational database. It is ignored by Git.
- `uploads/sale_price_catalog.xlsx`: most recent uploaded import workbook.
  The `uploads/` directory is ignored by Git.
- `backups/*.db`: local SQLite snapshots currently present; ignored by Git.
- `static/product-images/`: real product images and generated thumbnail
  folder. The whole product-image directory is ignored by Git; deploy and
  back it up separately from tracked code.
- `.gitignore`: excludes the database, uploads, backups, product images,
  configuration, logs, caches, and a named local token file. Keep
  operational data/secrets out of commits.
- `md/PROJECT_HANDOFF.md`: older continuity handoff; useful for design
  history and known gotchas, but some details are stale.
- `md/gtm_catalog_project_path.md`: older, longer handoff/history. It
  currently contains unresolved Git conflict markers; treat it as
  historical notes, not authoritative executable configuration.

## 4. Database schema and relationships

`db.init_db()` creates tables and applies additive migrations at app
startup. SQLite access is centralized in `db.py`.

| Table | Purpose and important fields |
|---|---|
| `products` | Catalog rows. `id` is the internal autoincrement PK; `product_id` is the unique business ID; remaining fields include name, UPC, unit, retail/wholesale prices, category/status, `generation`, description, supplier, and `has_image`. |
| `price_history` | Append-only retail/wholesale price changes keyed by business Product ID and generation, with old/new values, timestamp, and source (`manual` or import source). |
| `activity_log` | Unified public notification/activity rows: product added/replaced, price changed, out of stock, back in stock. Has Product ID, generation, name, details, source, and timestamp. Current-feed queries join to the current product generation. |
| `id_counter` | One-row, monotonically increasing `next_number` for auto-generated GTM IDs. |
| `sales_reps` | Admin-managed rep `code`, `name`, and active flag. Mobile login adds nullable `mobile_username`, Werkzeug `mobile_password_hash`, and SHA-256 `mobile_token_hash`; API reads do not return password/token hashes. Active reps with mobile credentials can be assigned delivery work. |
| `orders` | Order header: rep label, outlet name and optional `outlet_id`, status, retail/wholesale totals, timestamp, browser `idempotency_key`, mobile `rep_id`, `client_order_id`, `catalog_snapshot_id`, `price_mismatch`, `sync_received_at`, and optional `delivery_rep_id`. |
| `order_items` | Order line snapshots: order ID, product identity/name, unit retail/wholesale, quantity, line totals, and the internal `product_pk`/generation snapshot used to post delivery stock safely. Legacy lines are linked only when a generation-1 product identity is unambiguous. |
| `outlets` | Structured customer identity with name, area, note, shop type, optional credit limit/GPS fields, and timestamps. Historical free-text orders are linked on the first schema migration when identity is unambiguous. |
| `outlet_aliases` | Alternate outlet spellings used for offline and online order matching. Collisions across outlets are allowed but never guessed automatically. |
| `outlet_visits` | Order-linked and standalone field visits, with rep identity, visit type/time, optional note and consented GPS coordinates/accuracy. Standalone mobile retries are unique by `(rep_id, client_visit_id)`. |
| `catalog_snapshots` | Immutable JSON product-and-outlet payloads keyed by SHA-256 `snapshot_id`. Mobile orders resolve prices against the saved product payload, not client-supplied prices. Snapshots must be retained while offline devices may still have queued orders against them. |
| `push_subscriptions` | One row per browser push endpoint, with endpoint and public subscription keys. Endpoint is unique and the keys are refreshed on conflict. |
| `stock_balances` | Current quantity per internal product row; updated in the same transaction as each inventory movement. |
| `stock_movements` | Append-only manual receipts/issues, purchase receipts, and approved-count adjustments. Stores product identity/generation snapshots, signed quantity delta, balance after movement, reference, note, actor, and timestamp. |
| `stock_counts` / `stock_count_lines` | Draft/approved physical counts with product identity and book-quantity snapshots. A count changes stock only when fully populated and approved; approval and ledger adjustments commit atomically. |
| `purchase_orders` / `purchase_order_items` | Supplier order header and line snapshots, ordered/received quantities, unit costs, status, invoice/reference, and creator. Partial receipts add stock through the same inventory ledger; cancelled/received order history is preserved. |
| `delivery_assignments` | Append-only assignment/reassignment history; only one assignment per order is active at a time. Orders are assigned to active reps with configured mobile credentials. |
| `delivery_attempts` / `delivery_attempt_items` | Idempotent repeated delivered/failed field outcomes with rep, optional consented GPS, note, cash collected, and delivered product/quantity snapshots. Each successful attempt issues only its delivered quantities; the order stays assigned until complete, while a failed attempt cancels the undelivered balance without changing stock. |
| `delivery_handovers` / `delivery_handover_items` | Rep-declared cash handovers linked to the exact delivered cash collections, then verified with actual cash received, verifier, timestamp, and discrepancy preserved. Each collection can be included in only one handover. |

### Schema and history notes

- A partial unique index prevents duplicate nonblank Product IDs. A
  second unique index normalizes case and ordinary spaces to match lookup
  behavior.
- Reusing an OOS product updates its existing product row and increments
  generation, rather than creating a new PK. Product images are removed
  during this path. Old generation history is retained for audit.
- Manual price changes and import price changes write both
  `price_history` and `activity_log` through `_log_price_change()`.
- Stock transitions produce separate `out_of_stock` or `back_in_stock`
  activity events.
- Orders and line items are not live foreign-key snapshots of current
  product/rep labels; deleting or renaming a product/rep does not rewrite
  historical order content.
- Mobile snapshots include the internal product PK, product identity,
  order-time retail/wholesale values, status, and generation; the public
  API omits Supplier and generation. Sync flags `price_mismatch` when the
  current product is missing/replaced or its price/status/generation
  differs from the saved snapshot.
- One active bearer token is stored per rep; re-login replaces its prior
  token. Deactivation, credential changes, and mobile-account removal
  revoke the token. The rep's mobile username is case-insensitively
  unique.
- Product import's `replace=True` behavior marks products absent from the
  workbook as Out Of Stock, rather than deleting them.
- Inventory is separate from the catalog's editorial In Stock/Out Of Stock
  status. Receipts and issues update the ledger transactionally; issues
  cannot make a balance negative. Product rows with stock history cannot
  be deleted, and a discontinued ID cannot be reused while it has stock.
- A stock count snapshots all current products and book quantities. Every
  product must have a finite, non-negative physical count before approval.
  Approval reconciles against the latest balance, writes immutable
  `count_adjustment` movements, and cannot be repeated or edited afterward.
- Catalog orders do not yet alter inventory. Stock is not deducted until a
  delivery/fulfilment workflow is integrated, avoiding a false assumption
  that an order was physically dispatched.
- Supplier purchase orders can be created with multiple products and
  partial receipts. Each receipt updates the purchase line and stock ledger
  atomically; over-receipt is rejected. Orders with any received stock
  cannot be cancelled. Supplier returns, supplier settlements, and
  finance approval are not yet implemented.
- The configured Excel required headers include Base Price, B2C, and
  Supplier. `validate_pricing_rows()` contains additional cost/price
  validations, but the current `/upload` route does not call that helper.
  Do not assume those row-level checks are enforced by the upload path.

## 5. Python function map

### `app.py`

#### Startup and shared helpers

- `validate_excel(path)`: reads the `Product Catalog` sheet, checks
  required headers, rejects blank IDs, and rejects duplicate IDs after
  removing spaces and ignoring case.
- `login_required()`: returns whether the current request has an
  authenticated admin session.
- `require_admin()`: returns a redirect response to login when the
  session is not admin; otherwise returns `None`.
- `get_json_payload()`: returns a parsed JSON object or `{}` when the
  request body is absent, invalid, or not an object.
- `product_photo_slug()` / `build_product_image_filename()`: normalize
  business ID and form the canonical `.webp` filename.
- `is_allowed_image_file()` / `validate_image_size()`: validate uploaded
  image extension/MIME and configured byte limit.
- `compress_to_webp()`: resize to configured max width, flatten
  transparency onto white, and encode WebP.
- `generate_thumbnail()`: create/read the product's generated WebP
  thumbnail from the matching source image.
- `prepare_uploaded_product_image()`: validate an uploaded image and
  return compressed bytes plus canonical target filename.
- `save_product_image()`: save the prepared WebP image under its
  canonical name.
- `find_product_image()`: return the first matching static-relative
  image path across supported extensions, or `None`.
- `get_category_covers()`: choose up to four real image files for a
  gallery category and count all/OOS products in that category.
- `notify_activity_since(before_id)`: retrieve activity rows written
  after a snapshot and send them as one push batch.
- `product_form_to_dict(form)`: trim and convert submitted product form
  values to DB field names; invalid numeric values become zero.
- `product_record_to_db_payload(product, overrides=None)`: map a product
  row back into the database's snake_case write format.

#### Route inventory

The route table in §6 gives URL/method/auth. The handler responsibilities
are:

- `home()`: loads all products/categories/active reps and renders the
  List catalog.
- `product_detail(slug)`: resolves normalized business ID, renders
  details/image/reps, or returns a custom 404 page.
- `gallery_home()`: builds category collage/count data from products and
  renders Gallery Level 1.
- `product_thumbnail(product_id)`: serves cached thumbnail, generates
  it on demand, returns 404 if source image is missing, and logs/returns
  500 for generation errors.
- `gallery_category(category)`: validates category, clamps page number,
  slices category products, resolves their images, and renders Level 2.
- `api_prices()`: serializes all product dictionaries as JSON. This
  includes the DB `Supplier` field even though the public page does not
  display it.
- `api_product_images()`: returns only Product IDs with a resolvable
  image and the corresponding static-relative paths for precaching.
- `_sanitize_price_history_for_public_api(rows)`: selects only the
  explicitly allowed public history fields.
- `_format_activity_event(event)` / `_format_relative_time(timestamp)`:
  add human-readable event title/message/link/time for activity API
  responses.
- `api_price_history()`: returns public-safe history, optionally scoped
  to one `product_id`.
- `api_activity()`: returns activity feed, true latest ID, and
  cursor-pagination `has_more`.
- `api_activity_summary()`: groups a bounded activity batch by event
  type, with counts and up to five sample events per group.
- `api_push_public_key()`: exposes the VAPID public key needed by the
  browser; the public key is not the private signing key.
- `api_push_subscribe()` / `api_push_unsubscribe()`: validate/store or
  remove browser push subscriptions.
- `order_submit()`: validates rep/outlet/items, checks submitted rep
  against active rep labels, delegates price calculation and persistence
  to `db.create_order()`, and returns order ID and totals.
- `get_mobile_rep()`: extracts a bearer token, hashes it with SHA-256,
  and resolves only an active rep with a matching stored token hash.
- `api_mobile_login()`: validates username/password types, checks the
  admin-provisioned password hash and active rep status, generates a
  random bearer token, and stores only its SHA-256 hash.
- `api_mobile_catalog()`: requires a valid mobile bearer token and
  returns a public-safe catalog snapshot from `db.create_mobile_catalog_snapshot()`.
- `api_mobile_sync()`: authenticates rep, validates a batch of at most
  50 orders and each UUID/snapshot/outlet/time, then delegates each order
  to snapshot-priced `db.create_mobile_order()` and returns per-order
  results.
- `admin_outlets()` / `admin_outlet_detail()` /
  `admin_outlet_edit()` / alias handlers: admin-only outlet directory,
  profiles, alias management, linked orders, and order-derived visits.
- `admin_sales_report()`: validates a date range and optional historical
  rep label, then renders date-bounded performance summaries.
- `admin_inventory()` / `admin_inventory_movement()`: display current
  stock and the ledger, and record a validated manual receipt or issue.
- `admin_stock_counts()` / `admin_stock_count_create()` /
  `admin_stock_count_detail()` / `admin_stock_count_save()` /
  `admin_stock_count_approve()` / `admin_stock_count_delete()`: create
  and manage physical counts; only a complete draft can be approved to
  reconcile stock.
- `admin_purchases()` / `admin_purchase_order_new()` /
  `admin_purchase_order_detail()` / `admin_purchase_order_receive()` /
  `admin_purchase_order_cancel()`: create supplier orders, record partial
  receipts, and cancel only unreceived orders.
- `sw()`: serves `static/sw.js` at `/sw.js` with service-worker scope
  allowed at `/`.
- `login()` / `logout()`: verify configured username/password hash,
  establish the admin session with the configured actor name, or clear it.
- `admin()`: dashboard stats, today's order aggregate, recent orders,
  imported workbook timestamp/name.
- `admin_upload_images()`: processes a ZIP of GTM-named images; skips
  non-images, checks ID/file size, converts to WebP, updates image flag,
  and renders per-file results.
- `admin_clear_thumbnail_cache()`: removes generated `.webp` thumbnails
  and redirects to the dashboard.
- `admin_products()` / `admin_images()`: render product management list
  or image library; image page resolves actual file presence.
- `admin_product_add()`: renders generated-ID preview and reusable OOS
  IDs; on POST validates fields and either reserves a new ID or reuses
  an OOS ID, optionally saves image, logs activity, and redirects.
- `admin_product_edit()`: validates and saves product fields, handles
  optional image replacement/deletion, and provides current-generation
  price history.
- `admin_product_images()`: focused upload/replace/delete surface for a
  product image.
- `admin_product_delete()`: deletes a product only if it has no inventory
  ledger history; it does not itself call the image-file deletion helper.
- `admin_reps()` / `admin_rep_add()` / `admin_rep_edit()` /
  `admin_rep_delete()`: manage rep list and active status.
- `admin_orders()`: loads order rows, aggregates, rep breakdown, date
  list, and selected-day filter.
- `admin_order_detail()`: renders one order and its item snapshots.
- `admin_order_status()` / `admin_order_delete()`: update status or
  remove order and item rows.
- `export_prices_excel()`: admin-only workbook export of current
  products, excluding internal DB PK.
- `export_price_history_excel()`: admin-only export of current-generation
  price history.
- `admin_catalog_audit()`: shows duplicate IDs in DB, audits uploaded
  workbook for duplicates/blanks, previews cleaned rows, or exports a
  cleaned workbook.
- `upload_page()` / `upload()`: admin-only Excel import form and
  processing; upload checks workbook sheet/headers/blank/duplicate IDs,
  replaces the saved import file, imports via upsert, and pushes actual
  activity changes.
- `cleannum(value)`: removes trailing `.0` from whole-number form
  values.
- `sqlitedatetime(value)`: formats SQLite timestamp strings for display.
- `datetimeformat(value)`: formats Unix timestamps (used for workbook
  modification time).
- `timeago(value)`: renders SQLite timestamp strings as relative time.

### `db.py`

All of these functions use SQLite connections from `get_db_connection()`;
the module owns schema access and transactions.

#### Connection, schema, and record mapping

- `get_db_connection()`: opens configured SQLite DB and uses
  `sqlite3.Row`.
- `init_db()`: creates/migrates tables and indexes, repairs the ID
  counter, and bootstraps an empty database from the configured Excel
  file without flooding initial activity.
- `_row_to_dict(row)`: maps database columns into the public/template
  product dictionary shape, including Supplier, description, image flag,
  and latest-price metadata.
- `_log_activity(...)`: inserts one activity row inside the caller's
  transaction.
- `_format_price_change_details(...)`: formats changed retail/wholesale
  values for activity messages.
- `_format_activity_message(...)`: returns title/message pairs by event
  type and source.
- `_log_price_change(...)`: if retail or wholesale changed, inserts
  history and a matching activity event.
- `_log_status_change(...)`: logs only real status transitions as
  out-of-stock/back-in-stock events.

#### Push-subscription persistence

- `add_push_subscription(...)`: inserts or refreshes endpoint keys.
- `remove_push_subscription(endpoint)`: deletes a subscription.
- `get_all_push_subscriptions()`: returns stored subscriptions for
  delivery.

#### Product reads and activity/history

- `product_count()`: total product-row count.
- `get_all_products()`: returns products in PK order with latest price
  change/direction fields.
- `get_categories()`: sorted distinct non-empty categories.
- `get_product(product_pk)`: one product by internal PK with price
  metadata.
- `get_product_by_business_id(slug)`: one product by space-insensitive,
  case-insensitive business ID.
- `get_price_history(product_id, limit=100)`: history for a business ID
  and its current generation.
- `get_recent_price_changes(limit=50)`: latest current-generation
  history across products.
- `get_recent_activity(limit=30, before_id=None)`: current-generation
  activity feed, newest first, optionally using an ID cursor.
- `get_latest_activity_id()`: highest raw activity ID for unread checks
  and write-before/write-after push detection.
- `get_activity_after(after_id, limit=500)`: current-generation events
  written after a snapshot, oldest first.
- `get_stats()`: product/category/in-stock/OOS counts for admin.

#### Catalog audit and Product ID lifecycle

- `detect_duplicate_product_ids(rows)`: identifies duplicate IDs after
  stripping spaces and uppercasing.
- `build_cleaned_catalog_frame(df)`: removes blank IDs and keeps the
  first row per normalized ID.
- `get_duplicate_product_ids_in_db()`: duplicate scan of current DB
  products.
- `_highest_historical_gtm_number(conn)`: scans products, history, and
  activity for the highest GTM numeric ID.
- `_seed_id_counter_if_missing(conn)`: initializes/repairs the next ID
  against historical usage.
- `peek_next_product_id()`: read-only next-ID preview.
- `get_next_product_id(conn=None)`: reserves and returns a new ID,
  optionally in a caller-owned transaction.
- `get_available_product_ids()`: lists OOS rows whose IDs are eligible
  for explicit reuse.
- `reuse_product_id(product_id, data)`: verifies an existing OOS row,
  deletes its old image, increments generation, replaces product fields,
  logs replacement, and returns the stable internal PK; refuses reuse
  while the product has stock or an open purchase order.

#### Sales reps and orders

- `get_all_reps()` / `get_active_reps()`: return all reps or only those
  eligible for new orders.
- `get_rep(rep_id)`: lookup one rep.
- `rep_label(rep)`: formats exact saved/display label, normally
  `CODE (Name)`.
- `add_rep(...)` / `update_rep(...)` / `delete_rep(...)`: CRUD for reps.
- `create_mobile_catalog_snapshot()`: canonicalizes the full catalog,
  hashes/persists its immutable product JSON snapshot, and returns public
  product data without Supplier or generation plus the outlet/alias
  directory for authenticated offline selection.
- `get_mobile_rep_by_username(username)` /
  `get_mobile_rep_by_token_hash(token_hash)`: resolve mobile credentials
  or active token identity.
- `set_mobile_token_hash(rep_id, token_hash)`: persist one active token
  hash for the rep.
- `_normalise_outlet_text()` / `_resolve_outlet_id()`: normalize outlet
  spellings, resolve one unique name/alias, create an outlet for a new
  name, and leave ambiguous names unlinked rather than guessing.
- `get_all_outlets()` / `get_outlet()` / `get_public_outlets()`: load
  outlet summaries, profiles, order/visit history, or public-safe
  outlet data for authenticated snapshots.
- `create_outlet()` / `update_outlet()` / `add_outlet_alias()` /
  `delete_outlet_alias()`: validate and persist outlet profile changes.
- `create_mobile_order(...)`: transactionally resolves products against
  a saved catalog snapshot, aggregates quantities, saves snapshot prices,
  compares product generation/price/status to current catalog, stores
  review metadata, resolves the outlet, records an order-derived visit
  and optional GPS fix, and returns the existing order on a repeated
  rep/UUID.
- `create_order(rep_name, cart_items, outlet_name="", idempotency_key=None)`:
  returns an existing order for a repeated key, otherwise validates each
  PK/quantity, loads current prices, inserts order and line snapshots,
  and returns `(order_id, total_retail, total_wholesale)` or a null tuple
  if there are no valid lines.
- `get_all_orders(date_filter=None)`: newest-first order list with item
  count, optionally limited to a date.
- `get_order_summary(date_filter=None)`: order, outlet, rep, retail, and
  wholesale totals for an optional date.
- `get_orders_by_rep(date_filter=None)`: per-rep count/outlet/retail
  aggregation.
- `get_sales_report(start_date, end_date, rep_name=None)`: returns
  date-bounded overall/per-rep order counts and submitted/fulfilled
  retail totals, daily activity, and product quantities excluding
  cancelled orders.
- `get_order_rep_names()`: distinct saved rep labels for historical
  report filtering.
- `get_order_dates()`: distinct order dates newest first.
- `get_order(order_id)`: returns order header and ordered line items.
- `update_order_status(...)`: changes header status.
- `delete_order(order_id)`: explicitly deletes line items then header.

#### Product writes and Excel import

- `add_product(data)`: inserts product generation 1, immediately captures
  the product row ID before activity insertion, logs product-added event,
  and returns internal PK.
- `update_product(product_pk, data)`: updates product values and logs
  price/status changes in the same transaction.
- `delete_product(product_pk)`: refuses deletion if stock movement history
  or an open/partial purchase order references the product; otherwise
  removes the row and its zero balance.
- `validate_pricing_rows(path)`: validates supplier/base-price/B2C and
  retail/wholesale relationship rules for in-stock rows; OOS rows are
  exempt. Present helper, not wired into current `/upload` handler.
- `backup_before_import()`: snapshots the SQLite DB with SQLite's backup
  API to a timestamped file under `backups/`.
- `import_excel_into_db(path, replace=True, source=..., log_activity=True)`:
  normalizes import columns, upserts by Product ID, preserves optional
  description/Supplier when the workbook omits those columns, records
  price/status/add events, auto-assigns IDs for blank rows if called
  directly, warns on historical-ID reuse, and optionally marks missing
  products OOS. Returns row count and reuse warnings.

#### Inventory and physical counts

- `_validated_stock_number(...)`: rejects booleans, invalid/non-finite
  numbers, and quantities outside the allowed non-negative/positive range.
- `_stock_balance(conn, product_pk)`: returns the current persisted
  balance, defaulting to zero.
- `_record_stock_movement(...)`: appends a product-identity snapshot to
  the ledger and updates its balance in the same caller-owned
  transaction.
- `record_stock_movement(...)`: serializes receipt/issue writes with
  `BEGIN IMMEDIATE`, rejects negative resulting balances, and returns
  the movement ID.
- `get_inventory_products()` / `get_stock_movements(...)`: read current
  balances and newest-first movement history.
- `create_stock_count(...)`: snapshots every current product and its
  book balance into a draft count.
- `get_stock_counts()` / `get_stock_count(...)`: return count progress,
  detail rows, and current book quantities.
- `save_stock_count(...)`: validates and saves the full count-line
  mapping while the count remains a draft.
- `approve_stock_count(...)`: requires every line, checks product
  generations, writes only the net differences as movements, and seals
  the count atomically.
- `delete_stock_count(...)`: removes draft counts only.
- `create_purchase_order(...)`: validates supplier/order details and
  product lines, snapshots current product identities/costs, and stores an
  open order without changing stock.
- `get_purchase_orders()` / `get_purchase_order(...)`: return order
  summaries, line totals, and ordered/received quantities.
- `receive_purchase_order(...)`: validates partial/full receipt amounts,
  rejects over-receipts or product-generation changes, and writes purchase
  receipts plus line/status changes atomically.
- `cancel_purchase_order(...)`: cancels only open/partial orders with no
  received quantities.

#### Delivery assignment and cash handovers

- `get_delivery_board()`: returns open orders, configured mobile reps, and
  handovers for the admin delivery workspace.
- `assign_delivery_order(...)`: verifies the order and active mobile rep,
  records assignment history, and atomically updates the order's assignee.
- `get_mobile_deliveries(...)`: returns only a rep's active assignments,
  with remaining quantities, cash collectible against delivered goods, and
  unhanded cash collections.
- `record_mobile_delivery_attempt(...)`: validates assignment, idempotency,
  remaining quantities, product generations, cash limits, and stock. Each
  attempt's stock movements and order status update commit atomically.
- `create_mobile_delivery_handover(...)`: validates ownership and exact
  unhanded delivery attempts, then records an idempotent cash-handover
  batch.
- `get_delivery_handovers(...)` / `verify_delivery_handover(...)`: provide
  admin review, one-time verification, received cash, and discrepancy
  history.
- `delete_rep(...)`: refuses to remove reps referenced by delivery
  assignments, attempts, or handovers; deactivate those accounts instead.

### `services.py`

- `product_photo_slug()` / `build_product_image_filename()`: shared ID
  normalization and canonical image filename.
- `validate_product_form(data)`: requires a name and requires supplier
  for In Stock product submissions.
- `is_allowed_image_file(file_obj)` / `validate_image_size(file_obj)`:
  image extension/MIME and maximum-byte checks.
- `compress_to_webp(file_obj, max_width=None, quality=None)`: resizes,
  normalizes color/transparency, and returns encoded WebP bytes.
- `prepare_uploaded_product_image(file_obj, product_id)`: combines
  validation and conversion, returning bytes and canonical filename.
- `save_product_image(file_obj, product_id, image_dir=None)`: writes
  prepared WebP to the chosen/default image directory.
- `delete_product_image_files(product_id, image_dir=None,
  thumbnails_dir=None)`: removes canonical image and generated
  thumbnail if present; does not alter DB.
- `save_image_upload(...)`: validates/prepares/saves through the shared
  upload helper and returns saved filename.
- `generate_thumbnail(...)`: creates a configured-width WebP thumbnail
  from the first supported matching source image.

`app.py` also retains local image helpers, notably
`find_product_image()` (runtime file discovery), `product_thumbnail()`
generation wrapper, and `compress_to_webp()` (used by ZIP import). Avoid
assuming every image helper is defined only in `services.py`.

### `push.py`

- `_send_one(subscription, payload_json)`: validates subscription shape,
  invokes `pywebpush`, logs delivery failures, and removes 404/410 or
  malformed endpoints.
- `send_to_all(title, body, url="/")`: sends one payload to every stored
  subscription; a failing endpoint does not stop the rest.
- `notify_new_activity(events)`: sends a specific product deep link for
  one activity event or one generic catalog summary for a batch.

## 6. HTTP route reference

“Public” means no admin login is required by the route. It does not mean
the endpoint has per-rep identity or access control.

| Method | Path | Access | Behavior |
|---|---|---|---|
| GET | `/` | Public | List catalog. |
| GET | `/product/<product_id_slug>` | Public | Detail page; normalized business ID; custom 404 on miss. |
| GET | `/gallery` | Public | Category collage browser. |
| GET | `/gallery/<category>` | Public | Category image grid; `?page=` pagination. |
| GET | `/thumbnails/<product_id>.webp` | Public | Serve or generate gallery thumbnail. |
| GET | `/api/prices` | Public | Product JSON for app and Power Query; currently includes Supplier in returned product dictionaries. |
| GET | `/api/product-images` | Public | Existing image paths for precache client. |
| GET | `/api/price-history` | Public | Public-safe price history; optional `?product_id=`. |
| GET | `/api/activity` | Public | Activity feed; `limit` capped at 100, optional `before_id` cursor; returns latest raw ID and `has_more`. |
| GET | `/api/activity/summary` | Public | Activity grouped by event type; `limit` capped at 500, optional `before_id`. |
| GET | `/api/push/public-key` | Public | Returns VAPID public key. |
| POST | `/api/push/subscribe` | Public | Validates/stores endpoint and browser keys. |
| POST | `/api/push/unsubscribe` | Public | Removes endpoint if supplied. |
| POST | `/order/submit` | Public | Validates rep/outlet/cart then creates or returns idempotent order. |
| POST | `/api/mobile/login` | Public credentials | Rep username/password → bearer token and rep identity; a new login replaces the rep's previous token. |
| GET | `/api/mobile/catalog` | Mobile bearer | Creates/reuses and returns public-safe catalog and outlet/alias data plus immutable SHA-256 product snapshot ID. |
| POST | `/api/mobile/sync` | Mobile bearer | Accepts 1–50 orders; each order gets an independent result. Prices use the referenced saved snapshot; selected outlet IDs are verified and ambiguous free-text matches are not guessed. |
| POST | `/api/mobile/visits/sync` | Mobile bearer | Sync standalone outlet visits in retry-safe batches, with optional validated GPS. |
| GET | `/api/mobile/deliveries` | Mobile bearer | Returns only the caller's assigned open deliveries (no supplier or cost fields) and cash collections not yet included in a handover. |
| POST | `/api/mobile/deliveries/sync` | Mobile bearer | Accepts up to 50 retry-safe delivery-quantity or failed-delivery outcomes. Each successful attempt posts only its delivered quantities atomically; the order stays open until complete, while failure cancels the remaining balance. |
| POST | `/api/mobile/handovers/sync` | Mobile bearer | Records rep-declared cash handovers linked to exact, unhanded delivery cash records. |
| GET | `/sw.js` | Public | Serves worker with root scope header. |
| GET/POST | `/login` | Public | Admin username/password-hash login. |
| GET | `/logout` | Session | Clears session. |
| GET | `/language/<code>` | Public | Saves `en`, `my`, or `zh` in a first-party cookie and safely returns to a local page. |
| GET | `/admin` | Admin | Dashboard. |
| GET | `/admin/products` | Admin | Product management table. |
| GET/POST | `/admin/products/add` | Admin | Add product or reuse OOS Product ID. |
| GET/POST | `/admin/products/<int:product_id>/edit` | Admin | Edit product and view price history. |
| GET/POST | `/admin/products/<int:product_id>/images` | Admin | Focused product image management. |
| POST | `/admin/products/<int:product_id>/delete` | Admin | Delete product DB row. |
| GET | `/admin/images` | Admin | Image library and filters. |
| GET/POST | `/admin/upload-images` | Admin | Bulk image ZIP upload. |
| POST | `/admin/clear-thumbnail-cache` | Admin | Delete generated thumbnails. |
| GET | `/admin/reps` | Admin | Sales rep manager. |
| GET/POST | `/admin/reps/add` | Admin | Add rep. |
| GET/POST | `/admin/reps/<int:rep_id>/edit` | Admin | Edit rep and active flag. |
| POST | `/admin/reps/<int:rep_id>/delete` | Admin | Delete rep. |
| GET | `/admin/orders` | Admin | Order list and optional `?date=YYYY-MM-DD` filter. |
| GET | `/admin/deliveries` | Admin | Assign open orders to mobile-enabled reps and review same-day cash handovers. |
| POST | `/admin/deliveries/<int:order_id>/assign` | Admin | Assign or reassign a new order to an active rep with mobile credentials. |
| POST | `/admin/deliveries/handovers/<int:handover_id>/verify` | Admin | Record actual cash received and verify a pending rep handover; discrepancy remains recorded. |
| GET/POST | `/admin/outlets` | Admin | Outlet directory and profile creation. |
| GET | `/admin/outlets/<int:outlet_id>` | Admin | Outlet profile, aliases, linked visits, and recent orders. |
| POST | `/admin/outlets/<int:outlet_id>/edit` | Admin | Update outlet name, area, type, note, and optional credit limit. |
| POST | `/admin/outlets/<int:outlet_id>/aliases` | Admin | Add an alternate outlet spelling. |
| POST | `/admin/outlets/<int:outlet_id>/aliases/<int:alias_id>/delete` | Admin | Remove an outlet alias. |
| GET | `/admin/sales-report` | Admin | Sales totals and performance by rep/product; accepts `from`, `to`, and `rep` query parameters. |
| GET | `/admin/inventory` | Admin | Current balances and movement ledger; optional `?product_id=` filters the ledger. |
| POST | `/admin/inventory/movement` | Admin | Record a positive receipt or issue; rejects invalid quantities and insufficient stock. |
| GET | `/admin/stock-counts` | Admin | List draft and approved stock counts. |
| POST | `/admin/stock-counts/create` | Admin | Snapshot current products/balances into a new draft count. |
| GET | `/admin/stock-counts/<int:count_id>` | Admin | View/edit a draft count or review an approved count. |
| POST | `/admin/stock-counts/<int:count_id>/save` | Admin | Save non-negative physical quantities for the draft. |
| POST | `/admin/stock-counts/<int:count_id>/approve` | Admin | Require every line, atomically reconcile balances, and seal the count. |
| POST | `/admin/stock-counts/<int:count_id>/delete` | Admin | Delete a draft count only. |
| GET | `/admin/purchases` | Admin | Supplier order list with order totals and receiving progress. |
| GET/POST | `/admin/purchases/new` | Admin | Create a supplier order with one or more products; no stock is added until received. |
| GET | `/admin/purchases/<int:purchase_order_id>` | Admin | Purchase-order detail and partial receiving form. |
| POST | `/admin/purchases/<int:purchase_order_id>/receive` | Admin | Post partial/full received quantities into purchase lines and stock ledger atomically. |
| POST | `/admin/purchases/<int:purchase_order_id>/cancel` | Admin | Cancel only an open order with no received quantities. |
| GET | `/admin/orders/<int:order_id>` | Admin | Order detail. |
| POST | `/admin/orders/<int:order_id>/status` | Admin | Change order status. |
| POST | `/admin/orders/<int:order_id>/delete` | Admin | Delete order and line items. |
| GET | `/admin/export/prices.xlsx` | Admin | Current catalog workbook download. |
| GET | `/admin/export/price-history.xlsx` | Admin | Price history workbook download. |
| GET/POST | `/admin/catalog-audit` | Admin | DB/Excel duplicate audit and cleaned workbook download. |
| GET/POST | `/upload` | Admin | Excel catalog upload/import. |

## 7. Mobile order API status

The server-side `/api/mobile/login`, `/api/mobile/catalog`,
`/api/mobile/sync`, `/api/mobile/visits/sync`,
`/api/mobile/deliveries`, `/api/mobile/deliveries/sync`, and
`/api/mobile/handovers/sync` endpoints and their database support remain
in `app.py`/`db.py`. They authenticate rep credentials, issue bearer
tokens, return immutable catalog snapshots, accept snapshot-priced
orders and retry-safe standalone visits, list only the caller's assigned
deliveries with undelivered quantities, and sync quantity-based delivery
attempts and cash handovers. A partial delivery keeps the order assigned;
the server rejects quantities beyond the remaining balance and posts
stock movements atomically. The catalog
PWA uses these endpoints for authenticated offline field operations.
Pending order, visit, delivery, handover data, cached assignments, and
the bearer token are AES-GCM encrypted in IndexedDB using a
non-extractable device key; the service worker bypasses cache for
`/api/mobile/*`. HTTPS/Web Crypto are required, and browser-side
encryption is not protection from a compromised device or same-origin
script. The Flutter/Android client and its APK build tooling remain
removed.

## 8. Browser behavior and offline strategy

### Local/session state

- Cart and its idempotency token use localStorage, so drafts survive
  reloads. The submitted JSON excludes browser price values.
- For reps signed in to Offline field sales, the catalog snapshot,
  bearer token, queued orders, and standalone visits are encrypted in
  IndexedDB. Orders are durably queued before the working cart is
  cleared. Sync is explicit, batches at most 50, keeps failed records
  queued with visible errors, and safely retries a lost response using
  the same client UUID.
- The signed-in PWA must refresh the catalog while connected before a
  field visit. Offline prices reflect that immutable snapshot; the server
  reports a price mismatch if the current catalog changed before sync.
- Known outlet names and aliases are cached in the encrypted catalog
  snapshot. A spelling matching multiple outlets requires an explicit
  selection; unknown names are created as outlets on successful sync.
- Reps may explicitly opt in to attaching device GPS to an offline order.
  Coordinates are validated by the server, retained in the encrypted
  queued order until sync, then stored on the linked visit. Without the
  checkbox, the browser does not request location. The first valid visit
  coordinate fills an outlet's location only if it was previously unset.
- Reps may also record an encrypted standalone visit without creating an
  order. Visit type, optional note, outlet matching, and optional GPS are
  preserved offline; ambiguous outlet names are rejected unless the rep
  selects a matching outlet, and sync is idempotent.
- Admins assign new orders to reps with active mobile credentials. A
  signed-in rep downloads only their own open assignments, with previously
  delivered quantities removed. Delivery quantities are sealed into the
  encrypted queue before sync; each successful attempt atomically issues
  only those quantities from stock. The order stays assigned until all
  quantities are delivered, at which point it becomes fulfilled. A failed
  delivery requires a reason and cancels any remaining quantity without
  changing stock. Partial delivery is supported, including repeated
  attempts and idempotent retries.
- Cash collections are attached to a delivered attempt and cannot exceed
  the unpaid value of delivered items or the total order value. Reps queue
  a cash handover linked to the exact
  unhanded collections; an admin records actual cash received. Any
  declared/received difference remains visible. This is delivery cash
  custody tracking, not general customer payments, bank, or cashbox.
- Reps with delivery assignments or history cannot be deleted; deactivate
  their account instead so historical delivery and handover records remain
  attributable.
- Catalog search/sort/category/status/scroll use sessionStorage.
- Gallery preference and push subscription are local/browser state.
- English, Myanmar, and Chinese can be selected from public or admin
  navigation. `/language/<code>` stores the browser preference in a
  first-party cookie for one year and returns only to a validated local
  path. Local translation covers common catalog/admin interface strings;
  unmatched strings remain English. User-entered product and outlet values
  marked `translate="no"` remain unchanged.
- Last-seen activity ID and last offline sync timestamp are stored
  client-side; there is no user-account-specific notification state.

### Service-worker cache buckets

- `gtm-catalog-v41`: versioned static assets and public catalog root.
  This bucket is refreshed/removed on cache-version changes.
- `gtm-product-pages-v1`: stable product detail page cache.
- `gtm-product-images-v1`: stable product image cache.
- `gtm-gallery-pages-v1`: stable gallery page cache.

Gallery/product pages and product images use a cache-first response with
background refresh when an entry already exists; a first uncached request
waits for the network. `/` uses the same return-cached-and-refresh pattern.
Other navigations (including `/login` and `/admin/...`) are network-only
with no intentionally cached authenticated fallback. `/api/*` is
network-first with cache fallback. Other static requests are cache-first.

`precache.js` and `sw.js` manually duplicate the stable product/gallery
cache names. Keep them identical. When changing CSS/JS or the static asset
list, bump `CACHE_NAME` in `sw.js`; also ensure every required script is
listed in `STATIC_ASSETS`. The status-panel force refresh deletes all
caches except the persistent product-page bucket, then reloads.

Offline support remains subject to browser storage eviction and platform
behavior. iOS Home Screen installation and real-device testing are
particularly important; older handoff notes discuss known iOS limitations.

### ERP feature migration status

The `concepts/` prototype is a larger Flask ERP and is still being
migrated incrementally; it is not the live application. Implemented in
the current app are authenticated encrypted offline order and standalone
visit capture, structured outlet profiles/aliases and visit history with
explicit optional GPS capture, date-filtered sales reporting, an
auditable stock ledger, manual stock receipts/issues, approval-based
physical stock counts, supplier purchase orders with partial receiving,
quantity-based partial-delivery stock issue with delivery assignment and
cash handover verification, and an English/Myanmar/Chinese interface
language selector with local translations for common UI labels. Still not
implemented from the prototype
are supplier returns/settlements and finance approval, general customer
payment collection, cashbox/expenses/profit,
configurable role/permission administration, a general audit workflow,
backup/restore management, and broader analysis/tax/advisory pages.

## 9. Configuration and operation

`config.py` derives data paths from the repository directory. Relevant
environment names include:

- `GTM_ENV` (`production` requires the deployment settings below)
- `GTM_SECRET_KEY`
- `GTM_ADMIN_USERNAME`
- `GTM_ADMIN_PASSWORD_HASH`
- `GTM_VAPID_PRIVATE_KEY`
- `GTM_VAPID_PUBLIC_KEY`
- `GTM_VAPID_CLAIM_EMAIL`

The config module also sets image upload limits, accepted file formats,
WebP dimensions/quality, gallery page size, required Excel headers, and
local SQLite/Excel paths. In development, legacy fallback credentials
remain for local compatibility. Set `GTM_ENV=production` in deployment:
the app then refuses to start unless all six credential/VAPID settings
above are supplied. Use deployment-managed values and HTTPS (required
for service workers and encrypted offline orders). Never include actual
credentials or private keys in prompts, handoff docs, screenshots, or
commits.

The documented development/test command is:

```sh
pytest
```

Dependencies are listed in `requirement.txt`. Production entrypoint is
the Flask `app` object in `app.py`; Gunicorn is included as a dependency.
PythonAnywhere deployment may serve `/static/` directly using its static
file mapping while `/sw.js` remains a Flask route.

## 10. Tests

`tests/test_mobile_sync.py` covers mobile login/auth, Supplier exclusion,
snapshot-price preservation, price-mismatch flags, idempotent retry, and
token revocation when a rep is disabled or changes credentials.

`tests/test_outlets.py` covers order/visit linkage, standalone visit
sync/idempotence, alias resolution, explicit same-name outlet selection,
no-guess behavior for ambiguous names, opt-in GPS persistence, invalid
GPS rejection, and authenticated outlet admin pages.
`tests/test_sales_report.py` covers status totals, cancelled-line
exclusion, date filters, and report rendering.

`tests/test_inventory.py` covers receipt/issue ledger safety, complete
stock-count approval and idempotence, admin access checks, product-history
deletion/reuse guards, and purchase-order partial receiving/cancellation.
`tests/test_delivery.py` covers assignment/auth boundaries, atomic stock
issue and retry behavior, quantity-based partial deliveries and completion,
failed delivery, overcollection rejection, cash-handover
idempotence/verification, and delivery-rep deletion guards.
`tests/test_language.py` covers language preference persistence, safe
return redirects, supported-language fallback, and public/admin selector
rendering.
`tests/test_config_security.py` covers required production configuration
and development-only fallback behavior.

`tests/test_image_upload_helpers.py` covers:

- malformed push-subscription pruning/delivery continuation;
- admin image and product quick-filter rendering;
- admin-session check;
- environment-driven config values;
- Product ID to canonical image name mapping;
- WebP image preparation and ignoring client filenames;
- Excel blank/duplicate ID rejection;
- duplicate detection and cleaned workbook data;
- thumbnail generation and ZIP image upload;
- product price-change metadata and time formatting;
- catalog price-change badge markup.

`conftest.py` makes top-level modules importable from tests. Tests that
use the app/database should be mindful that `app.py` calls `db.init_db()`
on import and the configured DB path is the repository-local database
unless test setup overrides it.

## 11. Source-of-truth caveats for a future assistant

These are observations about the checked-out code, not instructions to
silently change behavior:

- `md/PROJECT_HANDOFF.md` says the worker is v21, but the current
  `static/sw.js` declares `gtm-catalog-v41` and has persistent gallery and
  product-image caches.
- The old `md/gtm_catalog_project_path.md` contains unresolved conflict
  markers. Do not use it as a canonical current-state description.
- Older notes mention `backup_db.py`; that file is absent from the
  current repository file inventory. `db.backup_before_import()` exists,
  and local backup snapshots exist, but verify actual scheduled-backup
  setup separately.
- `db.get_all_products()` and related lookup queries calculate the
  latest-price badge metadata by `product_id` without applying
  `generation`; the dedicated price-history/activity methods do apply
  generation filters. If changing generation behavior, check those badge
  subqueries too.
- `/api/prices` is public and serializes Supplier even though Supplier is
  described in the form as internal and is not displayed in the public
  UI. Treat this as an intentional/current API contract unless the owner
  asks to change it.
- `admin_product_delete()` removes the DB row but does not remove its
  image file. The database delete helper likewise does not manage files.
- Image files and database/workbook contents are ignored runtime data,
  not portable source assets. A clone alone does not contain the live
  catalog, uploaded workbook, or full product image library.
- `validate_pricing_rows()` is implemented but is not called by the
  current upload route. The upload route does validate sheet/header and
  normalized Product ID blank/duplicate conditions.
- The image utilities exist both in `services.py` and, for some
  request-specific behavior, in `app.py`. Inspect their call sites before
  consolidating; app-side ZIP handling and runtime file lookup have
  distinct responsibilities.
- The current worktree already has unrelated/uncommitted changes in the
  service worker, base template, UI reboot stylesheet, and handoff file.
  A future assistant should inspect `git status` before modifying or
  reverting anything.

## 12. Suggested feature tracing

- **Catalog card/filter behavior:** `templates/index.html` →
  `static/js/catalog-filters.js` / `catalog-state.js` →
  `app.home()` → `db.get_all_products()`.
- **Product details/images:** `app.product_detail()` →
  `db.get_product_by_business_id()` + `find_product_image()` →
  `product_detail.html`; upload paths are admin image routes →
  `services.py`.
- **Excel import:** `admin_upload.html` → `app.upload()` →
  `validate_excel()` → `db.import_excel_into_db()` →
  price/activity logging → `notify_activity_since()` → `push.py`.
- **Order creation:** `_order_modal.html` and product-card data →
  `order.js` localStorage payload → `/order/submit` →
  active-rep validation → `db.create_order()` → `orders` and
  `order_items`; admin views use `db.get_all_orders()`/`get_order()`.
- **Mobile field sales:** `/api/mobile/login` and `/api/mobile/catalog`
  issue authenticated credentials/snapshots; `/api/mobile/sync` writes
  snapshot-priced orders; `/api/mobile/visits/sync` writes standalone
  visits → mobile helpers in `app.py`/`db.py` → `catalog_snapshots`,
  `orders`, `order_items`, and `outlet_visits`. The PWA is the field
  client; no Android client is included.
- **Activity bell:** product writes in `db.py` →
  `activity_log` → `/api/activity` or `/api/activity/summary` →
  `status.js`; optional OS notification flows through `push.py` and
  service-worker `push`/`notificationclick` handlers.
- **Offline changes:** page/asset additions usually need coordinated
  template script includes, `precache.js` URL enumeration, service-worker
  request strategy/cache bucket, static asset list, and a cache version
  bump.
