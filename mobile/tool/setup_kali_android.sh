#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Install Flutter and the Android SDK command-line tools on Kali Linux, then
prepare and optionally build the GTM Field Orders Android app.

Usage:
  bash tool/setup_kali_android.sh [--tools-only] [--skip-build]

Options:
  --tools-only   Install Flutter/Android tools, but do not modify/build the app.
  --skip-build   Prepare the Flutter project and run checks, but skip APK build.
  -h, --help     Show this help.

Optional environment overrides:
  FLUTTER_DIR       Flutter SDK install path (default: ~/development/flutter)
  ANDROID_SDK_ROOT  Android SDK path (default: ~/Android/Sdk)
  ANDROID_API       Android SDK API level to install (default: 36)

Downloads are cached and retried. Re-run the same command after an
interruption; completed Flutter/SDK installations and cached downloads
will be reused.
EOF
}

TOOLS_ONLY=false
SKIP_BUILD=false
for arg in "$@"; do
  case "$arg" in
    --tools-only) TOOLS_ONLY=true ;;
    --skip-build) SKIP_BUILD=true ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
MOBILE_DIR="$REPO_ROOT/mobile"
FLUTTER_DIR="${FLUTTER_DIR:-$HOME/development/flutter}"
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"
ANDROID_API="${ANDROID_API:-36}"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/gtm-field-orders-setup"
mkdir -p -- "$CACHE_DIR" "$(dirname -- "$FLUTTER_DIR")" "$ANDROID_SDK_ROOT"
LOG_FILE="$CACHE_DIR/setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

download() {
  local url="$1"
  local destination="$2"
  local http_status
  local curl_status=0
  http_status="$(curl --fail --location --show-error \
    --connect-timeout 30 \
    --retry 20 --retry-all-errors --retry-delay 10 \
    --continue-at - \
    --output "$destination" \
    --write-out '%{http_code}' \
    "$url")" || curl_status=$?
  if [[ "$curl_status" -eq 0 ]]; then
    return 0
  fi
  if [[ "$http_status" == 416 && -s "$destination" ]]; then
    printf 'Cached download is already complete; checksum will verify it.\n'
    return 0
  fi
  return "$curl_status"
}

download_metadata() {
    local url="$1"
    local destination="$2"
    curl --fail --location --show-error \
      --connect-timeout 30 \
      --retry 20 --retry-all-errors --retry-delay 10 \
      --output "$destination" \
      "$url"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Required command is missing: %s\n' "$1" >&2
    exit 1
  }
}

printf 'Setup log: %s\n' "$LOG_FILE"
printf 'Flutter destination: %s\n' "$FLUTTER_DIR"
printf 'Android SDK destination: %s\n' "$ANDROID_SDK_ROOT"

if ! command -v curl >/dev/null 2>&1 ||
   ! command -v git >/dev/null 2>&1 ||
   ! command -v unzip >/dev/null 2>&1 ||
   ! command -v xz >/dev/null 2>&1 ||
   ! command -v python3 >/dev/null 2>&1 ||
   ! command -v javac >/dev/null 2>&1; then
  require_command sudo
  sudo apt-get update \
    -o Acquire::Retries=10 \
    -o Acquire::http::Timeout=60 \
    -o Acquire::https::Timeout=60
  sudo apt-get install -y \
    -o Acquire::Retries=10 \
    curl git unzip xz-utils zip python3 default-jdk
fi
if [[ ! "$ANDROID_API" =~ ^[0-9]+$ ]]; then
  printf 'ANDROID_API must be a numeric API level (received: %s).\n' "$ANDROID_API" >&2
  exit 2
fi

require_command curl
require_command git
require_command unzip
require_command xz
require_command python3
require_command javac

JAVA_BIN="$(readlink -f -- "$(command -v javac)")"
JAVA_HOME="$(dirname -- "$(dirname -- "$JAVA_BIN")")"
export JAVA_HOME
export ANDROID_HOME="$ANDROID_SDK_ROOT"
export ANDROID_SDK_ROOT
export PATH="$FLUTTER_DIR/bin:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$PATH"

