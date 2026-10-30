from __future__ import annotations

import argparse
import re
from pathlib import Path


def configure_android(project_dir: Path) -> None:
    manifest_path = project_dir / "android/app/src/main/AndroidManifest.xml"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"{manifest_path} not found. Run `flutter create --platforms android` first."
        )

    manifest = manifest_path.read_text(encoding="utf-8")
    if "android.permission.USE_BIOMETRIC" not in manifest:
        manifest, count = re.subn(
            r"(<manifest\b[^>]*>)",
            r'\1\n    <uses-permission android:name="android.permission.USE_BIOMETRIC" />',
            manifest,
            count=1,
        )
        if count != 1:
            raise ValueError(f"Could not find the manifest root in {manifest_path}.")

    application_match = re.search(r"<application\b[^>]*>", manifest)
    if application_match is None:
        raise ValueError(f"Could not find the application element in {manifest_path}.")
    application = application_match.group(0)
    backup_attribute = re.search(r'\bandroid:allowBackup\s*=\s*"[^"]*"', application)
    if backup_attribute:
        replacement = 'android:allowBackup="false"'
        manifest = (
            manifest[: application_match.start()]
            + application[: backup_attribute.start()]
            + replacement
            + application[backup_attribute.end() :]
            + manifest[application_match.end() :]
        )
    else:
        insert_at = application_match.end() - 1
        manifest = (
            manifest[:insert_at]
            + ' android:allowBackup="false"'
            + manifest[insert_at:]
        )
    manifest_path.write_text(manifest, encoding="utf-8")

    activities = list((project_dir / "android/app/src/main").rglob("MainActivity.kt"))
    activities += list((project_dir / "android/app/src/main").rglob("MainActivity.java"))
    if len(activities) != 1:
        raise FileNotFoundError(
            "Expected exactly one generated MainActivity.kt or MainActivity.java "
            f"under android/app/src/main; found {len(activities)}."
        )

    activity_path = activities[0]
    activity = activity_path.read_text(encoding="utf-8")
    activity = activity.replace(
        "import io.flutter.embedding.android.FlutterActivity",
        "import io.flutter.embedding.android.FlutterFragmentActivity",
    )
    activity = activity.replace(
        "extends FlutterActivity",
        "extends FlutterFragmentActivity",
    )
    activity = activity.replace(
        ": FlutterActivity()",
        ": FlutterFragmentActivity()",
    )
    if "FlutterFragmentActivity" not in activity:
        raise ValueError(
            f"Could not update the generated activity class in {activity_path}."
        )
    activity_path.write_text(activity, encoding="utf-8")

    print("Android host configured: screen-lock auth enabled; app backup disabled.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure generated Android files for GTM Field Orders."
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Flutter project directory (defaults to the mobile directory).",
    )
    args = parser.parse_args()
    configure_android(args.project_dir.resolve())


if __name__ == "__main__":
    main()
