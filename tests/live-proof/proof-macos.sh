#!/usr/bin/env bash
# Live update proof on a macOS GitHub runner (.github/workflows/live-update-proof.yml).
# Never run it on a desk Mac: it writes TCC grants, installs into /Applications and starts
# publikclip. Built on the GSpot proof (Blueturboguy07/gspot, tests/live-proof/proof-macos.sh).
#
# Usage: tests/live-proof/proof-macos.sh <from_tag> <expect_version> <out dir>
#
# 1. Downloads the dmg of release <from_tag> from GitHub (the asset publik's listing links), marks
#    it quarantined as a browser does, checks that Gatekeeper accepts it, and installs the app the
#    way a user does (Finder copies it from the mounted dmg to /Applications). A copy made by a
#    script runs translocated, so after the Gatekeeper check the quarantine flag is cleared and
#    the app opens from /Applications.
# 2. Records the app's designated requirement and writes real TCC grants for its bundle id:
#    camera and microphone in the user database, screen capture and accessibility in the system
#    database, each with the csreq made from that requirement.
# 3. Starts the app with `open` (no test hook, no feed override: the release asks the live feed
#    https://publikhq.com/api/updates/publikclip/latest.json). Sign-in uses a local stand-in for
#    publik's API (fake-publik.py, "unclaimed"), so nobody signs in and the app stays on its
#    "Sign in with publik" window: no main window, no job, no pipeline, no camera, microphone or
#    screen capture. publik's database is not touched. tcpdump records the TLS ClientHello the
#    app sends to publikhq.com (the feed request; publikclip has no log of its own for it).
# 4. <expect_version> = the version of <from_tag>: the app must find no update and stay as it is;
#    "Check for Updates…" in the app menu must answer "publikclip <v> is the latest version."
#    Higher: the app must download and install it by itself and ask; the proof answers "Restart"
#    (then the app must run the new version and say so through the menu), installs <from_tag>
#    again and answers "Later" (the app keeps running; the menu asks again; after a quit and
#    reopen it runs the new version).
# 5. After it: the designated requirement is unchanged, Gatekeeper accepts the app, and every TCC
#    row is still there with its csreq satisfied by the installed app (the check tccd makes).
# shellcheck disable=SC2024 # sudo runs the reader; its output goes to the runner user's files
set -uo pipefail
FROM_TAG="${1:?from_tag}"
EXPECT="${2:?expect_version}"
OUT="${3:?out dir}"
FROM="${FROM_TAG#v}"
EXPECT="${EXPECT#v}"
if ! [[ "$FROM" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$EXPECT" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "from_tag must be vX.Y.Z and expect_version X.Y.Z" >&2
  exit 2
fi
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d)"
NAME=publikclip
APP="/Applications/$NAME.app"
BUNDLE_ID=com.publikhq.publikclip
EXE=publikclip-app # CFBundleExecutable; checked after the install
FEED=https://publikhq.com/api/updates/publikclip/latest.json
DMG="$WORK/dl/${NAME}_${FROM}_aarch64.dmg"
MNT=/Volumes/publikclip-proof
PORT=47334
MODE=update
[ "$FROM" = "$EXPECT" ] && MODE=no-update
USER_DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
SYS_DB="/Library/Application Support/com.apple.TCC/TCC.db"
SERVICES=(kTCCServiceCamera kTCCServiceMicrophone kTCCServiceScreenCapture kTCCServiceAccessibility)
MENU_ITEM="Check for Updates"

PASS=0
FAILS=0
RESULTS="$OUT/results.md"
printf '| Check | Result | Detail |\n|---|---|---|\n' > "$RESULTS"
row() { printf '| %s | %s | %s |\n' "$1" "$2" "$(printf '%s' "${3:-}" | tr '|\n' '/ ' | cut -c1-300)" >> "$RESULTS"; }
pass() { PASS=$((PASS + 1)); echo "PASS  $1 ${2:+($2)}"; row "$1" PASS "${2:-}"; }
fail() { FAILS=$((FAILS + 1)); echo "FAIL  $1 ${2:+($2)}"; row "$1" "**FAIL**" "${2:-}"; }
note() { echo "NOTE  $1 ${2:+($2)}"; row "$1" note "${2:-}"; }
check() { local name="$1" detail="$2"; shift 2; if "$@"; then pass "$name" "$detail"; else fail "$name" "$detail"; fi; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
SHOTS=0
shot() { SHOTS=$((SHOTS + 1)); screencapture -x "$OUT/$(printf '%02d' "$SHOTS")-$1.png" 2>/dev/null || true; }
plist() { /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null; }
cdhash() { codesign -dvvv "$1" 2>&1 | sed -n 's/^CDHash=//p'; }
dr() { codesign -d -r- "$1" 2>&1 | sed -n 's/^designated => //p'; }
app_pid() { pgrep -x "$EXE" | head -1; }
app_running() { pgrep -x "$EXE" >/dev/null; }
gatekeeper_ok() { local a; a="$(spctl -a -vv -t "$1" ${2:+--context "$2"} "$3" 2>&1)"; echo "$a" >> "$OUT/spctl.txt"; echo "$a" | grep -q ": accepted" && echo "$a" | grep -q "source=Notarized Developer ID"; }
# Runs a command for at most $1 seconds (124 on a timeout).
limit() {
  local t="$1" p i
  shift
  "$@" & p=$!
  for ((i = 0; i < t; i++)); do kill -0 "$p" 2>/dev/null || { wait "$p"; return $?; }; sleep 1; done
  kill "$p" 2>/dev/null
  return 124
}
# Waits up to $1 seconds for a command to succeed.
wait_for() {
  local t="$1" i
  shift
  for ((i = 0; i < t; i++)); do "$@" && return 0; sleep 1; done
  return 1
}
# System Events through ui.applescript: ui <count|texts|press|menu> <pid|name> <process> [arg].
ui() { limit 40 osascript "$HERE/ui.applescript" "$@" 2>> "$OUT/osascript.log"; }
windows_of() { ui count name "$1" | tr -d '[:space:]'; }
has_window() { [ "$(windows_of "$1")" -gt 0 ] 2>/dev/null; }
texts_of() { ui texts name "$1"; }
press() { ui press name "$1" "$2" >/dev/null; }
log_has() { grep -qF "$2" "$1" 2>/dev/null; }
# The updater's answers and questions (app/src-tauri/src/updates.rs). The dialog plugin shows a
# message without a parent window with CFUserNotificationDisplayAlert, so macOS draws it in
# UserNotificationCenter; a window of the app itself is read too, in case it is drawn there.
dialog_texts() { { texts_of UserNotificationCenter; echo; [ -n "${PID:-}" ] && ui texts pid "$PID"; } 2>/dev/null | tr '\n' ' '; }
dialog_says() { dialog_texts | grep -qF "$1"; }
press_in_dialog() { press UserNotificationCenter "$1" || { [ -n "${PID:-}" ] && ui press pid "$PID" "$1" >/dev/null; }; }
no_question() { local n; n="$(windows_of UserNotificationCenter)"; [ "${n:-0}" = 0 ]; }
# The menu with real input: a click on the app's name in the menu bar, the item's first letters, Return.
# For the state after "Later": the app's file on disk is the new version while the old one runs, and
# macOS then refuses the accessibility API for that process ("not allowed assistive access").
real_menu_click() {
  [ -x "$WORK/click" ] || swiftc -O -o "$WORK/click" "$HERE/click.swift" 2>> "$OUT/osascript.log" || return 1
  open "$APP"
  sleep 3
  "$WORK/click" 70 12 left
  sleep 2
  shot real-menu-open
  "$WORK/click" type Check
  sleep 1
  "$WORK/click" key 36
  echo "real mouse click on the app's name in the menu bar, then the keys C-h-e-c-k and Return"
}
# "Check for Updates…" in the app menu (menu bar item 2), then the answer. Sets MENU_HOW, ANSWER.
menu_check() { # expected text
  local i
  MENU_HOW=""
  ANSWER=""
  for i in 1 2 3; do
    MENU_HOW="$(ui menu pid "$PID" "$MENU_ITEM")" && break
    MENU_HOW=""
    sleep 5
  done
  [ -n "$MENU_HOW" ] || return 1
  wait_for 60 dialog_says "$1"
  ANSWER="$(dialog_texts)"
  printf '%s' "$ANSWER" | grep -qF "$1"
}

log "publikclip live update proof: install $FROM_TAG, expect $EXPECT ($MODE) on $(sw_vers -productVersion) $(uname -m)"
echo "from=$FROM_TAG expect=$EXPECT mode=$MODE macos=$(sw_vers -productVersion)" > "$OUT/run.txt"
curl -fsS "$FEED" -o "$OUT/feed-at-start.json"
FEED_V="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$OUT/feed-at-start.json" 2>/dev/null)"
check "the live feed announces $EXPECT" "$FEED version $FEED_V" test "$FEED_V" = "$EXPECT"

# ---- 1. Download, Gatekeeper, install ---------------------------------------------------------
mkdir -p "$WORK/dl"
URL="https://github.com/$REPO/releases/download/$FROM_TAG/$(basename "$DMG")"
log "download $URL"
curl -fsSL "$URL" -o "$DMG"
curl -fsSL "https://github.com/$REPO/releases/download/$FROM_TAG/SHA256SUMS.txt" -o "$WORK/dl/SHA256SUMS.txt"
check "the dmg is the release's (SHA256SUMS.txt)" "$(shasum -a 256 "$DMG" | cut -c1-16)…" \
  grep -q "^$(shasum -a 256 "$DMG" | cut -d' ' -f1)  $(basename "$DMG")\$" "$WORK/dl/SHA256SUMS.txt"
# The quarantine flag a browser sets on a download.
xattr -w com.apple.quarantine "0083;$(printf '%x' "$(date +%s)");Safari;$(uuidgen)" "$DMG"
check "quarantine flag on the downloaded dmg" "$(xattr -p com.apple.quarantine "$DMG")" xattr -p com.apple.quarantine "$DMG"
check "Gatekeeper accepts the quarantined dmg (Notarized Developer ID)" "spctl -t open" gatekeeper_ok open context:primary-signature "$DMG"

mount_dmg() { hdiutil attach -noautoopen -readonly -mountpoint "$MNT" "$DMG" >/dev/null; }
install_from_dmg() {
  pkill -x "$EXE" 2>/dev/null; sleep 1
  rm -rf "$APP"
  mount_dmg || return 1
  # What a user does: drag the app from the dmg window to Applications, so Finder copies it.
  if limit 90 osascript -e "tell application \"Finder\" to duplicate (POSIX file \"$MNT/$NAME.app\" as alias) to (POSIX file \"/Applications\" as alias) with replacing" > /dev/null 2>> "$OUT/osascript.log" && [ -d "$APP" ]; then
    INSTALLED_BY="Finder copy from the dmg"
  else
    ditto "$MNT/$NAME.app" "$APP"
    INSTALLED_BY="ditto from the dmg (Finder did not copy it)"
  fi
  hdiutil detach "$MNT" -quiet || hdiutil detach "$MNT" -force -quiet
  [ -d "$APP" ]
}
# Gatekeeper on the installed copy while it still carries the download's quarantine flag; then
# the flag goes, so the app opens from /Applications and not from a translocated copy.
gatekeeper_then_clear() { # label
  check "$1the installed copy carries the download's quarantine flag" "$(xattr -p com.apple.quarantine "$APP" 2>/dev/null)" xattr -p com.apple.quarantine "$APP"
  check "$1Gatekeeper accepts the installed, quarantined app (Notarized Developer ID)" "spctl -t exec" gatekeeper_ok exec "" "$APP"
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null
  note "$1quarantine flag cleared after the Gatekeeper check" "a copy made by a script, not by a drag in a Finder window, would run translocated (read-only, from a random path)"
}
check "publikclip $FROM installed in /Applications" "" install_from_dmg
note "how it was installed" "$INSTALLED_BY"
check "the installed app is $FROM" "$(plist "$APP" CFBundleShortVersionString)" test "$(plist "$APP" CFBundleShortVersionString)" = "$FROM"
check "its executable is $EXE" "$(plist "$APP" CFBundleExecutable)" test "$(plist "$APP" CFBundleExecutable)" = "$EXE"
check "the feed URL compiled into the app is the live publikhq.com feed" "$FEED" \
  sh -c 'LC_ALL=C grep -q -a -F "$2" "$1"' _ "$APP/Contents/MacOS/$EXE" "$FEED"
gatekeeper_then_clear ""
FROM_CDHASH="$(cdhash "$APP")"
codesign -dvvv "$APP" > "$OUT/codesign-before.txt" 2>&1

# ---- 2. Designated requirement and TCC grants -------------------------------------------------
DR_BEFORE="$(dr "$APP")"
echo "$DR_BEFORE" > "$OUT/designated-requirement-before.txt"
log "designated requirement: $DR_BEFORE"
printf '%s' "$DR_BEFORE" | csreq -r- -b "$WORK/csreq.bin"
check "csreq blob made from the designated requirement" "$(wc -c < "$WORK/csreq.bin" | tr -d ' ') bytes" test -s "$WORK/csreq.bin"
python3 "$HERE/tcc.py" schema "$USER_DB" > "$OUT/tcc-schema-user.sql" 2>&1
sudo python3 "$HERE/tcc.py" schema "$SYS_DB" > "$OUT/tcc-schema-system.sql" 2>&1
note "TCC access table of this macOS version (tcc-schema-*.sql)" "$(tr -s ' \n' ' ' < "$OUT/tcc-schema-system.sql" | cut -c1-280)"
grant() { # db service auth_reason
  if [ "$1" = "$SYS_DB" ]; then sudo python3 "$HERE/tcc.py" grant "$1" "$2" "$BUNDLE_ID" "$WORK/csreq.bin" "$3"
  else python3 "$HERE/tcc.py" grant "$1" "$2" "$BUNDLE_ID" "$WORK/csreq.bin" "$3"; fi
}
db_of() { case "$1" in kTCCServiceCamera | kTCCServiceMicrophone) echo "$USER_DB" ;; *) echo "$SYS_DB" ;; esac; }
reason_of() { case "$1" in kTCCServiceCamera | kTCCServiceMicrophone) echo 2 ;; *) echo 3 ;; esac; }
db_label() { if [ "$(db_of "$1")" = "$SYS_DB" ]; then echo "system TCC.db"; else echo "user TCC.db"; fi; }
for s in "${SERVICES[@]}"; do
  check "TCC grant written: $s ($(db_label "$s"))" "auth_value 2, csreq of the designated requirement" grant "$(db_of "$s")" "$s" "$(reason_of "$s")"