if [[ ! -x "$FLUTTER_DIR/bin/flutter" ]]; then
  if [[ -e "$FLUTTER_DIR" ]]; then
    printf 'Flutter destination exists but is not a complete SDK: %s\n' "$FLUTTER_DIR" >&2
    printf 'Move it aside yourself or set FLUTTER_DIR to another path, then rerun.\n' >&2
    exit 1
  fi

  RELEASES_JSON="$CACHE_DIR/releases_linux.json"
  download_metadata \
    "https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json" \
    "$RELEASES_JSON"

  read -r FLUTTER_ARCHIVE_PATH FLUTTER_ARCHIVE FLUTTER_SHA256 < <(
    python3 - "$RELEASES_JSON" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as release_file:
    data = json.load(release_file)

current_hash = data["current_release"]["stable"]
release = next(
    item for item in data["releases"]
    if item["hash"] == current_hash and item["channel"] == "stable"
    and item.get("archive", "").startswith("stable/linux/")
)
archive_path = release["archive"]
archive = archive_path.rsplit("/", 1)[-1]
if archive_path != f"stable/linux/{archive}" or not re.fullmatch(
    r"flutter_linux_[A-Za-z0-9._-]+-stable\.tar\.xz", archive
):
    raise SystemExit(f"Unexpected Flutter archive name: {archive}")
sha256 = release.get("sha256")
if not sha256 or not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
    raise SystemExit("Flutter release metadata has no valid SHA-256 checksum.")
print(archive_path, archive, sha256)
PY
  )
  if [[ -z "$FLUTTER_ARCHIVE_PATH" || -z "$FLUTTER_ARCHIVE" || -z "$FLUTTER_SHA256" ]]; then
    printf 'Could not identify the current stable Flutter Linux release.\n' >&2
    exit 1
  fi

  FLUTTER_URL="https://storage.googleapis.com/flutter_infra_release/releases/$FLUTTER_ARCHIVE_PATH"
  FLUTTER_PACKAGE="$CACHE_DIR/$FLUTTER_ARCHIVE"
  if ! printf '%s  %s\n' "$FLUTTER_SHA256" "$FLUTTER_PACKAGE" | sha256sum --check --status; then
    download "$FLUTTER_URL" "$FLUTTER_PACKAGE"
  else
    printf 'Verified cached Flutter archive; reusing it.\n'
  fi
  if ! printf '%s  %s\n' "$FLUTTER_SHA256" "$FLUTTER_PACKAGE" | sha256sum --check --status; then
    printf 'Flutter download checksum failed; removing the corrupt cached archive.\n' >&2
    rm -f -- "$FLUTTER_PACKAGE"
    download "$FLUTTER_URL" "$FLUTTER_PACKAGE"
    printf '%s  %s\n' "$FLUTTER_SHA256" "$FLUTTER_PACKAGE" | sha256sum --check
  fi

  FLUTTER_STAGE="$(mktemp -d "$(dirname -- "$FLUTTER_DIR")/.flutter-install.XXXXXX")"
  tar -xJf "$FLUTTER_PACKAGE" -C "$FLUTTER_STAGE"
  if [[ ! -x "$FLUTTER_STAGE/flutter/bin/flutter" ]]; then
    printf 'Flutter archive did not contain the expected SDK layout.\n' >&2
    exit 1
  fi
  mv -- "$FLUTTER_STAGE/flutter" "$FLUTTER_DIR"
  rmdir -- "$FLUTTER_STAGE"
  rm -f -- "$FLUTTER_PACKAGE" "$RELEASES_JSON"
else
  printf 'Flutter SDK already installed; reusing it.\n'
fi

