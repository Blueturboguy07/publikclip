# Updates

publikclip updates itself with `tauri-plugin-updater`. Every update package is signed with the
publik update key. The app checks each package against the public key in
`app/src-tauri/tauri.conf.json` before it installs anything. The code is a copy of the GSpot
updater (Blueturboguy07/gspot, `docs/updates.md`), changed for publikclip as this file says.

## How it works

- The app reads `https://publikhq.com/api/updates/publikclip/latest.json`. publik serves the
  `latest.json` asset of the scan-approved release tag, byte for byte. publik does not sign or
  change it, so the release `latest.json` must be complete and correct. The route answers 404
  until a scan-approved release carries a `latest.json` asset. A 404 is silent in the background
  check, and a manual check shows it as an error.
- The app checks 10 s after launch and then once a day. "Check for Updates…" checks at once: it
  is in the publikclip menu on macOS, and in a Help menu on the Windows main window (the sign-in
  window has no menu). `PUBLIK_NO_AUTO_UPDATE=1` turns off the automatic checks (the CI
  workflows set it).
- The update is the whole app. On macOS it is the whole `publikclip.app`, on Windows the NSIS
  installer. Both carry `resources/`: the bundled `uv` and the Python pipeline source. So the
  pipeline changes together with the app. The Python env in `PUBLIKCLIP_HOME` is not part of
  the update.
- macOS: the app downloads the new `publikclip.app`, verifies it, puts it in place of the old
  one, and asks "Restart" or "Later". If the user cannot write to the app's folder, the app does
  not replace it in the background. It asks "Install and Restart" or "Later", and macOS asks for
  an admin password only after "Install and Restart".
- Windows: the app downloads the installer, verifies it, and asks "Restart and Install" or
  "Later". The installer runs with a progress bar only, then opens publikclip again.
- The signature must also name the version that `latest.json` announces
  (`requireSignedVersion`). The app never installs an older version.
- A failed automatic check writes one line to stderr only. A failed manual check shows the error.
- The old notice ("publikclip X is out", `app/src-tauri/src/update.rs`) stays. It links to the
  release page and downloads nothing.
- Code: `app/src-tauri/src/updates.rs`. Proof: `.github/workflows/updater-proof.yml`.

## The key

| Where | What |
|---|---|
| Release Mac, login keychain, service `publik-update-key` | `mann-private` (private key), `mann-password` (its password), `mann-public` (public key) |
| GitHub secrets on this repo | `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` (used by `publik-build.yml`, `release.yml` and `updater-proof.yml`) |
| `app/src-tauri/tauri.conf.json` | `plugins.updater.pubkey` (public, key ID `F5E0515C77912BD0`, the same key as GSpot) |

Keep a backup of the private key and its password. Without them, installed copies can never
update again.

## Builds with and without update files

`bundle.createUpdaterArtifacts` is OFF in `tauri.conf.json` (GSpot has it on). A build from
source (README, `scripts/bugfix-lab`) has no update key, and `tauri build` fails without the key
when the setting is on. The builds that make update files turn it on themselves:

| Build | Update files |
|---|---|
| Windows in `release.yml`, `publik-build.yml`, `updater-proof.yml` | `--config` with `createUpdaterArtifacts: true` and the key: `tauri build` writes `<installer>.sig` |
| macOS anywhere | None from `tauri build`. `scripts/updater-pack-macos.sh` packs the final, stapled app after signing |
| `macos.yml`, `windows.yml`, source builds | None |

An update bundle of an app that is signed again later must never ship: it would not carry the
Developer ID identity. That is why the macOS bundle is made only by the pack script, from the
stapled app.

## Cut a release on the release Mac

This is how every release since 0.2.4 was made (this repo holds no Apple secrets).

1. Set the new version in `app/src-tauri/tauri.conf.json`, `app/src-tauri/Cargo.toml` and
   `app/package.json`. Push.
2. Run `publik-build.yml` on that commit. Its artifacts are `publikclip-macos-arm64-unsigned`
   (the unsigned `.app` zip) and `publikclip-windows-setup` (`publikclip_<v>_x64-setup.exe` and
   its `.sig`).