done
tcc_rows() {
  python3 "$HERE/tcc.py" rows "$USER_DB" "$BUNDLE_ID"
  sudo python3 "$HERE/tcc.py" rows "$SYS_DB" "$BUNDLE_ID"
}
tcc_rows > "$OUT/tcc-rows-before.jsonl" 2>&1
# A row of the app for the service, allowed, with a csreq.
row_ok() { python3 -c 'import json,sys; sys.exit(0 if any(r["service"] == sys.argv[2] and r["auth_value"] == 2 and r.get("csreq") for r in map(json.loads, filter(str.strip, open(sys.argv[1])))) else 1)' "$2" "$1"; }
for s in "${SERVICES[@]}"; do
  check "TCC row present before the update: $s" "allowed, csreq set" row_ok "$s" "$OUT/tcc-rows-before.jsonl"
done
# The check tccd makes when the app uses a service: the app satisfies the stored csreq.
csreq_ok() { # service -> the installed app satisfies the csreq stored in that row
  local f="$WORK/stored-$1.bin"
  if [ "$(db_of "$1")" = "$SYS_DB" ]; then sudo python3 "$HERE/tcc.py" csreq "$SYS_DB" "$1" "$BUNDLE_ID" "$f" && sudo chown "$(id -u)" "$f"
  else python3 "$HERE/tcc.py" csreq "$USER_DB" "$1" "$BUNDLE_ID" "$f"; fi || return 1
  echo "$1: $(csreq -r "$f" -t 2>&1)" >> "$OUT/tcc-csreq-text.txt"
  codesign --verify -R "$f" "$APP" 2>> "$OUT/tcc-csreq-verify.txt"
}
for s in "${SERVICES[@]}"; do check "[before] $FROM satisfies the stored csreq of $s" "" csreq_ok "$s"; done

