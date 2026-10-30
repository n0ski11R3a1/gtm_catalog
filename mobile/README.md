# GTM Field Orders (Flutter)

Android-first offline order capture client. The app talks to Flask through
the authenticated `/api/mobile/*` endpoints; it does not download the
server's SQLite database.

## Current functionality

- One-time online sign-in using an admin-configured rep username/password.
- One active bearer token per rep/device. The server stores only its SHA-256
  hash; the token is kept in Android secure storage.
- App unlock on launch/resume using the phone's configured device
  credential (PIN, pattern, or password; biometrics may also be offered).
- SQLCipher-encrypted local database for catalog, order headers, and
  order-time line snapshots. The random database key is held in Android
  secure storage.
- The encrypted local database is bound to the first rep enrolled on it.
  Clear Android app data before enrolling a different rep, or that rep
  could otherwise see prior locally retained receipts.
- Offline catalog search and order capture; order saves are transactional.
- Manual Sync sends queued orders, then refreshes catalog. Every order has
  a UUID so retrying after a network interruption is idempotent.
- Orders stay on device after upload. Server-detected catalog/price changes
  are preserved and marked for admin review.

## Kali Linux toolchain and Android project

For Kali Linux, `tool/setup_kali_android.sh` installs the stable Flutter
SDK and Android SDK command-line tools (no Android Studio or emulator),
accepts SDK licenses interactively, and installs the Android platform and
build tools. Downloads are retried and cached so rerunning the script after
a network interruption can resume large archives. It also generates the
Android host, prepares its security settings, runs Flutter checks, and
builds a debug APK by default.

```sh
bash tool/setup_kali_android.sh
```

The script installs system packages with `sudo`, so run it from an account
with sudo access. Keep the terminal open while it runs; it may pause for
your password or Android SDK license acceptance. Full output is saved to
`~/.cache/gtm-field-orders-setup/setup.log`. Rerun the same command after
an interruption. To install only the toolchain without preparing/building
the app, use `--tools-only`; to prepare and test but skip APK compilation,
use `--skip-build`.

Defaults are `~/development/flutter`, `~/Android/Sdk`, and Android API 36.
Override them for the current run with `FLUTTER_DIR`, `ANDROID_SDK_ROOT`,
or `ANDROID_API`. After installation, open a new terminal (or source the
shell configuration file the script updated) to use Flutter and Android
tools outside the script.

The debug APK is written to
`build/app/outputs/flutter-apk/app-debug.apk`. It is for device testing,
not release distribution. Release APKs must be signed with a private
release keystore; do not put that keystore or its passwords in this
repository.

The standalone preparation script can also be rerun after manually
regenerating the Android host:

```sh
python3 tool/prepare_android.py
```

It changes the generated `MainActivity` to extend `FlutterFragmentActivity`
(required by `local_auth`), adds the biometric permission, and disables
Android app backup. A restored encrypted DB without its Keystore key is
unusable, and rep credentials must not migrate through cloud backup.

To run checks/build manually:

```sh
dart format lib
flutter analyze
flutter test
flutter build apk --debug
```

Install the debug APK on a connected phone with:

```sh
flutter install
```

The debug APK is for device testing, not distribution. A distributable
release APK must be signed with a private release keystore. Keep that
keystore and its passwords outside this repository; release signing should
be configured once the organization has chosen how to securely manage and
back up the signing key.

`FieldOrdersHome._enroll()` accepts only HTTPS server URLs. A debug-only
local HTTP exception is intentionally not included.

## Admin setup

In the existing web admin, go to Sales Team → Add/Edit Rep and set a unique
mobile username and password (minimum 8 characters). Share that login
privately with that rep. Changing the password or deactivating the rep
revokes the phone's current token; its locally queued orders remain on the
phone and can sync after the rep is re-enabled and signs in again.

Only one current token is allowed per rep. Signing in on another device
revokes the previous device's authorization. The current mobile app has no
account/password reset flow; an admin must set a new password.

## Server API used by this client

- `POST /api/mobile/login`: username/password → bearer token and rep
  identity.
- `GET /api/mobile/catalog`: authenticated public-safe product snapshot
  and immutable snapshot ID.
- `POST /api/mobile/sync`: authenticated batch of at most 50 orders;
  per-order success/error results. The client verifies every response
  maps to one distinct submitted order before changing local sync states.

The server stores a catalog snapshot the first time a version is fetched.
When syncing an offline order, it calculates totals from the stored
snapshot (not from client-supplied prices), compares the items with current
product prices/status/generation, saves the order-time prices, and flags
mismatches for admin review.

## Known next verification

This app has not yet been built with Flutter or exercised against the
production deployment. Before field rollout, generate the Android host,
run Dart formatting/analyzer and Flutter tests, test airplane-mode capture
and interrupted/retried sync on a phone, deploy the Flask migration, set up
rep credentials, and verify HTTPS plus the server URL. Do not send real
rep passwords or API tokens in source control or chat.