3. On the release Mac, sign the app with the Developer ID (the bundled
   `Contents/Resources/resources/bin/uv` first, then the app, with the hardened runtime and
   `app/src-tauri/entitlements.plist`), notarize it and staple it. Make, sign, notarize and staple
   the dmg as before. Then pack the stapled app (the script reads the key from the keychain):
   ```sh
   scripts/updater-pack-macos.sh publikclip.app out
   ```
   `out/` now has `publikclip_<v>_aarch64.app.tar.gz` and its `.sig`. Put the dmg and the two
   Windows files in `out/` too.
4. Write the feed. The script refuses a signature that does not verify or names another version:
   ```sh
   node scripts/updater-latest-json.mjs --version <v> \
     --base-url https://github.com/Blueturboguy07/publikclip/releases/download/v<v>/ \
     --platform darwin-aarch64=out/publikclip_<v>_aarch64.app.tar.gz \
     --platform windows-x86_64=out/publikclip_<v>_x64-setup.exe \
     --notes-file notes.md --out out/latest.json
   scripts/updater-check-release.sh out <v> v<v>
   ```
   The check compares `latest.json` with the files and the tag, and the update bundle with the
   app in the dmg (same code, notarized, stapled).
5. Make a draft release with the dmg, the setup.exe, the `.app.tar.gz`, both `.sig` files,
   `latest.json` and `SHA256SUMS.txt`.
6. Run `clean-mac-check.yml` with the draft tag. It runs the same update-file check on a clean
   runner. Publish the release. Installed copies update when publik approves the release and
   serves its `latest.json`.

## Cut a release with release.yml

`release.yml` does steps 3 to 5 on GitHub runners when this repo has the Apple secrets (it does
not today). It refuses to start without the update key, packs the stapled app with
`scripts/updater-pack-macos.sh`, signs the Windows installer in `tauri build`, and attaches the
dmg, the setup.exe, the `.app.tar.gz`, both `.sig` files and `latest.json` (absolute asset URLs
of the tag) to the release.

## Roll back

The app installs only a version higher than its own. You cannot push an older version.

1. Stop the bad release at once: withdraw it in publik, or delete its `latest.json` asset.
   Copies that did not update yet stay on the old version.
2. Revert the bad change, give it a higher version (for example 0.2.10 after a bad 0.2.9), and
   cut a release. Copies on the bad version update to it.

Do not turn on `allowDowngrades`. It removes the only guard against an old, signed release
served as a new one.

## Rotate the key

The public key is compiled into each copy, so a new key reaches users only through an update.

1. Make a new key: `app/node_modules/.bin/tauri signer generate -w new.key`. Store the private
   key, its password and the public key in the keychain items and the repo secrets (new names
   first, so the old key stays available).
2. Put the new public key in `tauri.conf.json`. Release this version signed with the OLD key.
3. From the next release on, sign with the new key. Copies that skipped the bridge release can
   still update only to a release signed with the old key, so keep the old key until you accept
   that those copies stop updating.
4. If the private key leaks, cut the bridge release at once. A leaked key alone cannot push an
   update: the attacker must also control the feed on publikhq.com.

The key is shared by the publik apps (GSpot, publikclip and others). Rotate it in all of them.

## Test

Run `updater-proof.yml`. It builds 0.0.1 and 0.0.2, installs 0.0.1 on `macos-latest` and
`windows-latest`, and checks that 0.0.1 refuses three bad feeds and then updates to 0.0.2,
with the bundled pipeline and uv. Then it starts 0.0.1 once more and checks that the restart
question is on screen while the app keeps running. A push to `feat/updater` or `updater/**`
also starts it. The macOS builds are signed with a throwaway certificate made on the runner,
because this repo holds no Developer ID secrets.

The app has a test hook for this: `PUBLIK_UPDATE_SELF_TEST=version|check|install` runs the
updater with no window and no prompt and prints one JSON line. `prompt` runs the background
path and shows the real question. The hook does not click the buttons.
