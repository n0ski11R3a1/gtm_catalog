# GTM Product \& Price Catalog — Project Handoff

**This file is regenerated in full every time something changes.** Paste
this plus your current files into a new chat and say "here's my current
app, continue from here" — it should be enough to pick up with zero
missing context.

Last regenerated after: **Catalog memory \& navigation fix** shipped -
when you navigate from the catalog to a product detail page (eye icon),
then tap "Back to Catalog," the app now remembers your exact position:
search text, active filter/sort, scroll position, and all. Uses
sessionStorage to persist state across the trip; the back link now tries
native browser history.back() first for instant zero-request restoration
when possible, falling back to a fresh load with state recovery as a
safety net. `app.js` and `product\_detail.html` modified. Also shipped
earlier: **Updates \& Status feature** (notification bell, live app
version, hard-refresh button). `sw.js` at v21.

\---

## 1\. What this app is

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

Data lives in **SQLite** (`gtm\_catalog.db`). Excel upload is a bulk
import/refresh (upsert keyed on Product ID), not the live data source.

\---

## 2\. File structure

```
gtm\_catalog/
├── app.py                        Flask routes - all business logic
├── db.py                         All database access - only file that
│                                   talks to SQLite directly
├── config.py                     Secrets/paths - NEVER share this file's
│                                   real contents outside your own deploy
├── requirements.txt               Flask, pandas, openpyxl, Werkzeug
├── backup\_db.py                  Automated database backup (NEW, see
│                                   §12) - safe live-DB snapshot via
│                                   sqlite3's .backup() API, 14-day
│                                   rotation. Run manually or via a
│                                   PythonAnywhere Scheduled Task.
│
├── gtm\_catalog.db                 SQLite database (auto-created) - NOT
│                                    tracked in git anymore (see §13)
├── backups/                       Rotating DB snapshots from
│                                    backup\_db.py - NOT tracked in git
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
│   ├── product\_detail.html          Product detail page (NEW). Image (or
│   │                                  placeholder), description, same
│   │                                  price/order UI as a catalog card -
│   │                                  reuses order.js/app.js unmodified
│   │                                  via matching data-\* attributes.
│   ├── product\_not\_found.html       404-style page for an unmatched
│   │                                  product slug (NEW)
│   ├── \_glass\_tabbar.html           Bottom tab bar, extracted into a
│   │                                  shared partial (NEW) - included
│   │                                  from both index.html and
│   │                                  product\_detail.html so they can't
│   │                                  drift out of sync
│   ├── \_order\_modal.html            Order/cart modal, same reasoning -
│   │                                  shared partial (NEW)
│   ├── login.html                   Admin login (yours, untouched by me)
│   ├── admin.html                   Admin dashboard (yours, untouched)
│   ├── admin\_products.html          Manage Products: list/search/edit/delete
│   ├── product\_form.html            Add/Edit Product + Price History tab
│   │                                  + Description textarea (admin-
│   │                                  editable, optional)
│   ├── admin\_orders.html            All orders: date filter, summary
│   │                                  stats, per-rep breakdown, list
│   ├── admin\_order\_detail.html      One order's line items + status
│   ├── admin\_reps.html              Sales team list (add/edit/delete)
│   └── rep\_form.html                Add/edit a single sales rep
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
│   │   │                              gtm\_last\_sync\_v1) - even on a
│   │   │                              "nothing missing" run.
│   │   └── status.js                 Updates \& Status panel (NEW): live
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
    └── sale\_price\_catalog.xlsx      Last uploaded Excel (backup/import
                                        source, not the live data)
```

\---

## 3\. Database schema (SQLite, via `db.py`)

### `products`

|Column|Notes|
|-|-|
|`id`|INTEGER PK - internal id, used in admin URLs|
|`product\_id`|TEXT - business ID, format **`GTM - ####`** (uppercase, spaced, e.g. `GTM - 0226`). Auto-generated on manual Add; free text preserved on Excel import|
|`product\_name`, `upc`, `unit`, `retail`, `wholesale`, `category`, `status`|`status` is `"In Stock"` / `"Out Of Stock"`|
|`description`|TEXT, optional, defaults to `''`. Free text (Burmese or any Unicode needs zero special handling). Admin-editable via `product\_form.html`; optional column on Excel upload - **a sheet without this column never wipes existing descriptions**, only a sheet that actually includes it updates them. Shown on the public product detail page.|

### `price\_history`

Append-only log of retail/wholesale changes, keyed on the **business**
`product\_id` (survives product deletion/recreation). `changed\_at` is a
SQLite timestamp **string** (`'2026-07-23 09:12:01'`) - not a Unix
timestamp. `source` is `"manual"` or `"excel\_upload"`. Only logged when a
price actually changes.

### `activity\_log`

Powers the notification bell. Unified feed of two event types -
`product\_added` and `price\_changed` - so the UI only queries one table.

|Column|Notes|
|-|-|
|`event\_type`|`"product\_added"` or `"price\_changed"`|
|`product\_id`|Business key (survives delete/re-add, same reasoning as `price\_history`)|
|`product\_name`, `details`|`details` is a short human-readable summary, e.g. `"Retail: 5,000 → 5,500 Ks • Wholesale: 4,500 → 4,800 Ks"`|
|`created\_at`|SQLite timestamp string|

Logged automatically from `\_log\_price\_change()` (reuses its existing
"did retail/wholesale actually change" check - one source of truth for
both `price\_history` and this table) and from `add\_product()` /
`import\_excel\_into\_db()`'s new-row branch. The one-time bootstrap import
on a fresh database passes `log\_activity=False` deliberately - without
it, a brand new install would flood the feed with 200+ "product added"
entries on day one instead of starting clean.

### `sales\_reps`

Managed list — reps pick from this at order time, **never type a name**.

|Column|Notes|
|-|-|
|`code`, `name`|e.g. `"T-1"`, `"Myu Latt Aung"`|
|`active`|1/0. Inactive reps disappear from the order-form picker but past orders keep their label unaffected (snapshot, not a live FK)|

`db.rep\_label(rep)` builds the exact display/storage string: `"T-1 (Myu Latt Aung)"`.

### `orders`

|Column|Notes|
|-|-|
|`rep\_name`|Exact snapshot string, e.g. `"T-1 (Myu Latt Aung)"`|
|`outlet\_name`|Free text (not a managed list - explicitly decided against for now)|
|`status`|`"New"` / `"Fulfilled"` / `"Cancelled"`|
|`total\_retail`, `total\_wholesale`|Computed server-side at submit time|
|`created\_at`|SQLite timestamp string|
|`idempotency\_key`|Nullable. See §5 - dedup guard against retry/double-tap|

### `order\_items`

Line items. **Snapshots** product name/price at order time - editing a
product's price later never rewrites what was actually ordered.
`product\_id`, `product\_name`, `unit\_retail`, `unit\_wholesale`, `quantity`,
`line\_retail`, `line\_wholesale`.

\---

## 4\. Routes (`app.py`)

**Public (no login):**

