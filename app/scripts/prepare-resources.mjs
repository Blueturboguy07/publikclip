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
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
} from "node:fs";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
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
const uvPath = resolveUvBinary();
copyFileSync(uvPath, path.join(res, "bin", uvName));
if (process.platform !== "win32") {
  chmodSync(path.join(res, "bin", uvName), 0o755);
}

console.log(`resources staged at ${res}`);

// --- helpers ----------------------------------------------------------

// Find a uv binary to copy into the bundle. Prefers whatever is already on
// PATH; on Windows, self-heals by fetching astral-sh/uv's own release
// binary when nothing is found there, so a build doesn't hard-fail just
// because a separate "install uv" step was skipped, run in a different
// shell session, or silently failed (see cluster
// publikclip-windows-nsis-bundle-missing: the guide's "install-app" step
// then threw a raw "bundle\nsis does not exist" error with no indication
// uv was ever the cause).
function resolveUvBinary() {
  const locator = process.platform === "win32" ? "where" : "which";
  // execFileSync throws on a nonzero exit (locator finds nothing) before
  // this line can ever inspect its result -- catch that specific failure
  // and fall through to self-heal (Windows) or the friendly error below,
  // instead of letting a raw "Command failed: where uv" Node stack trace
  // be the only thing a builder without uv on PATH ever sees.
  try {
    const found = execFileSync(locator, ["uv"], { encoding: "utf8" })
      .split(/\r?\n/)[0]
      .trim();
    if (found && existsSync(found)) return found;
  } catch {
    // fall through
  }

  if (process.platform === "win32") {
    try {
      return selfInstallUvWindows();
    } catch (err) {
      console.warn(
        `uv self-install failed (${err.message}); falling back to the PATH error`,
      );
    }
  }

  throw new Error("uv not found on PATH — install it before building");
}

// Downloads the official astral-sh/uv Windows release zip to a temp dir,
// verifies it against astral-sh's own published sha256, extracts it with
// the OS's built-in `tar` (bsdtar, bundled with Windows since 10 1803 --
// handles .zip too), and returns the extracted uv.exe's path. No shell
// eval / pipe-to-iex: a pinned URL, a checksum check, then a local
// extract -- the same binary the guide's own "install-uv" step (winget
// install astral-sh.uv) would have put on PATH, fetched here instead so
// this build doesn't depend on that separate step having run first.
function selfInstallUvWindows() {
  const dir = path.join(tmpdir(), "publikclip-uv-selfheal");
  const exePath = path.join(dir, "uv.exe");
  if (existsSync(exePath)) return exePath; // reuse a prior self-heal this job

  mkdirSync(dir, { recursive: true });
  const asset = "uv-x86_64-pc-windows-msvc.zip";
  const base = `https://github.com/astral-sh/uv/releases/latest/download/${asset}`;
  const zipPath = path.join(dir, asset);
  const shaPath = path.join(dir, `${asset}.sha256`);

  console.log(
    "uv not found on PATH -- downloading it for this build (astral-sh/uv official release)",
  );
  execFileSync("curl.exe", ["-sL", "--fail", "-o", zipPath, base], {
    stdio: "inherit",
  });
  execFileSync(
    "curl.exe",
    ["-sL", "--fail", "-o", shaPath, `${base}.sha256`],
    { stdio: "inherit" },
  );

  const expected = readFileSync(shaPath, "utf8").trim().split(/\s+/)[0];
  const actual = createHash("sha256")
    .update(readFileSync(zipPath))
    .digest("hex");
  if (!expected || expected.toLowerCase() !== actual.toLowerCase()) {
    throw new Error(
      `uv download checksum mismatch (expected ${expected || "<none>"}, got ${actual})`,
    );
  }

  execFileSync("tar.exe", ["-xf", zipPath, "-C", dir], { stdio: "inherit" });
  if (!existsSync(exePath)) {
    throw new Error("uv.exe missing from the downloaded archive after extraction");
  }
  console.log(`self-installed uv at ${exePath}`);
  return exePath;
}
