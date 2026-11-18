#!/usr/bin/env bash
# Checks the update files of one publikclip release against its dmg (docs/updates.md). Read-only,
# opens no window (the dmg is mounted with -nobrowse). clean-mac-check.yml runs it on a clean
# runner; the release Mac can run it on the files of a draft before the draft is published.
#
# Usage: scripts/updater-check-release.sh <dir> <version> <tag> [owner/repo]
#   <dir> holds publikclip_<v>_aarch64.dmg, publikclip_<v>_aarch64.app.tar.gz(.sig),
#   publikclip_<v>_x64-setup.exe(.sig) and latest.json, as the release carries them.
# Checks:
#   - latest.json is what scripts/updater-latest-json.mjs writes for these files: version <v>,
#     the absolute GitHub asset URLs of <tag>, the signatures of the .sig files; each signature
#     verifies with the pubkey in app/src-tauri/tauri.conf.json and names <v>.
#   - the app in the update bundle is the app in the dmg (same cdhash), and it is Developer ID
#     signed, notarized and stapled: what an update installs is what a download installs.
set -euo pipefail
DIR="${1:?dir with the release files}"; V="${2:?version}"; TAG="${3:?tag}"
REPO="${4:-${GITHUB_REPOSITORY:-Blueturboguy07/publikclip}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME=publikclip
DMG="$DIR/${NAME}_${V}_aarch64.dmg"
PKG="$DIR/${NAME}_${V}_aarch64.app.tar.gz"
WIN="$DIR/${NAME}_${V}_x64-setup.exe"
FEED="$DIR/latest.json"
FAILS=0
ok() { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
for f in "$DMG" "$PKG" "$PKG.sig" "$WIN" "$WIN.sig" "$FEED"; do
  [ -s "$f" ] || bad "the release carries $(basename "$f")"
done
[ "$FAILS" -eq 0 ] || exit 1
WORK="$(mktemp -d)"
trap 'hdiutil detach "$WORK/m" -quiet 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "==> latest.json"
if node "$ROOT/scripts/updater-latest-json.mjs" --version "$V" --base-url "https://github.com/$REPO/releases/download/$TAG/" \
     --platform "darwin-aarch64=$PKG" --platform "windows-x86_64=$WIN" --notes "-" --out "$WORK/expected.json"; then
  ok "both signatures verify with the publikclip update key and name $V"
  if python3 - "$FEED" "$WORK/expected.json" <<'PY'
import json, sys
got = json.load(open(sys.argv[1]))
want = json.load(open(sys.argv[2]))
bad = []
if got.get("version") != want["version"]:
    bad.append(f"version {got.get('version')!r}, want {want['version']!r}")
platforms = got.get("platforms") or {}
for target, entry in want["platforms"].items():
    g = platforms.get(target)
    if not g:
        bad.append(f"no {target} entry")
        continue
    if g.get("url") != entry["url"]:
        bad.append(f"{target} url {g.get('url')!r}, want {entry['url']!r}")
    if g.get("signature") != entry["signature"]:
        bad.append(f"{target} signature is not the one in the .sig file")
extra = sorted(set(platforms) - set(want["platforms"]))
if extra:
    bad.append(f"unexpected entries {extra}")
for b in bad:
    print("      " + b)
sys.exit(1 if bad else 0)
PY
  then ok "latest.json names $V, this release's asset URLs and the .sig signatures"
  else bad "latest.json matches the release files"; fi
else
  bad "the signatures verify with the publikclip update key and name $V"
fi

echo "==> The update bundle is the app in the dmg"
mkdir -p "$WORK/x"
tar -xzf "$PKG" -C "$WORK/x"
UAPP="$WORK/x/$NAME.app"
hdiutil attach "$DMG" -mountpoint "$WORK/m" -nobrowse -readonly -quiet
DAPP="$WORK/m/$NAME.app"
cdhash() { codesign -dvvv "$1" 2>&1 | sed -n 's/^CDHash=//p'; }
if [ -d "$UAPP" ] && [ -d "$DAPP" ]; then
  U="$(cdhash "$UAPP")"; D="$(cdhash "$DAPP")"
  if [ -n "$U" ] && [ "$U" = "$D" ]; then ok "same code as the dmg's app (cdhash $U)"; else bad "same code as the dmg's app (update $U, dmg $D)"; fi
  P="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$UAPP/Contents/Info.plist")"
  if [ "$P" = "$V" ]; then ok "the update bundle's app is $V"; else bad "the update bundle's app is $V (it is $P)"; fi
  if codesign --verify --strict --deep "$UAPP"; then ok "its signature is intact"; else bad "its signature is intact"; fi
  if spctl --assess --type execute -vv "$UAPP" 2>&1 | tee "$WORK/spctl.txt" | grep -q "Notarized Developer ID"; then
    ok "Gatekeeper: Notarized Developer ID"
  else
    bad "Gatekeeper: Notarized Developer ID ($(tr '\n' ' ' < "$WORK/spctl.txt"))"
  fi
  if xcrun stapler validate -q "$UAPP"; then ok "the notarization ticket is stapled"; else bad "the notarization ticket is stapled"; fi
else
  bad "the update bundle holds $NAME.app and the dmg holds $NAME.app"
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "UPDATE FILES: PASS"; else echo "UPDATE FILES: FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