# ---- 3. Start the app the normal way ----------------------------------------------------------
mkdir -p "$HOME/Library/Application Support/publik/apps"
python3 "$HERE/fake-publik.py" "$PORT" unclaimed > "$OUT/fake-publik.log" 2>&1 &
FAKE_PID=$!
# The same environment for every launch through LaunchServices, also the one Tauri makes when it
# restarts into the update: no usage counts, the stand-in for sign-in. Nothing about the feed.
ENV_VARS=(DO_NOT_TRACK=1 "PUBLIK_ORIGIN=http://127.0.0.1:$PORT")
for kv in "${ENV_VARS[@]}"; do launchctl setenv "${kv%%=*}" "${kv#*=}"; done
trap 'kill $FAKE_PID 2>/dev/null; sudo pkill -x tcpdump 2>/dev/null; pkill -x "$EXE" 2>/dev/null; for kv in "${ENV_VARS[@]}"; do launchctl unsetenv "${kv%%=*}"; done; true' EXIT
wait_for 20 curl -fs "http://127.0.0.1:$PORT/api/v1/wallet" -o /dev/null
note "sign-in" "PUBLIK_ORIGIN is a local stand-in that answers \"unclaimed\": the app stays on Sign in with publik (no main window, no job); it logs the version each app process sends with its install request"
CREDENTIAL="$HOME/Library/Application Support/publik/apps/publikclip.json"
# The next process that starts sends POST /installs with its compiled-in version: forget the stand-in key.
forget_key() { rm -f "$CREDENTIAL"; }
told_version() { grep -q "install request: app_version=$1 " "$OUT/fake-publik.log"; }
# The inode of the executable a process runs, and of the one on disk now.
running_inode() { lsof -a -p "$1" -d txt -Fin 2>/dev/null | awk -v exe="/Contents/MacOS/$EXE" '/^i/ { i = substr($0, 2) } /^n/ && substr($0, length($0) - length(exe) + 1) == exe { print i; exit }'; }
disk_inode() { stat -f %i "$APP/Contents/MacOS/$EXE"; }
runs_disk_file() { local i; i="$(running_inode "$1")"; [ -n "$i" ] && [ "$i" = "$(disk_inode)" ]; }
runs_old_file() { local i; i="$(running_inode "$1")"; [ -n "$i" ] && [ "$i" != "$(disk_inode)" ]; }
# Quits publikclip (SIGTERM; an Apple event to it would need an Automation consent on the runner).
quit_app() { pkill -x "$EXE"; wait_for 15 sh -c '! pgrep -x "$1" >/dev/null' _ "$EXE" || pkill -9 -x "$EXE"; sleep 1; }
start_app() { # log file
  local e=()
  for kv in "${ENV_VARS[@]}"; do e+=(--env "$kv"); done
  open "${e[@]}" --stdout "$1.stdout" --stderr "$1" "$APP"
}
launch() { # log file, label
  : > "$1"
  start_app "$1" &
  wait_for 60 app_running
  PID="$(app_pid)"
  [ -n "$PID" ]
}
running_from_applications() { ps -o command= -p "$PID" | grep -q "^$APP/Contents/MacOS/$EXE"; }