if [[ ! -x "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]]; then
  if [[ -e "$ANDROID_SDK_ROOT/cmdline-tools/latest" ]]; then
    printf 'Android cmdline-tools/latest exists but sdkmanager is missing.\n' >&2
    printf 'Move that incomplete directory aside yourself, then rerun.\n' >&2
    exit 1
  fi

  ANDROID_REPOSITORY_XML="$CACHE_DIR/repository2-1.xml"
  download_metadata \
    "https://dl.google.com/android/repository/repository2-1.xml" \
    "$ANDROID_REPOSITORY_XML"
  IFS='|' read -r TOOLS_URL CHECKSUM_TYPE CHECKSUM < <(
    python3 - "$ANDROID_REPOSITORY_XML" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
def local_name(element):
    return element.tag.rsplit("}", 1)[-1]

def children(element, name):
    return [child for child in element if local_name(child) == name]

def text_child(element, name):
    matches = children(element, name)
    return matches[0].text if matches else None

package = next(
    item for item in root.iter()
    if local_name(item) == "remotePackage"
    if item.attrib.get("path") == "cmdline-tools;latest"
)
found = False
for archive in package.iter():
    if local_name(archive) != "archive":
        continue
    if text_child(archive, "host-os") != "linux":
        continue
    completes = children(archive, "complete")
    if not completes:
        continue
    complete = completes[0]
    url = text_child(complete, "url")
    if not url or not url.startswith("commandlinetools-linux-") or not url.endswith(".zip"):
        continue
    checksums = children(complete, "checksum")
    checksum = checksums[0] if checksums else None
    if checksum is not None and checksum.text:
        checksum_value = checksum.text.strip()
        checksum_type = checksum.attrib.get("type", "").lower()
        if not checksum_type:
            checksum_type = {
                40: "sha1",
                64: "sha256",
            }.get(len(checksum_value), "")
        print("|".join((
            "https://dl.google.com/android/repository/" + url,
            checksum_type,
            checksum_value,
        )))
    else:
        print("https://dl.google.com/android/repository/" + url + "||")
    found = True
    break
if not found:
    raise SystemExit("No Linux Android command-line tools archive found.")
PY
  )
  if [[ -z "$TOOLS_URL" ]]; then
    printf 'Could not identify the Android Linux command-line tools download.\n' >&2
    exit 1
  fi

  TOOLS_PACKAGE="$CACHE_DIR/${TOOLS_URL##*/}"
  case "$CHECKSUM_TYPE" in
    sha1)
      if printf '%s  %s\n' "$CHECKSUM" "$TOOLS_PACKAGE" | sha1sum --check --status; then
        printf 'Verified cached Android command-line tools; reusing them.\n'
      else
        download "$TOOLS_URL" "$TOOLS_PACKAGE"
      fi
      if ! printf '%s  %s\n' "$CHECKSUM" "$TOOLS_PACKAGE" | sha1sum --check --status; then
        printf 'Android command-line tools checksum failed; removing cached archive.\n' >&2
        rm -f -- "$TOOLS_PACKAGE"
        download "$TOOLS_URL" "$TOOLS_PACKAGE"
        printf '%s  %s\n' "$CHECKSUM" "$TOOLS_PACKAGE" | sha1sum --check
      fi
      ;;
    sha256)
      if printf '%s  %s\n' "$CHECKSUM" "$TOOLS_PACKAGE" | sha256sum --check --status; then
        printf 'Verified cached Android command-line tools; reusing them.\n'
      else
        download "$TOOLS_URL" "$TOOLS_PACKAGE"
      fi
      if ! printf '%s  %s\n' "$CHECKSUM" "$TOOLS_PACKAGE" | sha256sum --check --status; then
        printf 'Android command-line tools checksum failed; removing cached archive.\n' >&2
        rm -f -- "$TOOLS_PACKAGE"
        download "$TOOLS_URL" "$TOOLS_PACKAGE"
        printf '%s  %s\n' "$CHECKSUM" "$TOOLS_PACKAGE" | sha256sum --check
      fi
      ;;
    "")
      printf 'Android tools metadata supplied no supported checksum; continuing with HTTPS download.\n'
      ;;
    *)
      printf 'Unsupported Android tools checksum type: %s\n' "$CHECKSUM_TYPE" >&2
      exit 1
      ;;
  esac

  mkdir -p -- "$ANDROID_SDK_ROOT/cmdline-tools"
  TOOLS_STAGE="$(mktemp -d "$ANDROID_SDK_ROOT/cmdline-tools/.install.XXXXXX")"
  unzip -q "$TOOLS_PACKAGE" -d "$TOOLS_STAGE"
  if [[ ! -x "$TOOLS_STAGE/cmdline-tools/bin/sdkmanager" ]]; then
    printf 'Android command-line tools archive has an unexpected layout.\n' >&2
    exit 1
  fi
  mv -- "$TOOLS_STAGE/cmdline-tools" "$ANDROID_SDK_ROOT/cmdline-tools/latest"
  rmdir -- "$TOOLS_STAGE"
  rm -f -- "$TOOLS_PACKAGE" "$ANDROID_REPOSITORY_XML"
