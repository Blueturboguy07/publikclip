#!/usr/bin/env node
// Stage the Python pipeline + a uv binary into src-tauri/resources for
// bundling. Packaged builds run: resources/bin/uv --directory
// resources/pipeline run publikclip — the env bootstraps on first launch.
//
// Node instead of bash so the exact same script runs on macOS and Windows
// (`beforeBuildCommand` executes under whatever shell the platform has).
// The exclude list keeps envs, caches and tests out of the bundle —
// wav2vec2_checkpoints is a stray HF cache a dev machine may carry, 700+ MB
// that must never ride into the app.
import {
  chmodSync,
  copyFileSync,
  cpSync,
  createWriteStream,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
} from "node:fs";
import { execFileSync } from "node:child_process";
import { get as httpsGet } from "node:https";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const appDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repoDir = path.dirname(appDir);
const res = path.join(appDir, "src-tauri", "resources");

const EXCLUDES = new Set([
  ".venv",
  "__pycache__",
  ".pytest_cache",
  "tests",
  "wav2vec2_checkpoints",
]);

rmSync(path.join(res, "pipeline"), { recursive: true, force: true });
mkdirSync(path.join(res, "bin"), { recursive: true });

cpSync(path.join(repoDir, "pipeline"), path.join(res, "pipeline"), {
  recursive: true,
  filter: (src) => !src.split(path.sep).some((part) => EXCLUDES.has(part)),
});

// uv: copy the host binary (same arch as the build machine / bundle target).
const uvName = process.platform === "win32" ? "uv.exe" : "uv";
const locator = process.platform === "win32" ? "where" : "which";

// `where`/`which` exits non-zero (execFileSync throws) when nothing on PATH
// matches — that's the ordinary "not installed in this shell yet" case, not
// a script bug. It fires whenever the Windows guide's winget install-uv step
// didn't land uv on this session's PATH (winget missing/broken, a fresh
// PowerShell window never opened, a managed machine with App Installer
// disabled) — winget itself is only the guide's most common cause, so this
// has to be handled here regardless of what step 4's command text says.
function locateOnPath() {
  try {
    const out = execFileSync(locator, ["uv"], { encoding: "utf8" })
      .split(/\r?\n/)[0]
      .trim();
    return out && existsSync(out) ? out : null;
  } catch {
    return null;
  }
}

// Same fallback shape as pipeline/publikclip_pipeline/render/ffmpeg_bin.py's
// static-ffmpeg fetch: when nothing capable exists on the machine, download
// the official static build once instead of leaving the user stuck. Here
// that means astral-sh/uv's own released binaries, straight from GitHub —
// no winget, no package manager, nothing outside this one build script.
function uvReleaseAsset() {
  const archMap = { x64: "x86_64", arm64: "aarch64" };
  const arch = archMap[process.arch];
  if (!arch) return null;
  if (process.platform === "win32") {
    return { name: `uv-${arch}-pc-windows-msvc.zip`, kind: "zip" };
  }
  if (process.platform === "darwin") {
    return { name: `uv-${arch}-apple-darwin.tar.gz`, kind: "tar" };
  }
  if (process.platform === "linux") {
    return { name: `uv-${arch}-unknown-linux-gnu.tar.gz`, kind: "tar" };
  }
  return null;
}

function download(url, dest, redirectsLeft = 5) {
  return new Promise((resolve, reject) => {
    httpsGet(url, (response) => {
      const { statusCode, headers } = response;
      if (
        statusCode >= 300 &&
        statusCode < 400 &&
        headers.location &&
        redirectsLeft > 0
      ) {
        response.resume();
        download(headers.location, dest, redirectsLeft - 1).then(
          resolve,
          reject,
        );
        return;
      }
      if (statusCode !== 200) {
        response.resume();
        reject(new Error(`download failed: HTTP ${statusCode}`));
        return;
      }
      const file = createWriteStream(dest);
      response.pipe(file);
      file.on("finish", () => file.close(() => resolve()));
      file.on("error", reject);
    }).on("error", reject);
  });
}

function findBinary(dir, name) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      const found = findBinary(full, name);
      if (found) return found;
    } else if (entry.name === name) {
      return full;
    }
  }
  return null;
}

async function bootstrapUv() {
  const asset = uvReleaseAsset();
  if (!asset) return null;
  const scratch = mkdtempSync(path.join(os.tmpdir(), "publikclip-uv-"));
  const archivePath = path.join(scratch, asset.name);
  const url = `https://github.com/astral-sh/uv/releases/latest/download/${asset.name}`;
  try {
    console.log(
      `uv not found on PATH — fetching ${asset.name} from astral-sh/uv releases (one-time)...`,
    );
    await download(url, archivePath);
    if (asset.kind === "zip") {
      // Expand-Archive ships with Windows PowerShell 5.1+ — no extra tool
      // needed on the platform that actually hits this fallback in practice.
      execFileSync("powershell", [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Expand-Archive",
        "-LiteralPath",
        `"${archivePath}"`,
        "-DestinationPath",
        `"${scratch}"`,
        "-Force",
      ]);
    } else {
      execFileSync("tar", ["-xzf", archivePath, "-C", scratch]);
    }
    const found = findBinary(scratch, uvName);
    if (found) {
      if (process.platform !== "win32") {
        chmodSync(found, 0o755);
      }
      return found;
    }
    console.warn(`uv bootstrap: ${uvName} not found inside ${asset.name}`);
  } catch (err) {
    console.warn(`uv bootstrap download failed: ${err.message}`);
  }
  return null;
}

let uvPath = locateOnPath();
if (!uvPath) {
  uvPath = await bootstrapUv();
}
if (!uvPath || !existsSync(uvPath)) {
  throw new Error(
    "uv not found on PATH and could not be downloaded automatically — install it before building (https://docs.astral.sh/uv/)",
  );
}
copyFileSync(uvPath, path.join(res, "bin", uvName));
if (process.platform !== "win32") {
  chmodSync(path.join(res, "bin", uvName), 0o755);
}

console.log(`resources staged at ${res}`);