# The feed request: tcpdump records every TLS ClientHello that leaves this runner from now on.
# publikclip names the server in it (SNI), so "publikhq.com" there is the app's request: this job
# does not contact publikhq.com again, and nothing else on the runner does. With the pktap
# interface macOS also names the process that sent each packet.
TLS="$OUT/tls-client-hello.txt"
IFACE="$(route -n get default 2>/dev/null | awk '/interface:/ { print $2 }')"
IFACE="${IFACE:-en0}"
HELLO='tcp dst port 443 and (tcp[((tcp[12:1] & 0xf0) >> 2):1] = 0x16)'
CAPTURE=pktap
sudo tcpdump -i "pktap,$IFACE" -k NP -n -l -A -s 0 "$HELLO" > "$TLS" 2> "$OUT/tcpdump.err" &
sleep 3
if ! grep -q "listening on" "$OUT/tcpdump.err"; then
  sudo pkill -x tcpdump 2>/dev/null
  CAPTURE=plain
  sudo tcpdump -i "$IFACE" -n -l -A -s 0 "$HELLO" > "$TLS" 2>> "$OUT/tcpdump.err" &
  sleep 3
fi
check "tcpdump records the TLS ClientHellos that leave the runner ($CAPTURE, $IFACE)" "$(head -1 "$OUT/tcpdump.err")" grep -q "listening on" "$OUT/tcpdump.err"
# The header lines (time, process with pktap, addresses) of the ClientHellos that name publikhq.com.
feed_hellos() { awk '/^[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\./ { h = $0 } /publikhq\.com/ && h != "" { print h; h = "" }' "$TLS"; }

