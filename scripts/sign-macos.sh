#!/usr/bin/env bash
# Sign, notarize and package the unsigned publikclip.app that publik-build.yml makes (artifact
# publikclip-macos-arm64-unsigned). It does what the release Mac did by hand for 0.2.4 to 0.2.9
# (docs/updates.md). Nothing opens a window: codesign, notarytool, stapler and hdiutil only, so it
# is safe on a Mac someone is using. scripts/publik-release.sh calls it; it also runs alone.
#
# Usage: scripts/sign-macos.sh <publikclip-macos-arm64-unsigned.app.zip> <out dir>
# Env:   NOTARY_PROFILE (notarytool keychain profile, default "publik"),
#        APPLE_SIGNING_IDENTITY (default: the Developer ID below),
#        TAURI_SIGNING_PRIVATE_KEY(+_PASSWORD) or the "publik-update-key" keychain item, and
#        TAURI_CLI (scripts/updater-pack-macos.sh; SKIP_UPDATER=1 skips the update bundle).
# Out:   <out>/publikclip_<version>_aarch64.dmg (signed, notarized, stapled; the app inside too),
#        <out>/publikclip_<version>_aarch64.app.tar.gz + .sig (the same stapled app, for the
#        updater; see docs/updates.md).
set -euo pipefail
ZIP="${1:?unsigned app zip}"
OUT="${2:?out dir}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME=publikclip
IDENTITY="${APPLE_SIGNING_IDENTITY:-Developer ID Application: Mann Bellani (R5R3ZS54LV)}"
PROFILE="${NOTARY_PROFILE:-publik}"
ENTITLEMENTS="$ROOT/app/src-tauri/entitlements.plist"
case "$IDENTITY" in "Developer ID Application:"*) ;; *) echo "Refusing: '$IDENTITY' is not a Developer ID Application identity." >&2; exit 1;; esac
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d)"
trap 'hdiutil detach "$WORK/mnt" -quiet 2>/dev/null || true; rm -rf "$WORK"' EXIT

notarize() {
  local file="$1" log="$WORK/notary.log"
  xcrun notarytool submit "$file" --keychain-profile "$PROFILE" --wait 2>&1 | tee "$log"
  grep -q "status: Accepted" "$log" || { echo "Notarization of $(basename "$file") was not accepted." >&2; exit 1; }
}

ditto -x -k "$ZIP" "$WORK/x"
APP="$WORK/x/$NAME.app"
[ -d "$APP" ] || { echo "No $NAME.app in $ZIP" >&2; exit 1; }
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
MAIN="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist")"
[ "$(lipo -archs "$APP/Contents/MacOS/$MAIN")" = arm64 ] || { echo "$MAIN is not an arm64 build" >&2; exit 1; }
echo "==> $NAME $VERSION: signing with $IDENTITY"
# Inner code first: every Mach-O file in the bundle other than the main executable. In
# publikclip that is the bundled uv (Contents/Resources/resources/bin/uv). It gets the hardened
# runtime and the app's entitlements, as in 0.2.4 to 0.2.9: uv starts the pipeline's Python.
n=0
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    echo "    inner code: ${f#"$APP/"}"
    codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$f"
    n=$((n + 1))
  fi
done < <(find "$APP/Contents" -type f ! -path "$APP/Contents/MacOS/$MAIN" -print0)
[ "$n" -ge 1 ] || { echo "No inner code found: the bundled uv is missing" >&2; exit 1; }
# Then the app: hardened runtime, app/src-tauri/entitlements.plist, a secure timestamp.
codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$APP"
codesign --verify --strict --deep --verbose=2 "$APP"

echo "==> Notarizing the app"
ditto -c -k --keepParent "$APP" "$WORK/$NAME.zip"
notarize "$WORK/$NAME.zip"
rm -f "$WORK/$NAME.zip"
xcrun stapler staple "$APP"

# The update bundle is the stapled app itself, so an update keeps the Developer ID identity.
if [ "${SKIP_UPDATER:-0}" != 1 ]; then
  echo "==> Update bundle"
  "$ROOT/scripts/updater-pack-macos.sh" "$APP" "$OUT"
fi

echo "==> Building the dmg (APFS, UDZO, the app and an Applications link, as 0.2.9)"
STAGE="$WORK/stage"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/$NAME.app"
ln -s /Applications "$STAGE/Applications"
DMG="$OUT/${NAME}_${VERSION}_aarch64.dmg"
rm -f "$DMG"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -fs APFS -format UDZO -ov "$DMG" >/dev/null
rm -rf "$STAGE"
codesign --force --sign "$IDENTITY" --timestamp "$DMG"
echo "==> Notarizing the dmg"
notarize "$DMG"
xcrun stapler staple "$DMG"

echo "==> Checks (no launch)"
xcrun stapler validate "$DMG"
spctl -a -vv -t open --context context:primary-signature "$DMG"
mkdir -p "$WORK/mnt"
hdiutil attach -nobrowse -noautoopen -readonly -mountpoint "$WORK/mnt" "$DMG" >/dev/null
xcrun stapler validate "$WORK/mnt/$NAME.app"
spctl -a -vv -t exec "$WORK/mnt/$NAME.app"
codesign -dvv "$WORK/mnt/$NAME.app" 2>&1 | grep -E "Authority=Developer ID Application|flags=|TeamIdentifier"
hdiutil detach "$WORK/mnt" -quiet
shasum -a 256 "$DMG"
echo "Dmg: $DMG"
