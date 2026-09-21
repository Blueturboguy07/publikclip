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
  rmSync,
} from "node:fs";
import { execFileSync } from "node:child_process";
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
// execFileSync throws on a nonzero exit (locator finds nothing) before this
// line can ever inspect its result -- the friendly message below used to be
// dead code on exactly the machines that need it (uv missing from PATH),
// which instead saw a raw "Command failed: where uv" Node stack trace. Catch
// that specific failure and fall through to the one intentional throw.
let uvPath = "";
try {
  uvPath = execFileSync(locator, ["uv"], { encoding: "utf8" })
    .split(/\r?\n/)[0]
    .trim();
} catch {
  uvPath = "";
}
if (!uvPath || !existsSync(uvPath)) {
  throw new Error("uv not found on PATH — install it before building");
}
copyFileSync(uvPath, path.join(res, "bin", uvName));
if (process.platform !== "win32") {
  chmodSync(path.join(res, "bin", uvName), 0o755);
}

console.log(`resources staged at ${res}`);