check "publikclip starts with open" "" launch "$OUT/app-1.log" first
log "pid $PID: $(ps -o command= -p "$PID")"
check "it runs from /Applications (not translocated)" "$(ps -o command= -p "$PID")" running_from_applications
shot started

# ---- 4. The live check ------------------------------------------------------------------------
LOG="$OUT/app-1.log"
if [ "$MODE" = no-update ]; then
  check "the app reports \"$FROM is the latest version\" (its updater read the live feed)" "" wait_for 120 log_has "$LOG" "publikclip updates: $FROM is the latest version"
  check "the request went to publikhq.com (TLS ClientHello with SNI publikhq.com)" "$(feed_hellos | head -1)" sh -c 'test -n "$1"' _ "$(feed_hellos | head -1)"
  if [ "$CAPTURE" = pktap ]; then
    check "the process that sent it is publikclip ($EXE, pid $PID)" "$(feed_hellos | head -1)" sh -c 'printf "%s\n" "$1" | grep -q "$2"' _ "$(feed_hellos)" "$EXE"
  fi
  sleep 20
  shot no-update
  check "no update was installed or offered" "" sh -c '! grep -q "updates: publikclip .* is ready" "$1"' _ "$LOG"
  check "no question on screen (no UserNotificationCenter window)" "" no_question
  check "the app keeps running (same process)" "pid $PID" kill -0 "$PID"
  check "the running app told its version: $FROM (install request)" "" told_version "$FROM"
  check "the running process executes the file on disk (inode)" "$(running_inode "$PID") = $(disk_inode)" runs_disk_file "$PID"
  check "the app on disk is still $FROM" "$(plist "$APP" CFBundleShortVersionString)" test "$(plist "$APP" CFBundleShortVersionString)" = "$FROM"
  check "the app on disk is the same code (CDHash)" "$(cdhash "$APP")" test "$(cdhash "$APP")" = "$FROM_CDHASH"
  # The menu item, through the UI: the app's own answer.
  BEFORE_MENU="$(feed_hellos | wc -l | tr -d ' ')"
  menu_check "publikclip $FROM is the latest version."
  RC=$?
  check "menu: \"Check for Updates…\" clicked in the app menu" "$MENU_HOW" test -n "$MENU_HOW"
  shot menu-answer
  check "menu: the answer is \"publikclip $FROM is the latest version.\"" "$ANSWER" test "$RC" = 0
  sleep 2
  AFTER_MENU="$(feed_hellos | wc -l | tr -d ' ')"
  check "menu: the menu check asked publikhq.com again (one more ClientHello)" "$AFTER_MENU ClientHellos, $BEFORE_MENU before the click" test "$AFTER_MENU" -gt "$BEFORE_MENU"
  press_in_dialog OK
  sleep 3
  check "menu: the answer closes with OK; no question is left" "" no_question
  check "the app keeps running after the menu check" "pid $PID" kill -0 "$PID"
  note "variants Restart and Later" "not applicable: the feed offers no newer version"
