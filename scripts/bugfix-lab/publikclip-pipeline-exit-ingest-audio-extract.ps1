# bugfix-lab controlled-environment repro (Windows) for cluster:
# publikclip-pipeline-exit-ingest-audio-extract
#
# Symptom (report 06a321e2, Windows): the INGEST row reads "Extracting
# analysis audio…" — i.e. AFTER download/probe already show done/cached —
# when the app shows "The pipeline exited unexpectedly. Resume the job to
# continue from its last checkpoint." — a generic, unattributed crash
# banner. The reporter also says no Resume button is visible on screen.
#
# Mechanism under test (real, unmodified pipeline source):
# pipeline/publikclip_pipeline/ingest/stage.py's IngestStage.run() calls
# normalize.extract_analysis_audio() UNWRAPPED — contrast the
# normalize.probe() call 8 lines above it in the same function, which IS
# wrapped (`except normalize.FfmpegError as err: raise StageError(...) from
# err`). When extract_analysis_audio()'s ffmpeg subprocess genuinely fails,
# normalize.py's _run() raises a real `FfmpegError`, which is NOT a
# subclass of `queue.StageError`. cli.py's `_execute()` only catches
# `queue.StageError` around `queue.run_stages(...)`; FfmpegError propagates
# UNCAUGHT and crashes the Python sidecar with no final
# `{"event":"result",...}` JSONL line. The Tauri shell
# (app/src-tauri/src/main.rs::stream_pipeline) redirects the sidecar's
# stderr to Stdio::null(), so the traceback is discarded; it only sees
# stdout stop and a non-zero exit status and emits {"event":"exited"},
# which app/src/App.tsx turns into the generic banner above.
#
# Drives the REAL cli.main(["--jsonl","run",<local-file>]) -> cmd_run ->
# _execute -> queue.run_stages -> IngestStage.run -> normalize.probe()
# [genuinely succeeds] -> normalize.extract_analysis_audio() [genuinely
# fails] path against a real local mp4 fixture
# (fixtures/ingest-audio-extract-fail.mp4: normal H.264+AAC CFR mp4 with
# every AUDIO SAMPLE BYTE overwritten in place — file structure/length
# untouched, so `ffprobe -show_format -show_streams` still reports a
# normal video+audio stream pair while `ffmpeg -vn -ac 1 -ar 16000 -c:a
# pcm_s16le` genuinely fails to decode any frame and exits nonzero — see
# fixtures/build_fixture_corrupt_audio.py), so a real ffmpeg subprocess
# actually runs and actually fails at exactly this sub-step (no mocking,
# no network needed for a local-file job).
#
# Prints BUGFIX_LAB_PRESENT / BUGFIX_LAB_ABSENT and exits 1 / 0 to match.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..\..

Write-Host "=== ffmpeg/ffprobe on PATH ==="
$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobeCmd = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $ffmpegCmd -or -not $ffprobeCmd) {
    Write-Host "ffmpeg/ffprobe not preinstalled — installing via choco..."
    choco install ffmpeg -y | Out-Null
    $env:Path = "C:\ProgramData\chocolatey\bin;$env:Path"
    $ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    $ffprobeCmd = Get-Command ffprobe -ErrorAction SilentlyContinue
}
if (-not $ffmpegCmd -or -not $ffprobeCmd) {
    Write-Host "BUGFIX_LAB_ABSENT (oracle could not run — no ffmpeg/ffprobe available)"
    exit 2
}
Write-Host "ffmpeg: $($ffmpegCmd.Source)"
Write-Host "ffprobe: $($ffprobeCmd.Source)"
& ffmpeg -version | Select-Object -First 1 | ForEach-Object { Write-Host $_ }

# --- minimal pipeline env: ingest + normalize + queue + cli only need
#     httpx (ingest/ytdlp.py imports it at module scope even though this
#     file-based job never calls it) — matching cli.py's own deferred-
#     import pattern that skips torch/whisperx/etc. until a stage past
#     ingest actually runs, which this repro never reaches either way (see
#     oracle_drive.py docstring for the one narrowing: cli._stages() is
#     patched to [IngestStage()] so those unrelated imports never fire). ---
Push-Location pipeline
uv venv --python 3.12 .venv-oracle
uv pip install --python .venv-oracle\Scripts\python.exe httpx
Pop-Location

$homeDir = Join-Path $env:RUNNER_TEMP "publikclip_home"
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
$env:PUBLIKCLIP_HOME = $homeDir
$env:PUBLIKCLIP_PIPELINE_DIR = (Resolve-Path "pipeline").Path

$fixture = (Resolve-Path "scripts\bugfix-lab\fixtures\ingest-audio-extract-fail.mp4").Path
Write-Host ""
Write-Host "=== fixture probe (product's exact ffprobe args) ==="
& ffprobe -v error -print_format json -show_format -show_streams $fixture 2>$null |
    Tee-Object -Variable probeJsonLines | Out-Null
$probeJson = ($probeJsonLines -join "`n") | ConvertFrom-Json
$vStreams = $probeJson.streams | Where-Object { $_.codec_type -eq "video" }
$aStreams = $probeJson.streams | Where-Object { $_.codec_type -eq "audio" }
Write-Host "video streams: $($vStreams.Count)  audio streams: $($aStreams.Count)"
if ($vStreams.Count -gt 0) {
    Write-Host "avg_frame_rate=$($vStreams[0].avg_frame_rate) r_frame_rate=$($vStreams[0].r_frame_rate) (equal -> not VFR -> normalize_to_cfr never runs)"
}

$stdoutLog = Join-Path $env:RUNNER_TEMP "oracle_out.json"
& "pipeline\.venv-oracle\Scripts\python.exe" "scripts\bugfix-lab\oracle_drive.py" $fixture 1>$stdoutLog
$harnessCode = $LASTEXITCODE

Write-Host ""
Write-Host "=== publikclip cli.main(['--jsonl','run', '$fixture']) via oracle_drive.py ==="
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
$lastIsExtractProgress = $lastLine -match '"event":\s*"progress"' -and ($lastLine -match 'Extracting analysis audio')

if ($data.uncaught_traceback) {
    Write-Host "--- uncaught Python traceback (discarded by stderr(Stdio::null()) in the real app) ---"
    Write-Host $data.uncaught_traceback
}

if (-not $hasResult -and $lastIsExtractProgress) {
    Write-Host ""
    Write-Host "BUGFIX_LAB_PRESENT (process ended with no final result event; last progress was 'Extracting analysis audio…' -- probe/download had already succeeded -- generic 'pipeline exited unexpectedly' banner)"
    exit 1
} else {
    Write-Host ""
    Write-Host "BUGFIX_LAB_ABSENT (a final result event was emitted -- failure reported gracefully instead of crashing)"
    exit 0
}