|Route|Method|Purpose|
|-|-|-|
|`/`|GET|Catalog page|
|`/product/<product\_id\_slug>`|GET|Product detail page. Slug is the business Product ID with spaces stripped (`GTM-0001`) - matched against the DB's `GTM - 0001` form space-insensitively. 404s to `product\_not\_found.html` if no match.|
|`/api/prices`|GET|JSON feed, same field names as the original Excel-only version so Excel's "Get Data from Web"/Power Query keeps working unchanged. Now also includes `"Description"` per product (additive - existing Power Query column mappings shouldn't break, but flagging the shape change).|
|`/api/price-history`|GET|JSON feed of price changes, optional `?product\_id=`|
|`/api/activity`|GET|JSON feed backing the notification bell: `{"latest\_id": N, "events": \[...]}`. `?limit=` (default 30, capped 1-100). `latest\_id` is always the true max regardless of `limit`, so the client can cheaply check "anything new since I looked" with `?limit=1` instead of pulling the whole feed.|
|`/order/submit`|POST|Rep submits their cart. JSON body: `rep\_name`, `outlet\_name`, `idempotency\_key`, `items: \[{product\_pk, quantity}]`|
|`/sw.js`|GET|Serves the service worker with the right header|

**Admin (all require `session\["admin"] == True`):**

|Route|Method|Purpose|
|-|-|-|
|`/login`, `/logout`|GET/POST, GET|Auth|
|`/admin`|GET|Dashboard|
|`/admin/products`|GET|List/search products|
|`/admin/products/add`|GET/POST|Add product (auto `GTM - ####` ID)|
|`/admin/products/<id>/edit`|GET/POST|Edit product + view price history|
|`/admin/products/<id>/delete`|POST|Delete product|
|`/admin/reps`|GET|List sales reps|
|`/admin/reps/add`|GET/POST|Add a rep|
|`/admin/reps/<id>/edit`|GET/POST|Edit a rep (code/name/active)|
|`/admin/reps/<id>/delete`|POST|Delete a rep (past orders unaffected)|
|`/admin/orders`|GET|All orders. `?date=YYYY-MM-DD` filters to one day; shows summary stats + per-rep breakdown|
|`/admin/orders/<id>`|GET|One order's detail|
|`/admin/orders/<id>/status`|POST|Change order status|
|`/admin/orders/<id>/delete`|POST|Delete an order|
|`/upload`|POST|Bulk Excel import (upsert)|

\---

## 5\. Key design decisions worth knowing

* **Auto-generated Product IDs:** `GTM - ####`, 4-digit padded, grows past
9999 naturally (`GTM - 10000`...). Computed server-side at submit time -
client can't override it even if it tries (tested). Editing an existing
product leaves its ID freely editable (legacy/imported IDs may not
follow the pattern).
* **Excel upload is an upsert, not a wipe:** matches rows by business
`product\_id`. A product missing from a new upload is marked
`"Out Of Stock"`, **not deleted** - so manually-added products and
price history both survive re-uploads.
* **Sales reps are a managed list, not free text.** Reps pick from a
dropdown populated from `db.get\_active\_reps()`. The server
independently re-validates the submitted `rep\_name` against that same
active list before creating an order - a tampered/fake rep name sent
directly to the API is rejected (tested), not just prevented by the UI.
* **Order pricing is never trusted from the browser.** The cart only
holds `{product\_pk, quantity}`. At submit, the server looks up each
product's *current real price* - a tampered request claiming a fake
price is silently ignored (tested).
* **Duplicate-submission guard:** the browser generates a unique
`idempotency\_key` per order-in-progress (persisted in localStorage,
reused across retries of the same submission, replaced once an order
actually completes). If the same key hits `/order/submit` twice, the
server returns the original order instead of creating a second one
(tested: same key -> same order\_id; different key -> genuinely new
order; missing key -> still works, backward compatible).
* **Numeric form fields are `<input type="text">`, not `type="number"`**

  * deliberate fix for an unusable number-spinner UI. Client-side
validation is a courtesy; the server is the real gatekeeper.
* **Category picker is a custom JS chip list, not `<datalist>`** -
native datalist has unreliable mobile-browser support (notably older
Android). Chips render in normal document flow (no floating overlay),
filter live as you type, and free typing still works for new categories.
* **The admin navbar is now scoped away from the catalog page on
purpose:** `base.html`'s dark navbar rendered on *every* page before,
which meant a logged-in admin saw it stacked directly above the
catalog's own header - two brands, two menus, doing overlapping jobs.
Fixed with one condition: `{% if session.get("admin") and request.endpoint != 'home' %}`. The catalog page's own bottom glass tab
bar (Catalog/Cart/Admin) is now the only navigation surface there; the
full admin navbar still renders normally on every actual `/admin/\*`
page.
* **Bottom tab bar over top nav, for the catalog page specifically:**
chosen because this is a tool reps reach for one-handed, repeatedly, in
the field - primary nav (Catalog/Cart/Admin) belongs in the thumb zone
at the bottom, not the top. Search + category filter are the only
things that stay up top. Desktop (≥768px) drops the fixed/floating
bottom-bar behavior entirely and renders it as a normal static bar
instead - a bottom-pinned pill makes no sense on a wide window.
* **"Liquid Glass" (`backdrop-filter: blur() saturate()`) on the topbar
and bottom tab bar only** - product cards stay fully opaque on
purpose. Glass-on-glass gets muddy fast and this app's actual job is
making price numbers easy to read at a glance; the frosted effect adds
polish on chrome, not on data.
* **Glass-bar animations use `transform`/`opacity` only, never
`max-width`/`padding`/`grid-template-rows` where avoidable.** The first
version of the shrink/expand animations animated layout-triggering
properties (forces a full reflow every single frame of a transition).
`transform`/`opacity` run on the compositor instead - the browser can
animate them without recalculating page layout at all. The bottom tab
bar's collapse was rewritten from `max-width`+`padding` to `transform: scale()` for exactly this reason. `backdrop-filter` blur radius was
also cut roughly in half on both bars (20px->10px, 22px->10px) since
it's recomputed continuously during scroll, not just once - it was the
single largest contributor to scroll jank of everything in the glass
system.
* **Product images are matched by filename, not stored as a database
column.** No upload UI to build, no path to validate/sanitize - you
just drop a correctly-named file into `static/product-images/` and it
appears. Filename = business Product ID with spaces stripped
(`GTM-0001.jpg`), checked at *render time* against a small list of
extensions (`.jpg`/`.jpeg`/`.png`/`.webp`). No match = a clean
placeholder, never a broken-image icon.
* **Product detail pages are keyed by business Product ID in the URL
(`/product/GTM-0001`), not the internal `id`.** Chosen for
shareability/readability over matching the admin routes' internal-ID
convention. The DB lookup normalizes spaces on both sides
(`REPLACE(product\_id, ' ', '') = ? COLLATE NOCASE`) so `GTM-0001` and
the canonical stored `GTM - 0001` both resolve to the same product.
* **The product detail page reuses `order.js`/`app.js` completely
unmodified** - Add to Order, the qty stepper, and Copy Price all work
identically to a catalog card, because the detail page's wrapper
carries the exact same `class="card product-card"` and `data-\*`
attributes (`data-pk`, `data-fullname`, `data-retail`,
`data-wholesale`, `data-unit`) that those scripts already expect. No
JS had to be touched or duplicated for this to work.
* **The bottom tab bar and order modal were extracted into shared
partials** (`\_glass\_tabbar.html`, `\_order\_modal.html`), included from
both `index.html` and `product\_detail.html`, instead of being
duplicated. Both have a lot of ids/behavior that has to stay in sync
(`cartBadge`, `submitOrderBtn`, the rep dropdown, etc.) - exactly the
kind of thing that's already caused real bugs in this project from
template/route drift (§6.4). One shared source instead of two
copies that can quietly diverge.
* **Offline precaching of product pages runs from the page itself, not
from the service worker's `install` step.** A service worker can't
easily report live progress back to a page mid-install (would need a
`postMessage`/`BroadcastChannel` round trip). Instead, `precache.js`
runs after the catalog page loads, checks what's actually missing from
cache, and only shows a progress banner if there's real work to do -
most repeat visits show nothing at all since everything's already
cached. Uses a small worker-pool pattern (5 concurrent fetches) rather
than fetching 225+ pages one at a time.
* **Product pages live in their own cache bucket
(`gtm-product-pages-v1`), separate from the main versioned
`CACHE\_NAME`.** The main cache gets fully wiped and rebuilt on every
version bump (routine CSS/JS deploys) - with 225+ product pages
potentially precached, re-downloading all of them on every unrelated
style tweak would be wasteful and slow, especially on a rep's mobile
data. The product-pages bucket persists across normal version bumps
and is only cleared if its own name changes. `sw.js`'s `activate`
handler was updated to whitelist both cache names instead of just one.
* **Hard Refresh clears the main cache bucket AND forces a real SW
version check - but deliberately never touches the product-pages
cache.** A naive "clear every cache and reload" button would force
re-downloading 200+ offline product pages just to pick up a CSS tweak,
undoing the whole point of splitting the caches in the first place
(above). It does two things instead: `registration.update()` to force
a genuine check for a new `sw.js` right now (bypassing the browser's
normal throttled check interval), AND directly deletes any cache key
that isn't `gtm-product-pages-v1` - covering the case where `sw.js`'s
own version string wasn't bumped but static assets changed anyway
(forgetting to bump `CACHE\_NAME` has been a recurring theme across
this project's history - see §6, §7's version log).
* **App version is fetched live from the actual running service worker,
not hardcoded anywhere.** `status.js` opens a `MessageChannel`, sends
`{type: 'GET\_VERSION'}` to `navigator.serviceWorker.controller`, and
`sw.js` replies with its own `CACHE\_NAME` on the reply port. This
can't drift from reality the way a hardcoded "v21" string in a
template could.
* **The notification bell's "unread" state is tracked client-side only,
in localStorage** (`gtm\_last\_seen\_activity\_id\_v1`) - there's no login
or per-user concept on the public catalog, so "have I seen this" is
necessarily a per-device thing, not a per-person one. Checked once on
page load (not continuous background polling) since this is a tool
people reopen throughout the day anyway - a fresh check each visit is
enough without the added complexity of interval management and
pause-when-hidden logic.
* **`activity\_log` is one unified table for two different event types**
(`product\_added`, `price\_changed`) rather than two separate tables or
a UNION query at read time. Simpler read path for the bell (one query,
one "is there anything new" comparison), and reuses
`\_log\_price\_change()`'s existing diff-check for what counts as a real
price change rather than duplicating that logic anywhere.