else
  # Variant A: "Restart".
  check "[Restart] the updater downloads, verifies and installs $EXPECT by itself" "" wait_for 300 log_has "$LOG" "publikclip updates: publikclip $EXPECT is ready"
  check "[Restart] the request went to publikhq.com (TLS ClientHello with SNI publikhq.com)" "$(feed_hellos | head -1)" sh -c 'test -n "$1"' _ "$(feed_hellos | head -1)"
  check "[Restart] the app on disk is now $EXPECT" "$(plist "$APP" CFBundleShortVersionString)" test "$(plist "$APP" CFBundleShortVersionString)" = "$EXPECT"
  check "[Restart] the question is on screen" "" wait_for 30 has_window UserNotificationCenter
  shot restart-question
  Q="$(dialog_texts)"
  check "[Restart] it says \"publikclip $EXPECT is installed. Restart publikclip to use it.\"" "$Q" sh -c 'printf "%s" "$1" | grep -qF "publikclip $2 is installed. Restart publikclip to use it."' _ "$Q" "$EXPECT"
  OLD_PID="$PID"
  check "[Restart] until the answer, the old code keeps running (inode of the running file is not the new one)" "$(running_inode "$OLD_PID") / $(disk_inode)" runs_old_file "$OLD_PID"
  forget_key
  if press_in_dialog Restart; then
    note "[Restart] answered \"Restart\" in the question" "System Events click; Tauri starts the app again"
    wait_for 30 sh -c '! kill -0 "$1" 2>/dev/null' _ "$OLD_PID"
    wait_for 30 sh -c 'p="$(pgrep -x "$1" | head -1)"; [ -n "$p" ] && [ "$p" != "$2" ]' _ "$EXE" "$OLD_PID"
  else
    note "[Restart] the question could not be pressed on this runner" "quit and reopen instead"
    quit_app
    launch "$LOG.reopen" reopen-after-restart-question
  fi
  PID="$(app_pid)"
  check "[Restart] publikclip runs again as a new process" "pid $OLD_PID -> ${PID:-none}" test -n "$PID" -a "$PID" != "$OLD_PID"
  check "[Restart] the new process is the installed app" "$(ps -o command= -p "${PID:-0}")" running_from_applications
  check "[Restart] the new process executes the $EXPECT file on disk (inode)" "" runs_disk_file "${PID:-0}"
  check "[Restart] the running app told its version: $EXPECT (install request)" "" wait_for 60 told_version "$EXPECT"
  shot restarted
  sleep 20
  menu_check "publikclip $EXPECT is the latest version."
  RC=$?
  check "[Restart] menu: \"Check for Updates…\" clicked in the app menu" "$MENU_HOW" test -n "$MENU_HOW"
  shot restarted-menu-answer
  check "[Restart] menu: the running app answers \"publikclip $EXPECT is the latest version.\"" "$ANSWER" test "$RC" = 0
  press_in_dialog OK

  # Variant B: "Later", from a fresh install of the old version.
  log "variant Later: install $FROM again"
  quit_app
  check "[Later] publikclip $FROM installed again" "" install_from_dmg
  gatekeeper_then_clear "[Later] "
  forget_key
  check "[Later] publikclip starts with open" "" launch "$OUT/app-2.log" later
  LOG="$OUT/app-2.log"
  check "[Later] the updater installs $EXPECT by itself" "" wait_for 300 log_has "$LOG" "publikclip updates: publikclip $EXPECT is ready"
  check "[Later] the question is on screen" "" wait_for 30 has_window UserNotificationCenter
  shot later-question
  LATER_PID="$PID"
  if press_in_dialog Later; then
    note "[Later] answered \"Later\"" "System Events click"
    sleep 15
    check "[Later] the app keeps running (same process)" "pid $LATER_PID" kill -0 "$LATER_PID"
    check "[Later] the question is gone" "" no_question
    check "[Later] the running process is still the old code (inode)" "$(running_inode "$LATER_PID") / $(disk_inode)" runs_old_file "$LATER_PID"
    menu_check "publikclip $EXPECT is installed. Restart publikclip to use it."
    RC=$?
    shot later-menu-answer
    if [ -z "$MENU_HOW" ]; then
      MENU_HOW="$(real_menu_click)"
      wait_for 60 dialog_says "publikclip $EXPECT is installed. Restart publikclip to use it."
      RC=$?
      ANSWER="$(dialog_texts)"
      shot later-menu-answer-real
    fi
    check "[Later] menu: \"Check for Updates…\" asks again for the installed $EXPECT" "$MENU_HOW; $ANSWER" test "$RC" = 0
    press_in_dialog Later
    sleep 3
  else
    note "[Later] the question could not be pressed on this runner" "the app is left running with the question open"
  fi
  check "[Later] the new version waits on disk ($EXPECT)" "$(plist "$APP" CFBundleShortVersionString)" test "$(plist "$APP" CFBundleShortVersionString)" = "$EXPECT"
  shot later
  log "variant Later: quit and open again"
  quit_app
  forget_key
  check "[Later] publikclip opens again" "" launch "$OUT/app-3.log" reopen
  check "[Later] after the reopen the running app reports \"$EXPECT is the latest version\"" "" wait_for 120 log_has "$OUT/app-3.log" "publikclip updates: $EXPECT is the latest version"
  check "[Later] after the reopen it is the installed app, the $EXPECT file (inode)" "" runs_disk_file "${PID:-0}"
  check "[Later] after the reopen the running app told its version: $EXPECT (install request)" "" told_version "$EXPECT"
