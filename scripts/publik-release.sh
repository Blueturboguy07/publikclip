#!/usr/bin/env bash
# The whole publikclip release in one command, on the release Mac (docs/updates.md, "Cut a release"):
#
#   scripts/publik-release.sh <version>
#
# Before it: put <version> in the seven places a bump touches (app/src-tauri/tauri.conf.json,
# app/src-tauri/Cargo.toml, app/src-tauri/Cargo.lock, app/package.json, app/package-lock.json,
# pipeline/pyproject.toml, pipeline/uv.lock), commit, push to main. The release notes are the
# message of that version-bump commit (the last commit that changed tauri.conf.json), or the
# Markdown file named by PUBLIK_RELEASE_NOTES.
#
# What it does, for macOS arm64 and Windows x64 (the two systems publikclip ships):
#  1. Checks: on main, clean tree, pushed; the version in every file; higher than the last
#     release; the tag is free; the Developer ID, the notary profile and the update key are there.
#  2. Runs publik-build.yml on that commit (the unsigned Mac app; the Windows installer and its
#     update .sig) and waits. The repo's own CI (macos.yml, windows.yml) on the same commit must
#     pass too (it is started when the push did not start it; a failed run is re-run once).
#  3. Downloads the unsigned app and the installer.
#  4. scripts/sign-macos.sh: signs the app (the bundled uv first, then the app; hardened runtime,
#     app/src-tauri/entitlements.plist) with the Developer ID, notarizes and staples it, packs and
#     signs the update bundle from the stapled app, builds the dmg, signs, notarizes, staples it.
#  5. Writes latest.json (absolute GitHub URLs) and signs it with the update key (latest.json.sig).
#  6. Uploads everything to a DRAFT release, and runs clean-mac-check.yml on the draft.
#  7. Downloads the draft back and verifies it: asset list, checksums, the feed and every
#     signature with the app's public key, versions (Info.plist, exe), codesign, spctl, stapler,
#     the designated requirement and the entitlements against the last release, the publik app
#     token compiled in.
#  8. Publishes, then checks the tag and every feed URL.
#
# Run the same command again after a failure: it reuses the CI runs of the commit, the files in
# the work folder and the draft, and goes on from there. For a published release it only runs
# the checks of step 7. It stops before publishing on any failed check (the draft stays), starts
# no app, and prints no secret: the update key goes from the keychain into the signer's
# environment only.
#
# Env: PUBLIK_RELEASE_WORK  work folder (default <repo>/.release/v<version>, git-ignored);
#                           deleted after a successful publish
#      PUBLIK_RELEASE_NOTES Markdown file with the release notes (default: the bump commit message)
#      APPLE_SIGNING_IDENTITY, NOTARY_PROFILE (default "publik"), TAURI_CLI
#      UPDATE_KEY_SERVICE   keychain service of the update key (default "publik-update-key",
#                           accounts mann-private and mann-password)
#      PUBLIK_RELEASE_NO_PUBLISH=1  stop after the verify and leave the draft
set -euo pipefail

V="${1:?usage: scripts/publik-release.sh <version>}"
V="${V#v}"
TAG="v$V"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP=publikclip
SLUG=publikclip
BUNDLE_ID=com.publikhq.publikclip
BRANCH=main
IDENTITY="${APPLE_SIGNING_IDENTITY:-Developer ID Application: Mann Bellani (R5R3ZS54LV)}"
TEAM=R5R3ZS54LV
PROFILE="${NOTARY_PROFILE:-publik}"
SERVICE="${UPDATE_KEY_SERVICE:-publik-update-key}"
WORK="${PUBLIK_RELEASE_WORK:-$ROOT/.release/$TAG}"
DMG="${APP}_${V}_aarch64.dmg"
EXE="${APP}_${V}_x64-setup.exe"
TGZ="${APP}_${V}_aarch64.app.tar.gz"
ZIP="publikclip-macos-arm64-unsigned.app.zip"
ASSETS=("$DMG" "$EXE" "$TGZ" "$TGZ.sig" "$EXE.sig" latest.json latest.json.sig SHA256SUMS.txt)

