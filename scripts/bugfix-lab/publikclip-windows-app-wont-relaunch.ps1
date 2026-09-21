# bugfix-lab oracle body for cluster publikclip-windows-app-wont-relaunch.
#
# Reporter's sequence: app opened fine -> entered API key -> pasted a long
# YouTube URL -> an in-app pipeline error appeared -> reporter closed the
# app -> relaunching it now makes the window flash open and instantly
# close, and it "hasn't opened since then".
#
# This script reproduces that sequence end to end against the REAL,
# installed Windows app (NSIS-built, silently installed, launched as a
# real Win32 process — not a mock):
#   1. Build + silently install the app, exactly like the windows.yml
#      release-smoke workflow does.
#   2. Write secrets.json + the onboarded marker under PUBLIKCLIP_HOME,
#      matching what the app's own save_gemini_key/mark_onboarded Tauri
#      commands write when a user finishes onboarding and enters a key.
#   3. Drive the SAME pipeline invocation the installed app's run_job
#      command would spawn (resources\bin\uv.exe --directory
#      resources\pipeline run publikclip --jsonl run <url>), against a
#      YouTube URL that is deliberately invalid so it fails the way real
#      long-video runs have failed for other reporters in this campaign
#      (publikclip-pipeline-exit-ingest-ytdlp). This leaves whatever job
#      dir / checkpoint / db state a real failed run leaves, without
#      needing to drive the Tauri UI from CI.
#   4. Relaunch the installed exe TWICE (matching "it hasn't opened since
#      then") and check, each time, whether the process is still alive
#      after the same 15s window the passing windows.yml smoke check
#      uses. Redirect stdout/stderr to files so a Rust panic message is
#      captured even though release builds use the windows subsystem
#      (no attached console).
#
# Prints BUGFIX_LAB_PRESENT and exits 1 if either relaunch dies within
# 15s. Prints BUGFIX_LAB_ABSENT and exits 0 if both relaunches are still
# alive after 15s (killed afterward). Any setup failure throws (caught by
# the workflow step, which will show as a red CI step rather than a silent
# ABSENT).

$ErrorActionPreference = 'Stop'

Write-Host "=== build + install ==="
Push-Location app
npm ci
npx tauri build --bundles nsis
Pop-Location

$setup = Get-ChildItem app\src-tauri\target\release\bundle\nsis -Filter *-setup.exe | Select-Object -First 1
if (-not $setup) { throw "no NSIS installer produced" }
Write-Host "installer: $($setup.Name)"
Start-Process -FilePath $setup.FullName -ArgumentList "/S" -Wait

$installDir = Join-Path $env:LOCALAPPDATA "publikclip"
if (-not (Test-Path $installDir)) { throw "install dir missing: $installDir" }
$exe = Get-ChildItem $installDir -Filter *.exe |
  Where-Object { $_.Name -notmatch "uninstall" } |
  Select-Object -First 1
if (-not $exe) { throw "no app exe under $installDir" }
Write-Host "installed exe: $($exe.FullName)"

$uv = Join-Path $installDir "resources\bin\uv.exe"
$pipelineDir = Join-Path $installDir "resources\pipeline"
if (-not (Test-Path $uv)) { throw "bundled uv.exe missing: $uv" }
if (-not (Test-Path $pipelineDir)) { throw "bundled pipeline dir missing: $pipelineDir" }

# PUBLIKCLIP_HOME is set at the workflow's job level (see bugfix-lab.yml),
# matching windows.yml's own isolation convention, and read by BOTH the
# Rust app (home_dir()) and the Python pipeline (config.home_dir()) so the
# CLI-triggered failed run and the relaunched app see the exact same state
# a real user's default ~\.publikclip would hold across two launches.
$appHome = $env:PUBLIKCLIP_HOME
if (-not $appHome) { throw "PUBLIKCLIP_HOME not set — expected the workflow to set it" }
New-Item -ItemType Directory -Force -Path $appHome | Out-Null

Write-Host "=== simulate 'entered API key' + finished onboarding ==="
$secrets = @{ gemini_api_key = "oracle-fake-key-not-real" } | ConvertTo-Json
Set-Content -Path (Join-Path $appHome "secrets.json") -Value $secrets
Set-Content -Path (Join-Path $appHome "onboarded") -Value "1"

Write-Host "=== run a job to a failure state (same invocation run_job spawns) ==="
# 11-char not-a-real-video id: fails fast and deterministically at the
# ingest stage (confirmed live on windows-ci by the sibling cluster
# publikclip-pipeline-exit-ingest-ytdlp, run 35430174398), leaving a job
# dir + db row + whatever checkpoint state a real failed long-video run
# leaves, without needing real network/model downloads.
$badUrl = "https://www.youtube.com/watch?v=zzzzzzzzzzz"
$pipelineOut = Join-Path $env:RUNNER_TEMP "pipeline-run.log"
& $uv --directory $pipelineDir run publikclip --jsonl run $badUrl 2>&1 | Tee-Object -FilePath $pipelineOut
$pipelineExit = $LASTEXITCODE
Write-Host "pipeline CLI exit code: $pipelineExit (nonzero is EXPECTED — this is the reporter's pipeline error)"

Write-Host "=== job/db state left behind under PUBLIKCLIP_HOME ==="
Get-ChildItem -Recurse $appHome | ForEach-Object { Write-Host $_.FullName }

# No app GUI process exists yet in this run (the failing job above was
# driven straight through the CLI, not the Tauri window), so there is
# nothing to close — go straight to relaunching, twice, matching the
# reporter's "closed it, reopened it, and it hasn't opened since".
$attempts = @()
for ($i = 1; $i -le 2; $i++) {
  Write-Host "=== relaunch attempt $i ==="
  $stdout = Join-Path $env:RUNNER_TEMP "app-stdout-$i.log"
  $stderr = Join-Path $env:RUNNER_TEMP "app-stderr-$i.log"
  $proc = Start-Process -FilePath $exe.FullName -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
  Start-Sleep -Seconds 15
  $proc.Refresh()
  $alive = -not $proc.HasExited
  Write-Host "attempt $i alive after 15s: $alive"
  if (-not $alive) {
    Write-Host "attempt $i exit code: $($proc.ExitCode)"
    Write-Host "--- stdout ---"
    if (Test-Path $stdout) { Get-Content $stdout }
    Write-Host "--- stderr ---"
    if (Test-Path $stderr) { Get-Content $stderr }
  } else {
    Stop-Process -Id $proc.Id -Force
  }
  $attempts += $alive
  Start-Sleep -Seconds 2
}

if ($attempts -contains $false) {
  Write-Host "BUGFIX_LAB_PRESENT"
  exit 1
} else {
  Write-Host "BUGFIX_LAB_ABSENT"
  exit 0
}