fi
sudo pkill -x tcpdump 2>/dev/null
sleep 1
feed_hellos > "$OUT/feed-requests.txt"
note "TLS ClientHellos to publikhq.com seen (feed-requests.txt)" "$(wc -l < "$OUT/feed-requests.txt" | tr -d ' '): $(head -1 "$OUT/feed-requests.txt")"

# ---- 5. After: signature, Gatekeeper, TCC -----------------------------------------------------
codesign -dvvv "$APP" > "$OUT/codesign-after.txt" 2>&1
DR_AFTER="$(dr "$APP")"
echo "$DR_AFTER" > "$OUT/designated-requirement-after.txt"
check "[after] the app on disk is $EXPECT" "$(plist "$APP" CFBundleShortVersionString)" test "$(plist "$APP" CFBundleShortVersionString)" = "$EXPECT"
check "[after] designated requirement unchanged" "$DR_AFTER" test "$DR_AFTER" = "$DR_BEFORE"
check "[after] signature intact (codesign --verify --strict --deep)" "" codesign --verify --strict --deep "$APP"
check "[after] Gatekeeper accepts the app (Notarized Developer ID)" "" gatekeeper_ok exec "" "$APP"
tcc_rows > "$OUT/tcc-rows-after.jsonl" 2>&1
for s in "${SERVICES[@]}"; do
  check "[after] TCC row still there: $s" "allowed, csreq set" row_ok "$s" "$OUT/tcc-rows-after.jsonl"
  check "[after] the installed app satisfies the stored csreq of $s" "codesign --verify -R <stored csreq>" csreq_ok "$s"