say() { printf '\n==> %s\n' "$*"; }
ok() { printf '  ok    %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }
die() { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }
FAILS=0
bad() { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
# expect <description> <command...>: ok when the command succeeds, FAIL otherwise.
expect() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
plist() { /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist"; }
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
dr() { codesign -d -r- "$1" 2>&1 | sed -n 's/^designated => //p'; }
# A plist (XML on stdin) as JSON with sorted keys, so two plists compare as text.
plist_json() { plutil -convert json -o - - 2>/dev/null | node -e 'const o = JSON.parse(require("fs").readFileSync(0, "utf8")); const s = (v) => v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, s(v[k])])) : v; console.log(JSON.stringify(s(o)))'; }
ents() { codesign -d --entitlements - --xml "$1" 2>/dev/null | plist_json; }
stapled() { xcrun stapler validate "$1" >/dev/null 2>&1; }

[[ "$V" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "the version must be x.y.z, not $V"
for t in gh git node npm xcrun codesign hdiutil security shasum spctl ditto tar unzip file curl plutil lipo; do
  command -v "$t" >/dev/null || die "$t is not installed"
done
gh auth status >/dev/null 2>&1 || die "gh is not logged in"
mkdir -p "$WORK"
# gh for reads and downloads, with retries: the GitHub API limit (5000 calls an hour) is per user
# and other jobs of the same user share it; a 5xx or a dropped connection is retried too.
ghr() {
  local out err i
  for i in $(seq 1 45); do
    if out="$(gh "$@" 2> "$WORK/gh.err")"; then
      [ -z "$out" ] || printf '%s\n' "$out"
      return 0
    fi
    err="$(tr '\n' ' ' < "$WORK/gh.err")"
    case "$err" in
      *"rate limit"* | *"HTTP 5"* | *"timed out"* | *"timeout"* | *"connection"* | *"EOF"* | *"TLS"*) ;;
      *) printf '%s\n' "$err" >&2; return 1 ;;
    esac
    echo "    GitHub API: ${err:0:110}... waiting 90 s (try $i of 45)" >&2
    sleep 90
  done
  printf '%s\n' "$err" >&2
  return 1
}
REPO="$(ghr repo view --json nameWithOwner -q .nameWithOwner)"
BASE_URL="https://github.com/$REPO/releases/download/$TAG/"
RELEASE_PAGE="https://github.com/$REPO/releases/tag/$TAG"
STATE="$WORK/state"
touch "$STATE"
get_state() { sed -n "s/^$1=//p" "$STATE" | tail -1; }
set_state() { echo "$1=$2" >> "$STATE"; }

# ---------------------------------------------------------------------------------------------
say "$APP $V: checks"
RELEASE="$(ghr release view "$TAG" -R "$REPO" --json isDraft,targetCommitish 2>/dev/null || true)"
if [ -z "$RELEASE" ]; then RSTATE=none
elif [ "$(node -p 'JSON.parse(process.argv[1]).isDraft' "$RELEASE")" = true ]; then RSTATE=draft
else RSTATE=published; fi
ok "release $TAG: $RSTATE"
# The newest published release below $V (for a published $TAG: the one before it).
PREV_TAG="$(ghr release list -R "$REPO" --exclude-drafts --exclude-pre-releases -L 30 --json tagName -q '.[].tagName' |
  grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | awk -v v="$V" '{ t = substr($0, 2); split(t, a, "."); split(v, b, ".");
    x = (a[1] * 1000 + a[2]) * 1000 + a[3]; y = (b[1] * 1000 + b[2]) * 1000 + b[3];
    if (x < y && x > best) { best = x; tag = $0 } } END { print tag }')"
[ -n "$PREV_TAG" ] || die "no earlier published release"
ok "last published release before $V: $PREV_TAG"
git fetch -q origin "$BRANCH" --tags

if [ "$RSTATE" = published ]; then
  SHA="$(git rev-parse "$TAG^{commit}")"
  note "$TAG is already published: verifying it only"
else
  [ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || die "check out $BRANCH first"
  [ -z "$(git status --porcelain)" ] || die "the working tree is not clean"
  SHA="$(git rev-parse HEAD)"
  [ "$SHA" = "$(git rev-parse "origin/$BRANCH")" ] || die "HEAD is not origin/$BRANCH: push (or pull) first"
  ok "on $BRANCH at ${SHA:0:12}, clean, pushed"
  if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then die "the tag $TAG exists but has no published release"; fi
  ok "tag $TAG is free"
  if [ "$RSTATE" = draft ]; then
    TARGET="$(node -p 'JSON.parse(process.argv[1]).targetCommitish' "$RELEASE")"
    [ "$TARGET" = "$SHA" ] || die "the draft $TAG is for commit $TARGET, not HEAD $SHA: delete the draft or check out that commit"
    ok "resuming the draft $TAG"
  fi
  # The version in every file a bump touches.
  CONF_V="$(node -p 'require("./app/src-tauri/tauri.conf.json").version')"
  PKG_V="$(node -p 'require("./app/package.json").version')"
  LOCK1_V="$(node -p 'require("./app/package-lock.json").version')"
  LOCK2_V="$(node -p 'require("./app/package-lock.json").packages[""].version')"
  CARGO_V="$(sed -n 's/^version = "\(.*\)"$/\1/p' app/src-tauri/Cargo.toml | head -1)"
  CLOCK_V="$(awk '/^name = "publikclip-app"$/ { getline; gsub(/version = |"/, ""); print; exit }' app/src-tauri/Cargo.lock)"
  PY_V="$(sed -n 's/^version = "\(.*\)"$/\1/p' pipeline/pyproject.toml | head -1)"
  UV_V="$(awk '/^name = "publikclip-pipeline"$/ { getline; gsub(/version = |"/, ""); print; exit }' pipeline/uv.lock)"
  for pair in "app/src-tauri/tauri.conf.json:$CONF_V" "app/package.json:$PKG_V" "app/package-lock.json:$LOCK1_V" \
    "app/package-lock.json packages[\"\"]:$LOCK2_V" "app/src-tauri/Cargo.toml:$CARGO_V" "app/src-tauri/Cargo.lock:$CLOCK_V" \
    "pipeline/pyproject.toml:$PY_V" "pipeline/uv.lock:$UV_V"; do
    [ "${pair##*:}" = "$V" ] || die "${pair%:*} has version ${pair##*:}, not $V"
  done
  ok "version $V in tauri.conf.json, Cargo.toml, Cargo.lock, package.json, package-lock.json (2), pyproject.toml, uv.lock"
  [ "$(printf '%s\n%s\n' "${PREV_TAG#v}" "$V" | sort -V | tail -1)" = "$V" ] && [ "${PREV_TAG#v}" != "$V" ] ||
    die "$V is not higher than the last release ${PREV_TAG#v}"
  ok "$V is higher than ${PREV_TAG#v}"
  security find-identity -v -p codesigning | grep -qF "\"$IDENTITY\"" || die "no signing identity \"$IDENTITY\" in the keychain"
  ok "signing identity: $IDENTITY"
  security find-generic-password -s "$SERVICE" -a mann-private >/dev/null 2>&1 || die "no update key: keychain service $SERVICE, account mann-private"
  ok "update key: keychain item $SERVICE (not read yet)"
  xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1 || die "the notary profile \"$PROFILE\" does not work (xcrun notarytool store-credentials)"
  ok "notary profile: $PROFILE"
fi
# The work folder belongs to one commit: files from another commit's builds are never reused.
if [ "$RSTATE" != published ]; then
  if [ -n "$(get_state sha)" ] && [ "$(get_state sha)" != "$SHA" ]; then
    note "the work folder has the builds of $(get_state sha | cut -c1-12): starting it again for ${SHA:0:12}"
    rm -rf "$WORK/in" "$WORK/out" "$WORK/verify" "$WORK/notes.md" "$WORK/feed-notes.txt" "$WORK/release-notes.md"
    : > "$STATE"
  fi
  [ "$(get_state sha)" = "$SHA" ] || set_state sha "$SHA"
fi
FREE_KB="$(df -k "$WORK" | awk 'NR == 2 { print $4 }')"
if [ "$RSTATE" = published ]; then NEED_KB=400000; else NEED_KB=1500000; fi
[ "$FREE_KB" -ge "$NEED_KB" ] || die "only $((FREE_KB / 1024)) MB free on the disk of $WORK; this needs $((NEED_KB / 1024)) MB"
ok "disk: $((FREE_KB / 1024)) MB free"
TOKEN_FILE_AT_SHA="$(git show "$SHA:app/src-tauri/publik-app-token.txt" 2>/dev/null | tr -d '[:space:]')"
[ -n "$TOKEN_FILE_AT_SHA" ] || die "no app/src-tauri/publik-app-token.txt at ${SHA:0:12}: the app token check needs it"

# The Tauri CLI signs the update files: the repo's, else the locked version in the work folder.
TAURI="${TAURI_CLI:-}"
if [ "$RSTATE" != published ]; then
  for c in "$ROOT/app/node_modules/.bin/tauri" "$WORK/tauri-cli/node_modules/.bin/tauri"; do
    [ -z "$TAURI" ] && [ -x "$c" ] && TAURI="$c"
  done
  if [ -z "$TAURI" ]; then
    CLI_V="$(node -p 'require("./app/package-lock.json").packages["node_modules/@tauri-apps/cli"].version')"
    [ -n "$CLI_V" ] || die "no @tauri-apps/cli version in app/package-lock.json"
    npm install --silent --no-save --no-package-lock --no-audit --no-fund --prefix "$WORK/tauri-cli" "@tauri-apps/cli@$CLI_V" >/dev/null
    TAURI="$WORK/tauri-cli/node_modules/.bin/tauri"
  fi
  ok "Tauri CLI $("$TAURI" --version | awk '{ print $NF }')"
  export TAURI_CLI="$TAURI"
fi

# Runs a command with the update key in its environment. The key is never printed.
with_update_key() {
  if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then "$@"; return; fi
  local key pw
  key="$(security find-generic-password -s "$SERVICE" -a mann-private -w 2>/dev/null)" || die "cannot read the update key"
  pw="$(security find-generic-password -s "$SERVICE" -a mann-password -w 2>/dev/null || true)"
  TAURI_SIGNING_PRIVATE_KEY="$key" TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$pw" "$@"
}

# ---------------------------------------------------------------------------------------------
# At most one GitHub API call per 90 s while waiting: other jobs of the user share the limit.
POLL=90
run_field() { ghr run view "$1" -R "$REPO" --json "$2" -q ".$2"; }
# "status conclusion" of a run in one call.
run_state() { ghr run view "$1" -R "$REPO" --json status,conclusion -q '"\(.status) \(.conclusion)"'; }
# The newest run of a workflow on $SHA (any event: the push of the commit or a dispatch), or nothing.
last_run() {
  ghr run list -R "$REPO" --workflow "$1" --commit "$SHA" -L 20 \
    --json databaseId,createdAt,event -q '[.[] | select(.event == "push" or .event == "workflow_dispatch")] | sort_by(.createdAt) | reverse | .[0].databaseId // empty'
}
# Starts a workflow on $BRANCH (the extra arguments are -f inputs) and prints the new run's id.
start_run() {
  local wf="$1" before id
  shift
  before="$(ghr run list -R "$REPO" --workflow "$wf" -L 50 --json databaseId -q '.[].databaseId' | sort)"
  ghr workflow run "$wf" -R "$REPO" --ref "$BRANCH" "$@" >/dev/null
  sleep 15
  for _ in $(seq 1 10); do
    id="$(ghr run list -R "$REPO" --workflow "$wf" --commit "$SHA" --event workflow_dispatch -L 20 --json databaseId -q '.[].databaseId' |
      sort | comm -13 <(printf '%s\n' "$before") - | tail -1)"
    [ -n "$id" ] && { echo "$id"; return; }
    sleep "$POLL"
  done
  die "$wf did not start"
}
# Waits for a run to complete; its conclusion is then in $RUN_CONCLUSION.
wait_run() {
  local id="$1" t=0 st
  while :; do
    st="$(run_state "$id")"
    [ "${st%% *}" = completed ] && break
    [ $((t % (POLL * 4))) -eq 0 ] && echo "    run $id: ${st%% *} ($((t / 60)) min)"
    sleep "$POLL"; t=$((t + POLL))
    [ "$t" -lt 10800 ] || die "run $id did not finish in 180 minutes"
  done
  RUN_CONCLUSION="${st#* }"
}
# A failed run of the repo's own CI (pipeline tests, Windows runner) is re-run once.
can_rerun() { [ "$(get_state "rerun_$2")" != "done" ]; }
rerun_failed() {
  echo "    $1 run $2 failed; re-running its failed jobs once" >&2
  set_state "rerun_$2" "done"
  ghr run rerun "$2" -R "$REPO" --failed >/dev/null
  sleep 15
}
# A run of the workflow on $SHA that is successful, running, or worth a re-run; else a new one.
ensure_run() {
  local wf="$1" id st=""
  id="$(last_run "$wf")"
  [ -z "$id" ] || st="$(run_state "$id")"
  if [ -n "$id" ] && [ "${st%% *}" = completed ] && [ "${st#* }" != success ]; then
    if can_rerun "$wf" "$id"; then rerun_failed "$wf" "$id"; else id=""; fi
  fi
  if [ -z "$id" ]; then
    id="$(start_run "$wf")"
    echo "    started $wf: https://github.com/$REPO/actions/runs/$id" >&2
  fi
  echo "$id"
}
# Waits for the run; it must end in success (after at most one re-run of failed jobs).
finish_run() {
  local wf="$1" id="$2" conclusion
  wait_run "$id"
  conclusion="$RUN_CONCLUSION"
  if [ "$conclusion" != success ] && can_rerun "$wf" "$id"; then
    rerun_failed "$wf" "$id"
    wait_run "$id"
    conclusion="$RUN_CONCLUSION"
  fi
  [ "$conclusion" = success ] || die "$wf run $id ended with $conclusion: https://github.com/$REPO/actions/runs/$id"
}

IN="$WORK/in"
OUT="$WORK/out"
mkdir -p "$IN" "$OUT"
have_all_assets() {
  local names
  names="$(ghr release view "$TAG" -R "$REPO" --json assets -q '.assets[] | select(.state == "uploaded") | .name' 2>/dev/null | sort)"
  [ "$names" = "$(printf '%s\n' "${ASSETS[@]}" | sort)" ]
}

if [ "$RSTATE" != published ]; then
  say "CI of ${SHA:0:12}"
  BUILD_RUN="$(ensure_run publik-build.yml)"
  MACOS_RUN="$(ensure_run macos.yml)"
  WINDOWS_RUN="$(ensure_run windows.yml)"
  echo "    publik-build.yml https://github.com/$REPO/actions/runs/$BUILD_RUN"
  echo "    macos.yml        https://github.com/$REPO/actions/runs/$MACOS_RUN"
  echo "    windows.yml      https://github.com/$REPO/actions/runs/$WINDOWS_RUN"
  finish_run publik-build.yml "$BUILD_RUN"
  ok "publik-build.yml (unsigned Mac app, Windows installer + update .sig): https://github.com/$REPO/actions/runs/$BUILD_RUN"
  set_state build_run "$BUILD_RUN"

  if [ "$RSTATE" = draft ] && have_all_assets && [ ! -f "$OUT/$DMG" ]; then
    note "the draft has every file and the work folder has none: verifying the draft"
  else
    say "Download the CI builds"
    if [ ! -f "$OUT/$EXE" ] || [ ! -f "$OUT/$EXE.sig" ]; then
      rm -rf "$IN/win"
      ghr run download "$BUILD_RUN" -R "$REPO" -n publikclip-windows-setup -D "$IN/win"
      [ -f "$IN/win/$EXE" ] && [ -f "$IN/win/$EXE.sig" ] || die "the publikclip-windows-setup artifact has no $EXE and $EXE.sig"
      mv "$IN/win/$EXE" "$IN/win/$EXE.sig" "$OUT/"
      rm -rf "$IN/win"
    fi
    [ "$(get_state ci_exe_sha)" = "$(sha "$OUT/$EXE")" ] || set_state ci_exe_sha "$(sha "$OUT/$EXE")"
    node scripts/exe-version.mjs "$OUT/$EXE" --expect "$V" >/dev/null || die "$EXE does not carry version $V"
    ok "$EXE (version $V) and its .sig, as built by CI"
    if [ ! -f "$OUT/$DMG" ] || [ ! -f "$OUT/$TGZ" ] || [ ! -f "$OUT/$TGZ.sig" ]; then
      [ -f "$IN/$ZIP" ] || ghr run download "$BUILD_RUN" -R "$REPO" -n publikclip-macos-arm64-unsigned -D "$IN"
      [ -f "$IN/$ZIP" ] || die "the publikclip-macos-arm64-unsigned artifact has no $ZIP"
      unzip -p "$IN/$ZIP" "$APP.app/Contents/Info.plist" > "$IN/Info.plist"
      [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$IN/Info.plist")" = "$V" ] || die "the CI app is not version $V"
      ok "$ZIP (version $V)"
    fi

    say "Sign, notarize and staple the Mac app; update bundle; dmg"
    # A dmg without a stapled ticket is from a run that stopped in the signing step (the dmg is
    # written before it is signed and notarized): make the files again.
    if [ -f "$OUT/$DMG" ] && [ -f "$OUT/$TGZ" ] && [ -f "$OUT/$TGZ.sig" ] && xcrun stapler validate -q "$OUT/$DMG" >/dev/null 2>&1; then
      note "already done in $OUT"
    else
      rm -f "$OUT/$DMG" "$OUT/$TGZ" "$OUT/$TGZ.sig" "$OUT/latest.json" "$OUT/latest.json.sig"
      with_update_key "$ROOT/scripts/sign-macos.sh" "$IN/$ZIP" "$OUT"
    fi
    rm -f "$IN/$ZIP" "$IN/Info.plist"

    say "Feed and checksums"
    if [ -n "${PUBLIK_RELEASE_NOTES:-}" ]; then
      [ -s "$PUBLIK_RELEASE_NOTES" ] || die "PUBLIK_RELEASE_NOTES=$PUBLIK_RELEASE_NOTES is empty or missing"
      cp "$PUBLIK_RELEASE_NOTES" "$WORK/notes.md"
    else
      # The message of the version-bump commit, without its trailers.
      git log -1 --format=%B "$SHA" -- app/src-tauri/tauri.conf.json | grep -Ev '^(Co-Authored-By|Signed-off-by):' |
        awk '{ l[NR] = $0 } END { n = NR; while (n > 0 && l[n] ~ /^[[:space:]]*$/) n--; for (i = 1; i <= n; i++) print l[i] }' > "$WORK/notes.md"
    fi
    [ -s "$WORK/notes.md" ] || die "no release notes"
    if [ ! -f "$OUT/latest.json" ]; then
      FIRST="$(sed -n '1p' "$WORK/notes.md" | sed -E "s/^#+ *//; s/^$APP $V: *//")"
      printf '%s %s: %s Details: %s\n' "$APP" "$V" "${FIRST%.}." "$RELEASE_PAGE" > "$WORK/feed-notes.txt"
      node scripts/updater-latest-json.mjs --version "$V" --base-url "$BASE_URL" \
        --platform "darwin-aarch64=$OUT/$TGZ" --platform "windows-x86_64=$OUT/$EXE" \
        --notes-file "$WORK/feed-notes.txt" --out "$OUT/latest.json"
      rm -f "$OUT/latest.json.sig"
    fi
    if [ ! -f "$OUT/latest.json.sig" ]; then
      with_update_key "$TAURI" signer sign --app-version "$V" "$OUT/latest.json" >/dev/null
    fi
    [ -s "$OUT/latest.json.sig" ] || die "latest.json was not signed"
    ok "latest.json and latest.json.sig"
    (cd "$OUT" && shasum -a 256 "$DMG" "$EXE" "$TGZ" "$TGZ.sig" "$EXE.sig" latest.json latest.json.sig > SHA256SUMS.txt)
    ok "SHA256SUMS.txt"

    say "Draft release $TAG"
    {
      cat "$WORK/notes.md"
      printf '\n## Files\n\n'
      printf -- '- `%s`: macOS 11 or later, Apple silicon. Signed with the publik Developer ID, notarized, stapled (the app and the dmg).\n' "$DMG"
      printf -- '- `%s`: Windows x64 installer (NSIS). Not code-signed. Windows SmartScreen can show "Windows protected your PC": click "More info", then "Run anyway".\n' "$EXE"
      printf -- '- `%s` and `.sig`: the macOS update bundle (the same notarized, stapled app as in the dmg) and its update signature.\n' "$TGZ"
      printf -- '- `%s.sig`: the update signature of the Windows installer.\n' "$EXE"
      printf -- '- `latest.json`: the update feed. publikhq.com serves it to installed copies (https://publikhq.com/api/updates/%s/latest.json) after publik approves this release. `latest.json.sig` is its signature.\n' "$SLUG"
      printf -- '- `SHA256SUMS.txt`: the sha256 of each file.\n\n'
      printf '## Updates\n\npublikclip 0.2.9 and later install this version themselves. Every update is signed with the publik update key, and publikclip checks the signature and the version before it installs anything. Copies on 0.2.8 or older download it one time by hand.\n\n'
      printf '## Built and tested\n\n'
      printf -- '- macOS app (unsigned, then signed and notarized on the release Mac with `scripts/publik-release.sh`) and the Windows installer: https://github.com/%s/actions/runs/%s\n' "$REPO" "$BUILD_RUN"
      printf -- '- Pipeline tests, Rust unit tests, build and launch on macOS: https://github.com/%s/actions/runs/%s\n' "$REPO" "$MACOS_RUN"
      printf -- '- Pipeline tests, build, install and launch on Windows: https://github.com/%s/actions/runs/%s\n' "$REPO" "$WINDOWS_RUN"
    } > "$WORK/release-notes.md"
    if [ "$RSTATE" = none ]; then
      (cd "$OUT" && ghr release create "$TAG" -R "$REPO" --draft --target "$SHA" --title "$APP $V" \
        --notes-file "$WORK/release-notes.md" "${ASSETS[@]}" >/dev/null)
      RSTATE=draft
      ok "draft created with ${#ASSETS[@]} files"
    else
      DIGESTS="$(ghr release view "$TAG" -R "$REPO" --json assets -q '.assets[] | "\(.name) \(.digest)"')"
      for a in "${ASSETS[@]}"; do
        if printf '%s\n' "$DIGESTS" | grep -qxF "$a sha256:$(sha "$OUT/$a")"; then continue; fi
        ghr release upload "$TAG" -R "$REPO" --clobber "$OUT/$a" >/dev/null
        ok "uploaded $a"
      done
      ghr release edit "$TAG" -R "$REPO" --notes-file "$WORK/release-notes.md" >/dev/null
      ok "draft up to date"
    fi
  fi

  say "clean-mac-check.yml on the draft"
  DMG_DIGEST="$(ghr release view "$TAG" -R "$REPO" --json assets -q ".assets[] | select(.name == \"$DMG\") | .digest")"
  CLEAN="$(get_state "clean_$DMG_DIGEST")"
  if [ -z "$CLEAN" ] || [ "$(run_field "$CLEAN" conclusion)" != success ]; then
    CLEAN="$(start_run clean-mac-check.yml -f "tag=$TAG" -f "version=$V" -f updater=true)"
    echo "    started clean-mac-check.yml: https://github.com/$REPO/actions/runs/$CLEAN"
    wait_run "$CLEAN"
    [ "$RUN_CONCLUSION" = success ] || die "clean-mac-check.yml failed on the draft: https://github.com/$REPO/actions/runs/$CLEAN"
    set_state "clean_$DMG_DIGEST" "$CLEAN"
  fi
  ok "clean-mac-check.yml: https://github.com/$REPO/actions/runs/$CLEAN"
  if [ -f "$WORK/release-notes.md" ] && ! grep -q "actions/runs/$CLEAN" "$WORK/release-notes.md"; then
    printf -- '- macOS: a clean GitHub macOS runner downloads the notarized dmg of this release with the quarantine flag, passes Gatekeeper, installs and opens the app, and checks the update files: https://github.com/%s/actions/runs/%s\n' "$REPO" "$CLEAN" >> "$WORK/release-notes.md"
    ghr release edit "$TAG" -R "$REPO" --notes-file "$WORK/release-notes.md" >/dev/null
  fi

  say "The repo's own CI on ${SHA:0:12}"
  finish_run macos.yml "$MACOS_RUN"
  ok "macos.yml: https://github.com/$REPO/actions/runs/$MACOS_RUN"
  finish_run windows.yml "$WINDOWS_RUN"
  ok "windows.yml: https://github.com/$REPO/actions/runs/$WINDOWS_RUN"
fi

# ---------------------------------------------------------------------------------------------
say "Verify $TAG as published files (downloaded back)"
VER="$WORK/verify"
rm -rf "$VER"
mkdir -p "$VER/prev" "$VER/tgz" "$VER/mnt" "$VER/pmnt"
ghr release download "$TAG" -R "$REPO" -D "$VER" --clobber >/dev/null
GOT="$(cd "$VER" && find . -maxdepth 1 -type f | sed 's|^\./||' | sort)"
if [ "$GOT" = "$(printf '%s\n' "${ASSETS[@]}" | sort)" ]; then ok "the release has exactly: ${ASSETS[*]}"
else bad "the release has exactly: ${ASSETS[*]}"; echo "        it has: $(echo "$GOT" | tr '\n' ' ')"; fi
expect "SHA256SUMS.txt matches every file" sh -c "cd '$VER' && shasum -a 256 -c SHA256SUMS.txt >/dev/null"
expect "SHA256SUMS.txt lists the dmg, the installer and the update bundle" sh -c \
  "grep -q '  $DMG\$' '$VER/SHA256SUMS.txt' && grep -q '  $EXE\$' '$VER/SHA256SUMS.txt' && grep -q '  $TGZ\$' '$VER/SHA256SUMS.txt'"
# The previous release's app: from its update bundle, else from its dmg (0.2.8 had no updater).
PREV_V="${PREV_TAG#v}"
PREV_APP=""
if ghr release download "$PREV_TAG" -R "$REPO" -p "${APP}_${PREV_V}_aarch64.app.tar.gz" -D "$VER/prev" --clobber >/dev/null 2>&1; then
  tar -xzf "$VER/prev/${APP}_${PREV_V}_aarch64.app.tar.gz" -C "$VER/prev"
  PREV_APP="$VER/prev/$APP.app"
elif ghr release download "$PREV_TAG" -R "$REPO" -p "${APP}_${PREV_V}_aarch64.dmg" -D "$VER/prev" --clobber >/dev/null 2>&1; then
  hdiutil attach -nobrowse -noautoopen -readonly -mountpoint "$VER/pmnt" "$VER/prev/${APP}_${PREV_V}_aarch64.dmg" >/dev/null
  ditto "$VER/pmnt/$APP.app" "$VER/prev/$APP.app"
  hdiutil detach "$VER/pmnt" -quiet
  PREV_APP="$VER/prev/$APP.app"
else
  note "$PREV_TAG has no macOS app to compare with"
fi
git show "$SHA:app/src-tauri/tauri.conf.json" > "$VER/tauri.conf.json"
git show "$PREV_TAG:app/src-tauri/tauri.conf.json" > "$VER/tauri.conf.prev.json"
pubkey_of() { node -p 'require(process.argv[1]).plugins?.updater?.pubkey ?? ""' "$1"; }
if [ -n "$(pubkey_of "$VER/tauri.conf.prev.json")" ]; then
  expect "the update public key is the one $PREV_TAG ships (installed copies trust it)" \
    test "$(pubkey_of "$VER/tauri.conf.json")" = "$(pubkey_of "$VER/tauri.conf.prev.json")"
else
  note "$PREV_TAG has no updater: no public key to compare"
fi
expect "the feed endpoint compiled into $V is https://publikhq.com/api/updates/$SLUG/latest.json" \
  test "$(node -p 'require(process.argv[1]).plugins.updater.endpoints.join(" ")' "$VER/tauri.conf.json")" = "https://publikhq.com/api/updates/$SLUG/latest.json"
if node scripts/updater-latest-json.mjs --check "$VER/latest.json" --config "$VER/tauri.conf.json" --version "$V" --base-url "$BASE_URL" \
  --platform "darwin-aarch64=$VER/$TGZ" --platform "windows-x86_64=$VER/$EXE" 2>&1 | sed 's/^/        /'; then
  ok "latest.json: version $V, absolute URLs, every signature (and latest.json.sig) verifies with the app's public key"
else
  bad "latest.json and its signatures"
fi
expect "latest.json.sig is there" test -s "$VER/latest.json.sig"
expect "Windows installer version resource is $V ($(node scripts/exe-version.mjs "$VER/$EXE" 2>&1))" sh -c 'node scripts/exe-version.mjs "$1" --expect "$2" >/dev/null' _ "$VER/$EXE" "$V"
if [ -n "$(get_state ci_exe_sha)" ]; then
  expect "Windows installer is the CI build, byte for byte" test "$(sha "$VER/$EXE")" = "$(get_state ci_exe_sha)"
fi

# The dmg and the app in it.
SPCTL_DMG="$(spctl -a -vv -t open --context context:primary-signature "$VER/$DMG" 2>&1 || true)"
expect "dmg: Gatekeeper accepts it (Notarized Developer ID)" sh -c 'printf "%s\n" "$1" | grep -q ": accepted" && printf "%s\n" "$1" | grep -q "source=Notarized Developer ID"' _ "$SPCTL_DMG"
expect "dmg: notarization ticket stapled" stapled "$VER/$DMG"
expect "dmg: signed by $IDENTITY" sh -c 'codesign -dvv "$1" 2>&1 | grep -qxF "Authority=$2"' _ "$VER/$DMG" "$IDENTITY"
hdiutil attach -nobrowse -noautoopen -readonly -mountpoint "$VER/mnt" "$VER/$DMG" >/dev/null
trap 'hdiutil detach "$VER/mnt" -quiet 2>/dev/null || true; hdiutil detach "$VER/pmnt" -quiet 2>/dev/null || true' EXIT
expect "dmg: holds $APP.app and an Applications link" sh -c 'test -d "$1/$2.app" && test "$(readlink "$1/Applications")" = /Applications' _ "$VER/mnt" "$APP"
COPYFILE_DISABLE=1 tar -xzf "$VER/$TGZ" -C "$VER/tgz"
expect "update bundle: one top folder $APP.app, no AppleDouble (._) files" sh -c \
  '! tar -tzf "$1" | grep -q "/\._" && test "$(tar -tzf "$1" | cut -d/ -f1 | sort -u)" = "$2.app"' _ "$VER/$TGZ" "$APP"
WANT_ENTS="$(git show "$SHA:app/src-tauri/entitlements.plist" | plist_json)"
PREV_DR=""
PREV_ENTS=""
PREV_BIN=/nonexistent
if [ -n "$PREV_APP" ]; then
  PREV_DR="$(dr "$PREV_APP")"
  PREV_ENTS="$(ents "$PREV_APP")"
  PREV_BIN="$PREV_APP/Contents/MacOS/$(plist "$PREV_APP" CFBundleExecutable)"
fi
for where in "dmg:$VER/mnt/$APP.app" "update bundle:$VER/tgz/$APP.app"; do
  w="${where%%:*}"
  a="${where#*:}"
  info="$(codesign -dvv "$a" 2>&1)"
  expect "$w app: Info.plist version $V (CFBundleShortVersionString and CFBundleVersion)" \
    test "$(plist "$a" CFBundleShortVersionString)/$(plist "$a" CFBundleVersion)" = "$V/$V"
  expect "$w app: bundle id $BUNDLE_ID" test "$(plist "$a" CFBundleIdentifier)" = "$BUNDLE_ID"
  expect "$w app: codesign --verify --strict --deep" codesign --verify --strict --deep "$a"
  expect "$w app: $IDENTITY, team $TEAM, hardened runtime" sh -c 'printf "%s\n" "$1" | grep -qxF "Authority=$2" && printf "%s\n" "$1" | grep -qxF "TeamIdentifier=$3" && printf "%s\n" "$1" | grep -q "flags=.*runtime"' _ "$info" "$IDENTITY" "$TEAM"
  UV="$a/Contents/Resources/resources/bin/uv"
  expect "$w app: bundled uv signed by $IDENTITY with the hardened runtime" sh -c 'i="$(codesign -dvv "$1" 2>&1)"; printf "%s\n" "$i" | grep -qxF "Authority=$2" && printf "%s\n" "$i" | grep -q "flags=.*runtime"' _ "$UV" "$IDENTITY"
  expect "$w app: entitlements are app/src-tauri/entitlements.plist (app and uv)" \
    test "$(ents "$a")" = "$WANT_ENTS" -a "$(ents "$UV")" = "$WANT_ENTS" -a -n "$WANT_ENTS"
  if [ -n "$PREV_ENTS" ]; then expect "$w app: entitlements as in $PREV_TAG" test "$(ents "$a")" = "$PREV_ENTS"; fi
  expect "$w app: notarization ticket stapled" stapled "$a"
  SPCTL_APP="$(spctl -a -vv -t exec "$a" 2>&1 || true)"
  expect "$w app: Gatekeeper accepts it (Notarized Developer ID)" sh -c 'printf "%s\n" "$1" | grep -q ": accepted" && printf "%s\n" "$1" | grep -q "source=Notarized Developer ID"' _ "$SPCTL_APP"
  expect "$w app: designated requirement as in $PREV_TAG (TCC grants carry over)" test -n "$PREV_DR" -a "$(dr "$a")" = "$PREV_DR"
  BIN="$a/Contents/MacOS/$(plist "$a" CFBundleExecutable)"
  expect "$w app: arm64" test "$(lipo -archs "$BIN")" = arm64
  expect "$w app: the publik app token of app/src-tauri/publik-app-token.txt is compiled in, as in $PREV_TAG" sh -c \
    'test "$(LC_ALL=C grep -c -a -F "$1" "$2" 2>/dev/null)" -ge 1 && test "$(LC_ALL=C grep -c -a -F "$1" "$3" 2>/dev/null)" -ge 1' _ "$TOKEN_FILE_AT_SHA" "$BIN" "$PREV_BIN" 2>/dev/null
  expect "$w app: the feed URL is compiled in" sh -c 'LC_ALL=C grep -q -a -F "https://publikhq.com/api/updates/$1/latest.json" "$2"' _ "$SLUG" "$BIN"
done
CD_DMG="$(codesign -dvvv "$VER/mnt/$APP.app" 2>&1 | sed -n 's/^CDHash=//p')"
CD_TGZ="$(codesign -dvvv "$VER/tgz/$APP.app" 2>&1 | sed -n 's/^CDHash=//p')"
expect "the update bundle is the app of the dmg (CDHash $CD_DMG)" test -n "$CD_DMG" -a "$CD_DMG" = "$CD_TGZ"
echo "  designated requirement: $(dr "$VER/tgz/$APP.app")"
hdiutil detach "$VER/mnt" -quiet
trap - EXIT
if [ "$RSTATE" = draft ]; then
  expect "the draft targets ${SHA:0:12}" test "$(ghr api "repos/$REPO/releases" -q ".[] | select(.tag_name == \"$TAG\") | .target_commitish")" = "$SHA"
fi
if [ "$RSTATE" = draft ]; then [ "$FAILS" -eq 0 ] || die "$FAILS check(s) failed. $TAG stays a draft: fix the cause and run the command again."; fi
[ "$FAILS" -eq 0 ] || die "$FAILS check(s) failed on the published $TAG."
ok "every check passed"

# ---------------------------------------------------------------------------------------------
if [ "$RSTATE" = draft ]; then
  if [ "${PUBLIK_RELEASE_NO_PUBLISH:-0}" = 1 ]; then
    say "Not published (PUBLIK_RELEASE_NO_PUBLISH=1): https://github.com/$REPO/releases (draft $TAG)"
    exit 0
  fi
  say "Publish $TAG"
  ghr release edit "$TAG" -R "$REPO" --draft=false --latest >/dev/null
  git fetch -q origin tag "$TAG"
  expect "tag $TAG points at ${SHA:0:12}" test "$(git rev-parse "$TAG^{commit}")" = "$SHA"
  expect "the release is public" test "$(ghr release view "$TAG" -R "$REPO" --json isDraft -q .isDraft)" = false
  for url in $(node -p 'Object.values(require(process.argv[1]).platforms).map((p) => p.url).join("\n")' "$VER/latest.json") "${BASE_URL}latest.json" "${BASE_URL}latest.json.sig" "${BASE_URL}$DMG"; do
    code="$(curl -s -o /dev/null -L -r 0-0 -w '%{http_code}' "$url")"
    expect "$url answers ($code)" test "$code" = 200 -o "$code" = 206
  done
  [ "$FAILS" -eq 0 ] || die "$FAILS check(s) failed after publishing $TAG: look at $RELEASE_PAGE"
  rm -rf "$WORK"
  say "Published: $RELEASE_PAGE"
  echo "publik takes the release in at its next daily intake (10:00 UTC) and then serves its"
  echo "latest.json at https://publikhq.com/api/updates/$SLUG/latest.json."
else
  rm -rf "$VER"
  say "$TAG is published and passes every check: $RELEASE_PAGE"
fi