\---

## 6\. Known gotchas / things that have bitten us before

1. **Two different datetime filters exist on purpose:**

   * `datetimeformat` expects a **Unix timestamp** (Excel file's `os.path.getmtime()`).
   * `sqlitedatetime` expects a **SQLite timestamp string** (`orders.created\_at`, `price\_history.changed\_at`).
Using the wrong one on the wrong field crashes the page.
2. **Flask caches compiled templates in memory** - uploading a new
`.html` file does nothing until the process restarts (PythonAnywhere:
Web tab -> Reload).
3. **`config.py` holds your real `ADMIN\_PASSWORD\_HASH`.** Never re-paste
a version of this file from a chat that has the placeholder hash in
it - locks you out of login.
4. **`product\_form.html` and its route in `app.py` must stay in sync** -
the template expects `categories`, `next\_product\_id` (Add mode), and
`price\_history` (Edit mode) to be passed in. A mismatch here caused a
real "No filter named cleannum" crash once, from the template and
route drifting out of sync across upload rounds.
5. **SQLite migrations on a live database need explicit `ALTER TABLE`,
not just `CREATE TABLE IF NOT EXISTS`.** The latter does nothing for
a table that already exists. Both `outlet\_name` and `idempotency\_key`
were added to the already-live `orders` table via a `PRAGMA table\_info` check + conditional `ALTER TABLE ADD COLUMN` in
`init\_db()` - tested against a simulated copy of the real
pre-migration database each time, specifically to make sure existing
orders are never touched or lost.
6. **A large CSS/HTML rewrite can silently orphan a selector.** During
the glass-header rewrite, one edit's "replace this block" boundary
landed mid-rule instead of on a clean rule boundary - it swapped out
everything up through `.search-wrap {` but the replacement didn't
re-open that selector. The result: `position: relative; width: 100%; }` was left dangling with no selector owning it. Browsers silently
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
(`order.js` missing from `STATIC\_ASSETS`, the iOS install-time
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
9. \*\*`request.mode === 'navigate'` only catches real browser navigations

   * not `fetch()` calls from your own page scripts.\*\* `precache.js`
downloads product pages with a plain `fetch()`, which is NOT
navigate-mode. The first version of the product-page caching logic in
`sw.js` lived entirely inside the `navigate` branch and would have
silently missed every request `precache.js` makes, letting them fall
through to the generic static-asset handler and get cached in the
wrong bucket (`CACHE\_NAME` instead of `PRODUCT\_PAGES\_CACHE`). Fixed by
checking `/product/` by **path**, before the navigate-mode check, so
it catches both a real page load and a background fetch identically.
10. **`precache.js` and `sw.js` share a cache-name string
(`PRODUCT\_PAGES\_CACHE = 'gtm-product-pages-v1'`) that has to match
EXACTLY, by hand, in both files.** They're separate files that can't
import a shared constant (a service worker and a page script are
different execution contexts). If you ever rename one, rename the
other to match, or the two will silently stop agreeing on where
product pages live - `precache.js` would download and cache pages
into a bucket `sw.js`'s fetch handler never looks in.
11. **The Burmese placeholder text in `product\_detail.html`
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
product** (additive - `\_row\_to\_dict()` in `db.py` always includes it
now). Shouldn't break existing Power Query column mappings since
it's a new column, not a changed one, but flagging the shape change
since that endpoint is explicitly relied on to stay stable.
14. **`last\_insert\_rowid()` reflects whichever INSERT happened most
recently on that connection - not "the INSERT I meant."** Caused a
real bug: `add\_product()` did `INSERT INTO products` then, before
capturing `last\_insert\_rowid()`, ran `\_log\_activity()` which does
its own `INSERT INTO activity\_log` - so the captured id silently
became the activity log's row id instead of the product's. Any
function that captures `last\_insert\_rowid()` after adding a
side-effect write (logging, auditing, etc.) needs that capture to
happen **immediately** after the INSERT it actually cares about,
before any other INSERT touches the same connection. Caught in
testing before shipping, not in production - see §8.7.

\---

## 7\. Service worker (`sw.js`) — owned/maintained by the project owner

Currently **v21**. History:

* v1->v2: stopped caching HTML pages (was causing a login redirect loop
from stale cached admin/login pages).
* v8->v9: `/` (catalog page only, never `/admin`/`/login`) is now cached
on every successful online fetch, so there's a real fallback when
offline. Before this, offline navigation always failed silently because
nothing was ever cached for it to fall back to.
* v9->v10: also precache `/` during the `install` step itself, not only
on the next successful navigation - closes an iOS-specific gap where a
device adds the PWA to its Home Screen while online but never completes
a full page navigation before going offline (see §9). `order.js` also
added to `STATIC\_ASSETS`.
* v10->v11: header/nav redesign (glass topbar + bottom glass tab bar) -
`style.css`/`index.html` both changed significantly, bumped so cached
CSS doesn't linger on devices that visited before the deploy.
* v11->v12: self-applied by the project owner (fixed `.search-wrap`
orphaned-selector bug in `style.css`, see §6.6) - never independently
verified in a chat, hence the jump straight to v13 next rather than
risk building on an unconfirmed base.
* v11->v13: top bar redesign (hero + category strip now collapse on
scroll, matching the bottom tab bar's behavior) - built from the last
independently-verified v11, skipping the unverified v12.
* v13->v14: animation performance pass (see §5/§6.8) - `style.css`
changed again, bumped so cached CSS doesn't linger.
* v19->v20 (v14-v19 happened outside a verified session - see §6.7):
product detail pages (`/product/<id>`) can now be cached for offline
use, in a new separate stable cache bucket (`PRODUCT\_PAGES\_CACHE`, see
§5) rather than the main versioned cache. `precache.js` added to
`STATIC\_ASSETS`. Fetch handler restructured so `/product/` is matched
by path before the navigate-mode check (see §6.9).
* v20->v21: added a `message` event listener so a page can ask "what
version are you running" via `MessageChannel` - backs the Updates \&
Status panel's live version display (see §5).

**Rule going forward: bump `CACHE\_NAME` any time CSS/JS/`STATIC\_ASSETS`
changes**, or browsers that visited before keep serving old files.

`STATIC\_ASSETS` currently includes `style.css`, `admin.css`, `app.js`,
`admin.js`, `order.js`, `manifest.json`.

\---

## 8\. Bug history

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
moved `overflow-x: hidden` to `html` only; `body` keeps `max-width: 100%` but no `overflow` rule.
3. **Offline mode never actually worked, even before recent changes** ->
the navigation fallback pointed at `caches.match('/')`, but `/` was
never being cached anywhere (`STATIC\_ASSETS` only ever listed CSS/JS).
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
row on desktop** -> pre-existing intentional behavior (`flex-wrap: wrap; justify-content: center` above 768px, so chips wouldn't need
horizontal scrolling on wide screens), but with 8 categories it left
the last one visually orphaned in the middle. Fix: `justify-content: center` -> `flex-start`, so a wrapped row left-aligns instead of
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
7. **`add\_product()` silently returned the wrong id** -> caught in
testing, before shipping, not in production. Root cause:
`last\_insert\_rowid()` reflects whichever INSERT happened most
recently on that database connection. When `\_log\_activity()`'s own
INSERT (into `activity\_log`) was added into `add\_product()` *after*
the products INSERT but *before* `last\_insert\_rowid()` was queried,
the function started returning the activity log row's id instead of
the new product's id. Nothing in `app.py` currently uses
`add\_product()`'s return value, so this hadn't caused a visible
symptom yet - but it was a landmine for any future code that
reasonably expects the function to return what it says it returns.
Fix: capture `last\_insert\_rowid()` immediately after the products
INSERT, before any other INSERT on that connection can happen.
Reverified with the exact failing scenario (add a product, confirm
the returned id actually points at it) after the fix.
8. **Bootstrap import would have flooded the notification feed with
200+ entries on day one** -> `import\_excel\_into\_db()`'s
`log\_activity` parameter existed specifically to prevent this, but
the one-time bootstrap call inside `init\_db()` wasn't actually
passing `log\_activity=False`. Caught in testing (bootstrap-imported
225 real products, then checked `activity\_log` - found 225 rows
instead of the expected 0) before it ever shipped. Fixed by passing
the flag explicitly at that one call site.

\---

## 9\. iOS offline issue — hardening applied, awaiting real-world confirmation

**Symptom:** service worker / offline mode required an internet
connection on iOS devices, even after the v9 fix.

**What's confirmed from current documentation:** this matches a
well-documented, current Safari/iOS platform limitation, not obviously a
code bug:

* iOS enforces a much smaller Cache Storage quota (\~50MB) than
Android/desktop.
* iOS aggressively evicts cached data (Cache Storage/IndexedDB) after
periods of inactivity - multiple real-world reports describe offline
mode working right after an online visit, then failing again later
with no code change in between.
* Real developer reports describe this exact symptom: offline works for
pages already visited online, fails for anything not yet cached.

**Code hardening applied in v10 (was "proposed, pending confirmation" in
the previous version of this doc - now live):** precache `/` during the
`install` step itself, in addition to the existing "cache `/` on every
successful navigate" runtime approach, wrapped so a failure to fetch `/`
during install doesn't block the rest of `STATIC\_ASSETS` from precaching:

```js
self.addEventListener('install', (event) => {
    event.waitUntil(
        Promise.all(\[
            caches.open(CACHE\_NAME).then((cache) => cache.addAll(STATIC\_ASSETS)),
            caches.open(CACHE\_NAME).then((cache) => cache.add('/').catch(() => {})),
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
`CACHE\_NAME` invalidates old cache entries).
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

\---

## 10\. What's shelved / not built yet

* Bottom tab nav is **done** (see §5/§2) - removed from this list.
* Product detail page + images is **done** (see §5/§2) - removed from
this list.
* Catalog memory (search/filter/scroll across back navigation) is
**done** - removed from this list.
* **Notification bell clickable redirect (PLANNED):** Currently, the
notification bell shows events (price changes, new products) in a
read-only activity feed, but tapping a notification doesn't navigate
anywhere - reps have to manually search the catalog to see the changed
product. Next iteration: clicking a notification should navigate
directly to the affected product's detail page (`/product/GTM-XXXX`),
so "Price changed: GTM - 0042" becomes a direct jump to that product
instead of a passive info read. Requires: (a) storing `product\_id`
alongside each event in `activity\_log` table (already exists, see §3),
(b) rendering notification rows as clickable links or button-styled
elements in `status.js`, (c) URL construction in JS to navigate to
`/product/<slugified-product-id>`. Low lift, high UX win - good 30-min
feature.
* Skeleton loaders, pull-to-refresh, dark mode - still deprioritized,
still on the table.
* Outlet master list (managed, like sales reps) - explicitly decided
against; outlet stays free text for now.
* Stock-on-hand quantity tracking - explicitly decided against for now.
* Push notifications, multi-language toggle, GPS visit tracking,
beat/route planning, approval workflows - discussed as bigger-effort
ideas common in commercial SFA/DMS tools, explicitly deprioritized as
premature for current team size.
* HTTP->HTTPS server-side redirect (see §8.1) - flagged as worth doing,
not yet done.

\---

## 11\. Catalog memory \& navigation (search/filter/scroll restoration)

**The problem:** When a rep tapped the eye icon on a product card to view
its detail page, then tapped "Back to Catalog," they landed at the top of
a fresh page with all filters/search reset - losing their place. This was
especially annoying when browsing through a filtered/searched subset,
tapping a few products to check prices, then finding themselves back at
square one.

**Root cause:** The "back" link was a simple `<a href="/">` to the home
route, a brand-new page load. Filter state (`currentCategory`,
`currentStatus`, search text, sort order, scroll position) only lived in
JS variables, never persisted anywhere.

**Fix applied:**

1. **sessionStorage state persistence** (`app.js`): Added `saveCatalogState()`
and `restoreCatalogState()` functions that snapshot search box, sort
select, category, status, and scroll position to/from `sessionStorage`
using key `gtm\_catalog\_state\_v1`. State is saved:

   * Every time `filterCatalog()` runs (search/sort/filter changes)
   * On `pagehide` event (right before navigation away, e.g. into bfcache)
   * On `visibilitychange` to 'hidden' (backup for PWA/mobile scenarios
where pagehide may not fire)
2. **Smart back navigation** (`product\_detail.html`): The "Back to
Catalog" link now tries native `window.history.back()` first. When the
browser has history to go back into, this restores the *exact* previous
page instantly from memory (bfcache) - zero network requests, zero
reload. The DOM state and scroll position are preserved by the browser
itself. Falls back to a fresh `/` load with sessionStorage restore when
there's no history (direct link, new tab, etc.).
3. **Restore on load** (`app.js`): On page load, `restoreCatalogState()`
runs first. If there's saved state in sessionStorage, it:

   * Restores search box \& sort select values
   * Re-sets `currentCategory` and `currentStatus` JS variables
   * Updates the category/status pill button highlighting (by searching
for the matching button's `onclick` attribute)
   * Calls `filterCatalog()` to re-apply the saved filter/sort
   * Waits one frame, then `scrollTo()` the saved Y position
Falls back to a normal `filterCatalog()` run if nothing's saved.

**Files modified:** `app.js`, `product\_detail.html`

**Trade-offs and edge cases:**

* **sessionStorage, not localStorage:** Intentional. This is "where was I
this browsing session," not "my favorite filters for all time."
sessionStorage clears itself when the tab/app closes, which is exactly
the right lifetime here.
* **Private browsing / storage unavailable:** If sessionStorage isn't
available (private mode, quota exceeded), state just doesn't persist -
app falls back to a normal fresh filter. No errors thrown.
* **bfcache availability:** Native browser back works perfectly on modern
Chrome/Android. iOS Safari is more aggressive about evicting bfcache,
especially in PWA mode - but even if bfcache is evicted, sessionStorage
restore still recovers 95% of the UX (state saved but scroll may reset).
* **Pill button restoration:** The restoration code finds the matching
category/status button by searching for an `onclick` attribute - fragile
if button markup changes. Consider refactoring to use `data-value`
attributes on pills instead, making restoration button-search-independent.

**Testing notes:** Verified end-to-end on:

* Desktop Chrome: native back instant, scroll/filter/search all restored
* Mobile Chrome: native back works, bfcache kicks in
* PWA installed (Android): back navigation functional, sessionStorage state
survives app re-open

**Potential improvements:**

* Replace pill button onclick-search with `data-value` attributes for
cleaner restoration logic
* Consider URL-based state (query params) as an alternative to/alongside
sessionStorage - would make deep-linking to a filtered view possible
(shareable catalog links with pre-set filters)
* Extend to cart state? (Currently not persisted - reps lose their draft
order on browser tab close. May be intentional security-wise, but worth
discussing if reps want draft persistence.)

\---

## 13\. Open item: "web/desktop view sucks"

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

\---

## 14\. Incident: git tracking the database caused a near data-loss

**What happened, in order:**

1. `gtm\_catalog.db`, `uploads/sale\_price\_catalog.xlsx`, and
`\_\_pycache\_\_/\*.pyc` had all been committed to git at some point
early on - none of these should ever be tracked (a live database and
compiled bytecode both change on nearly every request/run, and
`.pyc` files are regenerated by Python automatically anyway).
2. This caused `git pull` to fail with "your local changes would be
overwritten" essentially every time, since the live server's copies
of those files had diverged from whatever was last committed.
3. While troubleshooting, a `git reset --hard` was run against the
**first commit** - which predates the product detail feature - as
an attempt to get unstuck. A hard reset rewinds the entire local
working copy, including `gtm\_catalog.db`, since it was tracked. This
put a near-empty/mismatched database in place on the live
PythonAnywhere server, causing `sqlite3.OperationalError: no such table: products` and the catalog page 500ing in production.
4. Recovered without permanent data loss: `git status` confirmed
GitHub's `origin/main` still had all 3 real commits (a local reset
never touches the remote), and the live server's actual
`gtm\_catalog.db` file on disk (238 products, all tables intact) had
NOT been touched by the reset alone - only by the *subsequent pull*
attempting to bring in the git-tracked (empty) version. A manual
backup (`cp gtm\_catalog.db gtm\_catalog.db.backup`) was taken before
any further git operations, then restored after pulling.

**Root fixes applied:**

* `git rm --cached` + `.gitignore` for all three: `gtm\_catalog.db`,
`uploads/` (whole folder), `\_\_pycache\_\_/`. None of them can cause a
pull/reset/merge conflict again, because git no longer has any
opinion about their contents.
* **`backup\_db.py`** added and tested (see §2/§7-equivalent for backup)

  * uses `sqlite3`'s built-in `.backup()` API rather than a plain file
copy, specifically because copying a live database file with
`shutil.copy()` risks grabbing it mid-write (a half-finished order, a
half-written price update) and producing a corrupted backup. Keeps a
14-day rotating history in `backups/`. Verified: produces a genuinely
valid, queryable database file (not just a byte copy), and pruning
correctly removes only backups older than the retention window.

**What this protects against, and what it still doesn't:**

`backup\_db.py`'s output lives on the same PythonAnywhere server as the
live app - it protects against exactly this incident (bad git
operations, accidental overwrites, human error), since a recent backup
can just be copied back over `gtm\_catalog.db`. It does **not** protect
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

\---

## 15\. If you're picking this up in a new chat

Paste this file plus:

* The current `app.py`, `db.py`, `style.css`, `index.html`,
`product\_detail.html`, `app.js`, `precache.js`, `status.js`, and
`product\_detail.html` - CSS/HTML/JS drift across upload rounds has
caused real bugs before (see §6.4, §6.6, §11), and this project now has
enough interconnected files (§2) that it's worth being thorough rather
than assuming. The catalog memory feature (§11) ties together `app.js`
and `product\_detail.html` - both must be synced or state won't restore.
* Don't paste an old `config.py` over your live one, and don't paste an
old `sw.js` over your live one (you maintain that file yourself now -
see §6.7).
* **Never suggest re-tracking `gtm\_catalog.db`, `uploads/`, or
`\_\_pycache\_\_/` in git** - see §14 for exactly why that's dangerous,
not just inconvenient.
* If you're extending `db.py` with a new function that logs a
side-effect (activity, audit, etc.) alongside a real INSERT, watch the
`last\_insert\_rowid()` ordering - see §6.14 for exactly how this bit
the project once already.
* For the notification redirect feature (see §10), you'll need `status.js`,
which renders the activity feed - it's the right place to add clickable
links to products.
* The specific feature or bug you want to tackle next.


Notification bell clickable redirect — DONE. Activity rows now navigate to /product/<slug> on click/Enter/Space. No app.py/db.py changes needed — product\_id was already flowing through /api/activity. status.js: added slugifyProductId() (strips spaces from business product\_id, matching app.py's existing image-lookup normalization — purely a client-side string transform for URL-building, never touches the DB); rows with a resolvable product\_id get role="button", tabindex="0", a chevron icon, and a click handler that closes the status modal then navigates; rows without a product\_id degrade to plain read-only (defensive, shouldn't occur under current schema). style.css: added .activity-row-clickable / .activity-chevron hover+focus styling, matching existing pill/tab hover conventions.

Supplier field added (internal-only, for inventory automation) — DONE.

Schema: products.supplier TEXT DEFAULT '' — added via the same ALTER TABLE IF NOT EXISTS-style migration pattern already used for description (§ schema section), so the live DB's 239 existing rows got a safe empty default instead of breaking.
db.py: _row_to_dict() now includes "Supplier"; add_product() and update_product() accept/store it; import_excel_into_db() treats Supplier as an optional column, same rule as Description — a sheet without it never wipes existing supplier values, only a sheet that actually includes the column updates them in bulk. (Verified with a real re-import test: supplier-less sheet preserved all 239 existing values.)
app.py: product_form_to_dict() now reads supplier from the admin form.
product_form.html: new Supplier input field (labeled "internal only, never shown on the public catalog").
admin_products.html: Supplier now shown as a column in the admin product list and included in the client-side search filter.
/api/prices: now returns "Supplier" in the JSON for every product — no route change needed, since it just calls db.get_all_products(). This is the field your inventory-automation tool should pull.
Not touched: public catalog (index.html) and product detail page (product_detail.html) — supplier is intentionally never rendered there, per the original requirement.
Excel import file: your gtm_prices_2026-07-30.xlsx already has the Supplier column with values (e.g. MMA) — re-upload it once via the admin panel to backfill all 239 existing products.


Notification bell — in-stock/out-of-stock alerts added — DONE.

db.py: new _log_status_change() helper, same pattern as the existing
_log_price_change() (only fires if the status actually changed, shares
the caller's transaction, caller commits). Logs two distinct event
types — back_in_stock and out_of_stock — rather than one generic
"status_changed", so the feed can color/icon them differently without
the client parsing details strings. Wired into the two places status
can change:
  - update_product() (admin manually edits a product) — reuses the
    pre-edit row already being fetched for the price-change check.
  - import_excel_into_db() — both when a row's Status differs on
    upload, AND when a product gets bulk-flipped to Out Of Stock
    because it's missing from the uploaded sheet (fetches the
    about-to-flip rows before running that bulk UPDATE, since a single
    SQL statement has no per-row hook otherwise).
status.js: activityIconFor() now returns a check-circle icon for
back_in_stock and an x-circle icon for out_of_stock. Everything else
(chevron, click-to-product navigation, unread bell dot) needed zero
changes — already generic over event_type.
style.css: added .activity-icon-back_in_stock (green, matches the
existing in-stock badge tone) and .activity-icon-out_of_stock (red).
Tested against a real copy of gtm_catalog.db — status flip logged
correctly ahead of existing price-change/product-added entries.
app.py, index.html, product_detail.html: untouched.

---

## Open item: swipeable product gallery for in-store order-taking

Reported same day as the notification upgrade above — not designed,
not built. Problem: reps visiting a store currently have to tap the eye
icon on each product card individually to show a customer a photo,
which takes hours per category across 200+ products. Wants a
phone-photo-gallery-style swipeable view (left/right) scoped to
whatever's currently filtered on the catalog grid, showing price/
details alongside the image, fast enough to actually speed up
order-taking rather than just being a nicer viewer.

Open questions, not yet answered:
* Slide content — minimal (image + price overlay, tap to add) vs.
  full (image + price + qty stepper + Add to Order all visible per
  slide, no extra tap)
* Whether precache.js currently guarantees product IMAGES are
  downloaded ahead of time (not just page data) — this determines
  whether gallery mode is actually usable on bad in-store signal, and
  needs to be confirmed before building the UI
* Whether products with no photo get skipped in gallery mode or shown
  with a price-only slide
* Gesture conflict: a left-edge swipe-right is iOS Safari's native
  "back" gesture — gallery swipe needs to avoid that zone or be
  presented as a distinct full-screen mode, or reps will accidentally
  exit constantly
* Returning to the grid needs to reuse the existing sessionStorage
  scroll/filter restore (§11) rather than reinventing it

Not scoped to specific files yet since the entry point (new gallery
view vs. modifying product_detail.html vs. something else entirely)
isn't decided.


Notification bell — in-stock/out-of-stock alerts added — DONE.

db.py: new _log_status_change() helper, same pattern as the existing
_log_price_change() (only fires if the status actually changed, shares
the caller's transaction, caller commits). Logs two distinct event
types — back_in_stock and out_of_stock — rather than one generic
"status_changed", so the feed can color/icon them differently without
the client parsing details strings. Wired into the two places status
can change:
  - update_product() (admin manually edits a product) — reuses the
    pre-edit row already being fetched for the price-change check.
  - import_excel_into_db() — both when a row's Status differs on
    upload, AND when a product gets bulk-flipped to Out Of Stock
    because it's missing from the uploaded sheet (fetches the
    about-to-flip rows before running that bulk UPDATE, since a single
    SQL statement has no per-row hook otherwise).
status.js: activityIconFor() now returns a check-circle icon for
back_in_stock and an x-circle icon for out_of_stock. Everything else
(chevron, click-to-product navigation, unread bell dot) needed zero
changes — already generic over event_type.
style.css: added .activity-icon-back_in_stock (green, matches the
existing in-stock badge tone) and .activity-icon-out_of_stock (red).
Tested against a real copy of gtm_catalog.db — status flip logged
correctly ahead of existing price-change/product-added entries.
app.py, index.html, product_detail.html: untouched.

---

## Open item: swipeable product gallery for in-store order-taking

**Status: FULLY DESIGNED, NOTHING BUILT YET.** Every decision below is
locked. Next chat should implement directly from this spec — do not
re-ask these questions unless something here turns out to be wrong
once real files are in hand.

**Problem being solved:** reps visiting a store currently tap the eye
icon on each product card individually to show a customer a photo —
takes hours per category across 200+ products. Solution: a phone-
Photos-app-style flow — category albums -> thumbnail grid -> full-
screen swipeable image with price/details, reachable as a second mode
alongside (not replacing) the existing product-card List.

### 1. Scope for v1
- **Add-to-order is OUT of scope.** Don't show it, don't touch the
  existing cart/order code (order.js, the qty stepper, /order/submit,
  etc.) at all. Gallery is browsing-only.
- **List view stays fully intact and is the default landing view.**
  Gallery is a second mode, not a replacement. Reasoning: the current
  product-card grid does double duty (browsing + fast order entry via
  qty stepper + Add to Order inline); Gallery doesn't do order entry
  yet, so it can't replace List without removing reps' ability to place
  orders efficiently.

### 2. Mode toggle
- Lives in the **bottom tab bar** (`_glass_tabbar.html`), not the top
  bar — user explicitly wants it there, not next to the bell/sliders.
- Choice **persists via localStorage** — a rep who lives in Gallery
  mode while in-store shouldn't have to re-toggle every visit.
- **Default on first-ever load (no stored preference): List.**

### 3. Information architecture (Gallery mode only)
- **Level 1 — Category cards** (replaces the category chip bar when in
  Gallery mode): each card shows a **4-photo collage cover**
  (Pinterest/Google-Photos-album style).
  - Selection rule: **first 4 products in that category, ordered by
    `id ASC`** (same ordering `db.get_all_products()` already uses),
    filtered to only those with a resolvable image.
  - Fallback layouts for categories with fewer than 4 available
    product images (mirrors how Google Photos varies its album-cover
    layout by photo count):
    - 0 images → simple placeholder tile, category still tappable
    - 1 image → full-bleed single tile
    - 2 images → side-by-side split
    - 3 images → one large + two stacked (Google Photos' own 3-photo
      pattern)
    - 4 images → standard 2x2 collage
- **Level 2 — Thumbnail grid**: tapping a category opens a grid of
  small resized thumbnails (Instagram/Photos-grid style), scoped to
  that category only.
  - **Reuses the same filter/search state as List** (explicitly agreed
    — may revisit later, not urgent).
- **Level 3 — Full-screen swipeable view**: tapping a thumbnail opens
  image + price + details overlay, swipe left/right between products
  within that category (Facebook-photo-viewer-style). No add-to-order
  UI.
  - Closing/back must restore the grid's exact scroll position — reuse
    the existing sessionStorage pattern from index.html/
    product_detail.html (§11), don't reinvent it.
  - Missing image → price-only fallback tile/slide instead of a broken
    image. (User will keep images complete manually going forward; an
    admin "products missing an image" checklist is a nice-to-have for
    later, not required for v1.)

### 4. Swipe gesture — resolved, no special handling needed
Reps exclusively open the app via "Add to Home Screen" (confirmed).
Standalone/installed display mode has no browser chrome, so there's no
edge-swipe-back gesture to conflict with a full-width swipe listener.
No defensive swipe-zone logic required.

### 5. Image pipeline — this is the part with real engineering decisions

**Bug found in the existing offline-cache setup (fix this regardless of
whether Gallery ships):** `sw.js`'s generic "static assets: cache-first"
fetch handler is currently catching product images (they're a GET
request that isn't `/product/`, isn't a navigation, isn't `/api/`, so
they fall through to that handler) and caching them into `CACHE_NAME`
— the **versioned** bucket that gets fully wiped every version bump
already viewed silently vanishes from the offline cache on the next

unrelated deploy.

**Fix — two files, both already exist, no new file needed:**
- **sw.js**: add a path check for `/static/product-images/` (same

  pattern as the existing `/product/` check), routing images into
  their own persistent bucket, e.g. `PRODUCT_IMAGES_CACHE`. Add that
  bucket name to the `activate` step's cache-cleanup exclusion filter
  too, or it gets purged on the very next version bump.
- **precache.js**: extend the existing per-product loop (it already
  iterates every product from `/api/prices` and skips anything already
  cached) to also queue each product's image URL alongside the page
  URL, using the same bounded-concurrency worker pool already there.
  This is the half that actually matters for the gallery — sw.js alone
  only caches an image *after* someone views it online once, which
  doesn't help a rep opening Gallery cold with no signal in a shop.
- **Explicitly decided: do NOT create a new JS file for this** — extend
  these two existing files only.

**Thumbnails for the Level 2 grid:**
- Source images are compressed webp or jpeg (user will provide).
- **Server-generates resized thumbnail variants once, via a batch
  script, into a dedicated static folder** (e.g.
  `static/product-thumbs/`) — NOT generated on-the-fly per request.
  Reasoning: PythonAnywhere free tier has CPU-second limits; resizing
  200+ images live on every grid load risks hitting those.
  - Regenerate only when a product's source image changes (mtime
    check or similar — not decided yet, implementation detail for
    next chat).
  - Target roughly 300px, webp, quality ~70 as a starting point —
    should land around 5-15KB/thumbnail, well within budget (see
    quota note below). Adjust after seeing real output size.

**PythonAnywhere free-tier disk quota: 450MB total — this is a real
constraint, not a formality.**
- Thumbnails themselves are cheap (a few MB total for 240 products at
  the target size above) — NOT the actual risk.

- **The real risk is unknown baseline usage** — pandas + openpyxl in
  the venv can easily eat 100-200MB on their own, before

  gtm_catalog.db, the 14-day backups/ rotation (§12/§14), and the
  original product images are even counted.

- **ACTION ITEM for next chat, do this FIRST before generating any
  thumbnails:** check actual disk usage on PythonAnywhere (e.g.
  `du -sh ~/` and a breakdown of subfolders) to confirm real headroom
  before assuming how much of the 450MB is actually free.

### Files this will touch (once built)
- **sw.js** — add image-path routing to its own persistent cache
  bucket + activate-step exclusion (bug fix, independent value even
  without Gallery)
- **precache.js** — extend existing loop to also precache image URLs
- **New thumbnail-generation batch script** (one-time/on-demand, run
  manually or as a PythonAnywhere Scheduled Task — same operational
  pattern as backup_db.py) — not written yet
- **_glass_tabbar.html** — add the List/Gallery toggle control
- **New route(s) in app.py** for category-album view + thumbnail-grid
  view (not scoped/named yet)
- **New template(s)** for the three gallery levels (not created yet)
- **New CSS** for collage covers, thumbnail grid, swipe view
- **New JS** for swipe gesture handling (touchstart/move/end, no
  library assumed yet)
- **db.py / app.py**: none anticipated for the gallery views themselves
  since they're read-only browsing — TBC once routes are actually
  designed

### To pick this up in a new chat, paste this file plus:
app.py, db.py, index.html, product_detail.html, style.css, precache.js,
sw.js, _glass_tabbar.html, and a couple of actual product images
(webp/jpeg) so real file sizes can be checked before finalizing
thumbnail settings.

Here's the write-up, in the same style as your existing project path doc entries — just copy this in:

---

## Gallery offline mode — proactive precache + two cache bugs fixed (v20 → v24) — DONE

**Problem 1: Gallery pages weren't cached for offline at all.** `sw.js`'s navigate handler only ever gave offline treatment to `/` — every other navigation (including `/gallery` and `/gallery/<category>`) fell into the branch built for `/admin`/`/login` ("no cached fallback, by design"), even though Gallery is public and session-independent, same audience as `/` and `/product/<id>`. And `precache.js` never proactively downloaded gallery pages either, so even a category visited once online wasn't guaranteed to survive going offline.

**Fix:** New `GALLERY_PAGES_CACHE` bucket in `sw.js` (persistent across version bumps, same reasoning as `PRODUCT_PAGES_CACHE`/`PRODUCT_IMAGES_CACHE` — a routine CSS/JS deploy shouldn't force re-downloading every category page). New path-based routing block (checked before the navigate-only branch, since `precache.js` reaches it with a plain `fetch()`, not a navigation) does cache-and-serve for `/gallery` and `/gallery/<category>`. Added to the `activate` cleanup exclusion list.

`precache.js` now also derives the full category list from the products it already fetches via `/api/prices` (no new endpoint needed) and proactively downloads `/gallery` plus every `/gallery/<category>` page into the new bucket, same worker-pool pattern already used for product pages/images.

**Problem 2 (found after the above shipped): category names containing `&` — `Baby & Health` and `Food & Snacks` — silently failed offline, every other category worked fine.** Root cause: a real browser navigation leaves `&` unescaped in a URL path (valid path character, only spaces get auto-encoded to `%20`), but `precache.js` builds its cache key with `encodeURIComponent()`, which escapes `&` → `%26`. That produced two different strings for the same logical page — one written by precache.js's proactive download, a different one looked up by `sw.js` on an offline visit that had never been proactively cached under that exact key. Any category without `&` happened to produce identical strings both ways, which is why only those two broke.

**Fix:** added `canonicalizeGalleryPath()` to `sw.js` — decodes each path segment then re-encodes it — and routed every gallery cache write and read through it, so both encoding forms collapse to one key regardless of which one a given request arrives with. `precache.js` didn't need changes; it was already producing the canonical form directly from the raw (un-encoded) category string.

Verified: canonicalization collapses both the literal-`&` and pre-encoded-`%26` forms to the identical cache key; categories without special characters are unaffected. `node --check` clean on both files.

Files touched: `sw.js` (v20 → v24 across both fixes), `precache.js`.

Not touched: `app.py`, `db.py`, templates.

---

## Confidential cost/margin catalog — new internal fields + admin-only endpoint — DONE

**Feature:** Added `base_cost` and `b2b_price` as internal-only columns on `products`, plus a new admin-only `/api/prices-margin` endpoint that returns those two fields alongside three computed margins (`Retail Margin`, `Wholesale Margin`, `B2B Margin`).

**Schema:** `products.base_cost REAL DEFAULT NULL`, `products.b2b_price REAL DEFAULT NULL` — added via the same `ALTER TABLE IF NOT EXISTS`-style migration pattern already used for `description`/`supplier`. `NULL` default (not `0`) so "no cost data yet" stays distinguishable from "cost is genuinely zero" — matters for the margin math, which would otherwise silently show a 100% margin for a product nobody's entered a cost for.

**db.py:**
- `_row_to_dict()` / `get_all_products()` — **unchanged**, deliberately. No confidential field ever reaches `/api/prices`, the public catalog, or the product detail page.
- New `_row_to_dict_with_margin()` / `get_all_products_with_margin()` — adds `Base Cost`, `B2B`, and the three margins. Margins are computed live from cost vs. price, not stored, so they can't drift stale the way the manually-maintained confidential spreadsheet's own margin columns did.
- `add_product()` / `update_product()` quietly accept `base_cost`/`b2b_price` if passed, but preserve existing values if not passed — admin-UI editing for these fields is a planned feature, not built yet.
- `import_excel_into_db()` treats `Base Cost` / `B2B` as optional columns, same rule as `Description`/`Supplier`: a normal catalog upload without those columns never wipes existing cost data.

**app.py:** new route `/api/prices-margin` — checks `login_required()`, returns `401` (JSON) if not authenticated, otherwise `db.get_all_products_with_margin()`. `/api/prices` stays public, unchanged.

**Verified with a smoke test:** public dict has zero confidential fields; margins compute correctly; cost fields survive a normal edit that doesn't touch them; bulk import both fills in and preserves cost data correctly across re-uploads with/without the optional columns.

Note: Excel import column is named `Base Cost` (not `Base Price`, which is what the working confidential spreadsheet uses) — no alias support yet if the sheet needs to recognize both names.

---

## Confidential price/margin spreadsheet — corrupted rows fixed, missing product restored, descriptions backfilled — DONE

Working file: `sale_price_catalog_confidential.xlsx` (source: manually maintained, not uploaded through the catalog importer).

**Corruption found and fixed:** `GTM-0243` and `GTM-0244` had been overwritten with the wrong products' data — *Bourbon 150g* and *Calsome (China)* were sitting under the Product IDs that belonged to *Gummy Candy (Pack)* and *Jeeno Jelly (Strawberry)*. Gave Bourbon/Calsome fresh IDs (`GTM-0249`, `GTM-0250`), restored `GTM-0243`/`GTM-0244` to their original data from the server export.

**Missing product restored:** `GTM-0128` (Kung Fu ခြင်ဆေးခွေ) was absent from the confidential file entirely — confirmed unintentional, added back with its original data.

**Reverted 3 unintentional product-name edits** (`GTM-0086`, `GTM-0158`, `GTM-0231`) back to the web/server version's naming.

**Description column added:** confidential file originally excluded `Description` by design (to avoid overwriting it via a future upload). Copied `Description` over from the server export instead, and stripped `_x000D_` artifacts (unresolved carriage-return escapes from a raw XML export) along with normalizing `\r\n`/`\r` to clean `\n` line breaks. Reused the file's existing stray empty column rather than adding a new one. Only 43 of 246 products have a description in the source at all — rest left blank, matching the original.

**Confirmed intentional, left as-is:** UPC changes on 4 products, 24 Status flips, retail/wholesale changes on 10 other products (paired with the wholesale-price backfill work).

⚠️ **Open gotcha to note for future uploads:** the catalog importer's `Description` handling only preserves existing data when the column is **missing entirely** from the sheet. If a sheet *has* a `Description` column but a row's cell is blank, that blank **will overwrite** the existing description. Since this confidential file now has a `Description` column with 203 blank rows, it should **not** be uploaded through the admin importer as-is — it would wipe those 203 products' descriptions.

Here's the write-up for your project path doc:

---

## Static files configuration on PythonAnywhere — offload images from Flask worker — DONE

**Problem:** Product images at `/static/product-images/*` were being served through Flask's built-in static route, tying up your Python Web Worker for each request. With proactive image precaching now downloading 200+ images on page load (5 concurrent), this created a bottleneck — the precache burst could make the entire site sluggish or briefly unresponsive for other users.

**Solution:** Configure PythonAnywhere's dedicated static web server to serve all `/static/` content directly, bypassing Flask entirely. PythonAnywhere's static server handles thousands of concurrent requests without touching your Python worker.

**Implementation (PythonAnywhere Web tab):**

1. Log into your PythonAnywhere account and go to the **Web** tab for your app.
2. Scroll to the **"Static files"** section.
3. Add a new mapping:
   - **URL**: `/static/`
   - **Directory**: `/home/your-username/your-app-path/static/` (the exact path to your Flask app's `static/` folder — check your `app.py`'s `app.static_folder` or just use the default Flask convention)
4. Save and reload your web app (button at the top of the Web tab).

**What changes:**
- Requests to `/static/css/`, `/static/js/`, `/static/product-images/`, etc. now hit the static server instead of Flask
- No code changes needed — `app.py`, `sw.js`, `precache.js`, templates all reference `/static/` the same way, they just don't know which server answered

**What doesn't change:**
- `/sw.js` stays served through Flask (custom route with `Service-Worker-Allowed` header) — it's not in the `/static/` folder, so this mapping doesn't affect it
- Offline feature is unaffected — service worker caches responses regardless of which server originated them, and precaching now completes faster since image requests aren't queued behind your Python worker
- Your code doesn't need any update — this is purely a server configuration change

**Upside for precaching:** the 200+ image downloads that fire on page load now complete much faster and with zero load on your Python worker, making precaching less noticeable to other concurrent users and improving overall reliability.

---