done
note "consent prompts" "a runner cannot show a TCC consent prompt; the stored csreq of every grant is satisfied by the installed app, which is the check tccd makes, so no new prompt is needed"
if [ "$MODE" = update ]; then
  # Only now, for the comparison: the release's own update bundle of $EXPECT.
  mkdir -p "$WORK/compare"
  curl -fsSL "https://github.com/$REPO/releases/download/v$EXPECT/${NAME}_${EXPECT}_aarch64.app.tar.gz" -o "$WORK/compare/new.app.tar.gz" &&
    tar -xzf "$WORK/compare/new.app.tar.gz" -C "$WORK/compare"
  check "[after] the installed app is the $EXPECT release's app, byte for byte (CDHash)" "$(cdhash "$APP")" \
    test -n "$(cdhash "$APP")" -a "$(cdhash "$APP")" = "$(cdhash "$WORK/compare/$NAME.app")"
fi

# ---- What the app logged about the updates, and what this job downloaded ----------------------
for f in "$OUT"/app-*.log; do
  [ -f "$f" ] || continue
  echo "== $(basename "$f")"
  grep -E "publikclip updates:" "$f" | cut -c1-400
done > "$OUT/updater-lines.txt"
cat "$OUT/updater-lines.txt"
check "downloads by this job before the check: only the $FROM_TAG dmg and its checksums" "$(ls "$WORK/dl" | tr '\n' ' ')" \
  test "$(ls "$WORK/dl" | LC_ALL=C sort | tr '\n' ' ')" = "$(printf '%s\n' "$(basename "$DMG")" SHA256SUMS.txt | LC_ALL=C sort | tr '\n' ' ')"

pkill -x "$EXE" 2>/dev/null
shot end
echo
echo "$PASS passed, $FAILS failed"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  { echo "### macOS live update proof: $FROM_TAG -> $EXPECT ($MODE): $PASS passed, $FAILS failed"; echo; cat "$RESULTS"; } >> "$GITHUB_STEP_SUMMARY"
fi
[ "$FAILS" -eq 0 ]
