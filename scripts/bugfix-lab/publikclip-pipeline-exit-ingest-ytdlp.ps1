# bugfix-lab controlled-environment repro (Windows) for cluster:
# publikclip-pipeline-exit-ingest-ytdlp
#
# Symptom (17/33 publikclip reports, both platforms): the INGEST row still
# reads "Downloading video…" or "Updating yt-dlp…" when the app shows
# "The pipeline exited unexpectedly. Resume the job to continue from its
# last checkpoint." — a generic, unattributed crash banner.
#
# Mechanism under test (real, unmodified pipeline source): any yt-dlp
# subprocess failure inside pipeline/publikclip_pipeline/ingest/ytdlp.py
# raises `YtDlpError`, which is NOT a subclass of `queue.StageError`. The
# CLI's `_execute()` (pipeline/publikclip_pipeline/cli.py) only catches
# `queue.StageError` around `queue.run_stages(...)`; a `YtDlpError` (bad
# URL, stale extractor, self-update-retry exhausted, watchdog kill, ...)
# propagates UNCAUGHT and crashes the Python sidecar with no final
# `{"event":"result",...}` JSONL line. The Tauri shell
# (app/src-tauri/src/main.rs::stream_pipeline) redirects the sidecar's
# stderr to Stdio::null(), so the traceback is discarded; it only sees
# stdout stop and a non-zero exit status and emits {"event":"exited"},
# which app/src/App.tsx turns into the generic banner above.
#
# Drives the REAL cli.main(["--jsonl","run",<url>]) -> cmd_run -> _execute
# -> queue.run_stages -> IngestStage.run -> ingest.ytdlp.fetch_meta path
# against a real, deliberately-invalid YouTube video id, so a real yt-dlp
# subprocess actually runs and actually fails (no mocking).
#
# Prints BUGFIX_LAB_PRESENT / BUGFIX_LAB_ABSENT and exits 1 / 0 to match.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..\..

# --- minimal pipeline env: ingest + ytdlp + queue + cli only need httpx
#     (stdlib subprocess/threading otherwise) — matching cli.py's own
#     deferred-import pattern that skips torch/whisperx/etc. until a stage
#     past ingest actually runs, which this repro never reaches either way
#     (see oracle_drive.py docstring for the one narrowing: cli._stages()
#     is patched to [IngestStage()] so those unrelated imports never fire). ---
Push-Location pipeline
uv venv --python 3.12 .venv-oracle
uv pip install --python .venv-oracle\Scripts\python.exe httpx
Pop-Location

$homeDir = Join-Path $env:RUNNER_TEMP "publikclip_home"
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
$env:PUBLIKCLIP_HOME = $homeDir
$env:PUBLIKCLIP_PIPELINE_DIR = (Resolve-Path "pipeline").Path

$stdoutLog = Join-Path $env:RUNNER_TEMP "oracle_out.json"
$url = "https://www.youtube.com/watch?v=zzzzzzzzzzz"  # 11-char, not a real video id

& "pipeline\.venv-oracle\Scripts\python.exe" "scripts\bugfix-lab\oracle_drive.py" $url 1>$stdoutLog
$harnessCode = $LASTEXITCODE

Write-Host "=== publikclip cli.main(['--jsonl','run', '$url']) via oracle_drive.py ==="
Write-Host "harness exit code: $harnessCode"

if ($harnessCode -ne 0 -or -not (Test-Path $stdoutLog) -or (Get-Item $stdoutLog).Length -eq 0) {
    Write-Host "BUGFIX_LAB_ABSENT (oracle could not run — harness failure)"
    exit 2
}

$raw = Get-Content $stdoutLog -Raw
$data = $raw | ConvertFrom-Json

Write-Host "pipeline CLI exit_code: $($data.exit_code)"
$stdoutLines = $data.stdout -split "`n" | Where-Object { $_.Trim() -ne "" }
Write-Host "stdout JSONL lines: $($stdoutLines.Count)"
Write-Host "--- last 3 stdout lines ---"
$stdoutLines | Select-Object -Last 3 | ForEach-Object { Write-Host $_ }

$hasResult = $stdoutLines | Where-Object { $_ -match '"event":\s*"result"' } | Select-Object -First 1
$lastLine = $stdoutLines | Select-Object -Last 1
$lastIsIngestProgress = $lastLine -match '"event":\s*"progress"' -and ($lastLine -match 'Downloading video' -or $lastLine -match 'Updating yt-dlp')

if ($data.uncaught_traceback) {
    Write-Host "--- uncaught Python traceback (discarded by stderr(Stdio::null()) in the real app) ---"
    Write-Host $data.uncaught_traceback
}

if (-not $hasResult -and $lastIsIngestProgress) {
    Write-Host ""
    Write-Host "BUGFIX_LAB_PRESENT (process ended with no final result event; last progress was the ingest yt-dlp message -> generic 'pipeline exited unexpectedly' banner with INGEST still showing that message)"
    exit 1
} else {
    Write-Host ""
    Write-Host "BUGFIX_LAB_ABSENT (a final result event was emitted -- failure reported gracefully instead of crashing)"
    exit 0
}