else
  printf 'Android command-line tools already installed; reusing them.\n'
fi

SDKMANAGER="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager"
printf '\nReview and accept the Android SDK license terms to continue.\n'
"$SDKMANAGER" --sdk_root="$ANDROID_SDK_ROOT" --licenses
"$SDKMANAGER" --sdk_root="$ANDROID_SDK_ROOT" --install \
  "cmdline-tools;latest" \
  "platform-tools" \
  "platforms;android-$ANDROID_API" \
  "build-tools;$ANDROID_API.0.0"

flutter config --android-sdk "$ANDROID_SDK_ROOT"
flutter precache --android
if ! flutter doctor -v; then
  printf 'Flutter Doctor reported outstanding checks; continuing with the CLI-only setup.\n'
fi

SHELL_NAME="${SHELL##*/}"
case "$SHELL_NAME" in
  zsh) SHELL_RC="$HOME/.zshrc" ;;
  bash) SHELL_RC="$HOME/.bashrc" ;;
  *) SHELL_RC="$HOME/.profile" ;;
esac
ENV_MARKER="# >>> GTM Field Orders Android SDK >>>"
if ! grep -Fq "$ENV_MARKER" "$SHELL_RC" 2>/dev/null; then
  {
    printf '\n%s\n' "$ENV_MARKER"
    printf 'export ANDROID_HOME=%q\n' "$ANDROID_SDK_ROOT"
    printf 'export ANDROID_SDK_ROOT=%q\n' "$ANDROID_SDK_ROOT"
    printf 'export JAVA_HOME=%q\n' "$JAVA_HOME"
    printf 'export PATH=%q:$PATH\n' "$FLUTTER_DIR/bin"
    printf 'export PATH=%q:$PATH\n' "$ANDROID_SDK_ROOT/cmdline-tools/latest/bin"
    printf 'export PATH=%q:$PATH\n' "$ANDROID_SDK_ROOT/platform-tools"
    printf '%s\n' "# <<< GTM Field Orders Android SDK <<<"
  } >> "$SHELL_RC"
  printf 'Added Flutter/Android environment variables to %s\n' "$SHELL_RC"
else
  printf 'Flutter/Android environment block already exists in %s\n' "$SHELL_RC"
fi

if [[ "$TOOLS_ONLY" == true ]]; then
  printf '\nToolchain setup complete. Open a new terminal or source %s.\n' "$SHELL_RC"
  exit 0
fi

cd -- "$MOBILE_DIR"
if [[ ! -d android ]]; then
  flutter create --platforms android --org com.gtm.catalog .
else
  printf 'Android host already exists; leaving generated project files in place.\n'
fi
python3 tool/prepare_android.py
flutter pub get
dart format lib
flutter analyze
flutter test

if [[ "$SKIP_BUILD" != true ]]; then
  flutter build apk --debug
  printf '\nDebug APK created at: %s/build/app/outputs/flutter-apk/app-debug.apk\n' "$MOBILE_DIR"
fi

printf '\nSetup complete. Full log: %s\n' "$LOG_FILE"
printf 'Debug APK is for testing only; release APKs require a privately protected signing key.\n'
