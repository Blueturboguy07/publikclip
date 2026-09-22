# bugfix-lab controlled-environment repro (Windows) for cluster:
# publikclip-pipeline-exit-speakers-model
#
# Observes: on the pinned commit (matches lib/guides/publikclip.ts's
# sourceCommit and repo main/win-port), with an already-cached-but-corrupted
# CAM++ speaker-embedding checkpoint (models/specs.py registers CAMPPLUS
# with sha256=None, so models/registry.py's ensure() never validates a
# cached file), does diarize/stage.py's DiarizeStage.run() crash the whole
# Python sidecar with no final JSONL "result" event while showing "Loading
# speaker model…" -- the exact mechanism that makes app/src-tauri/src/
# main.rs's stream_pipeline() emit {"event":"exited"} and the UI show the
# generic "The pipeline exited unexpectedly. Resume the job to continue
# from its last checkpoint." banner?
#
# Prints BUGFIX_LAB_PRESENT / BUGFIX_LAB_ABSENT and exits 1 / 0 to match.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..\..

Write-Host "--- ffmpeg / ffprobe on this runner's PATH ---"
$ffmpegOnPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpegOnPath) { Write-Host "ffmpeg found at: $($ffmpegOnPath.Source)" } else { Write-Host "ffmpeg: NOT on PATH" }

# --- minimal pipeline env: httpx/torch/numpy only (diarize stage's deps up
#     to and including the crash point; whisperx/speechbrain/opencv/
#     onnxruntime/librosa/torchaudio are never imported because
#     cli._stages() is narrowed to ingest+asr+diarize and this CI run only
#     drives the poison case -- the mac-local oracle already proved the
#     valid/negative-control case on Sep 21 2026, see bugfix-lab/work/
#     publikclip-pipeline-exit-speakers-model/RESULT.json) ---
Push-Location pipeline
uv venv --python 3.12 .venv-oracle
uv pip install --python .venv-oracle\Scripts\python.exe -e . --no-deps
uv pip install --python .venv-oracle\Scripts\python.exe httpx torch numpy
Pop-Location

# --- tiny local mp4 fixture (2s, video+audio) ---
New-Item -ItemType Directory -Force -Path fixtures | Out-Null
$fixture = (Resolve-Path fixtures).Path + "\media.mp4"
if (-not (Test-Path $fixture)) {
    $fixtureFfmpeg = if ($ffmpegOnPath) { $ffmpegOnPath.Source } else { $null }
    if (-not $fixtureFfmpeg) {
        Write-Host "no ffmpeg on runner; installing one via choco solely to build the fixture…"
        choco install ffmpeg -y --no-progress | Out-Null
        $found = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if ($found) { $fixtureFfmpeg = $found.Source }
    }
    if (-not (Test-Path $fixtureFfmpeg)) {
        Write-Host "BUGFIX_LAB_ABSENT (oracle could not build fixture: no ffmpeg obtainable on runner)"
        exit 2
    }
    & $fixtureFfmpeg -y -f lavfi -i "testsrc=size=320x240:rate=10:duration=2" `
        -f lavfi -i "sine=frequency=440:duration=2" -shortest -pix_fmt yuv420p $fixture
}

# --- driver: the real CLI resume path, unmodified pipeline source, through
#     the real DiarizeStage.run() -> registry.ensure() -> campplus.load_model()
#     -> torch.load() call with a pre-poisoned CAM++ checkpoint file ---
$env:PUBLIKCLIP_PIPELINE_DIR = (Resolve-Path pipeline).Path
$env:ORACLE_FIXTURE_MEDIA = $fixture
$homeDir = Join-Path $env:RUNNER_TEMP "publikclip_home"
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
$env:PUBLIKCLIP_HOME = $homeDir

$rawLog = Join-Path $env:RUNNER_TEMP "driver_raw.json"
& "pipeline\.venv-oracle\Scripts\python.exe" `
    "scripts\bugfix-lab\publikclip-pipeline-exit-speakers-model_driver.py" `
    1> $rawLog 2> "$env:RUNNER_TEMP\driver_stderr.txt"
$harnessCode = $LASTEXITCODE

Write-Host "--- driver harness exit code: $harnessCode ---"
if ($harnessCode -ne 0 -or -not (Test-Path $rawLog) -or (Get-Item $rawLog).Length -eq 0) {
    Write-Host "--- driver stderr ---"
    Get-Content "$env:RUNNER_TEMP\driver_stderr.txt" -ErrorAction SilentlyContinue
    Write-Host "BUGFIX_LAB_ABSENT (oracle could not run: driver harness failed)"
    exit 2
}

$raw = Get-Content $rawLog -Raw | ConvertFrom-Json
Write-Host "job_id: $($raw.job_id)"
Write-Host "pipeline CLI exit_code: $($raw.exit_code)"
Write-Host "--- stdout JSONL (what main.rs streams) ---"
Write-Host $raw.stdout
if ($raw.uncaught_traceback) {
    Write-Host "--- uncaught Python traceback (this is what stderr(Stdio::null()) discards in the real app) ---"
    Write-Host $raw.uncaught_traceback
}

$events = @()
foreach ($line in ($raw.stdout -split "`n")) {
    if ($line.Trim().Length -gt 0) {
        try { $events += ($line | ConvertFrom-Json) } catch {}
    }
}
$hasResult = $false
$lastProgressMsg = $null
foreach ($e in $events) {
    if ($e.event -eq "result") { $hasResult = $true }
    if ($e.event -eq "progress") { $lastProgressMsg = $e.message }
}

Write-Host "final 'result' event present: $hasResult"
Write-Host "last progress message: $lastProgressMsg"

if ((-not $hasResult) -and ($lastProgressMsg -eq "Loading speaker model…")) {
    Write-Host "BUGFIX_LAB_PRESENT (crashed with no result event, last progress was 'Loading speaker model…' -> generic exit banner while SPEAKERS shows that message)"
    exit 1
} else {
    Write-Host "BUGFIX_LAB_ABSENT (a final result event was emitted, or the crash point differs)"
    exit 0
}
