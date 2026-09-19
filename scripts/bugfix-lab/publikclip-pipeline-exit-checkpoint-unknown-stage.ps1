# bugfix-lab controlled-environment repro (Windows) for cluster:
# publikclip-pipeline-exit-checkpoint-unknown-stage
#
# Observes: on a fresh Windows machine (no ffmpeg installed — the guide
# never installs it, and the app's only ffmpeg auto-fetch is wired into
# the RENDER stage, not ingest, which runs first), does
# `publikclip --jsonl run <local mp4>` crash with a bare, unattributed
# traceback (no JSONL "result" event) — the exact mechanism that makes
# main.rs emit {"event":"exited"} and the UI show the fully generic
# "The pipeline exited unexpectedly..." banner with no stage named?
#
# Prints BUGFIX_LAB_PRESENT / BUGFIX_LAB_ABSENT and exits 1 / 0 to match.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..\..

Write-Host "--- ffmpeg / ffprobe on this runner's PATH, out of the box ---"
$ffmpegOnPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobeOnPath = Get-Command ffprobe -ErrorAction SilentlyContinue
if ($ffmpegOnPath) { Write-Host "ffmpeg found at: $($ffmpegOnPath.Source)" } else { Write-Host "ffmpeg: NOT on PATH" }
if ($ffprobeOnPath) { Write-Host "ffprobe found at: $($ffprobeOnPath.Source)" } else { Write-Host "ffprobe: NOT on PATH" }

# --- minimal pipeline env: only what the ingest stage needs (numpy/httpx),
#     matching cli.py's deferred heavy imports (torch/whisperx never load
#     until asr/diarize/etc. run, which this repro never reaches) ---
Push-Location pipeline
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e . --no-deps
uv pip install --python .venv\Scripts\python.exe numpy httpx
Pop-Location

# --- tiny local mp4 fixture (2s, video+audio) — this runner has no ffmpeg
#     at all (confirmed above), same as a real fresh user's machine, so we
#     install one via choco JUST to synthesize the test input. This binary
#     is never added to the sanitized PATH used for the actual repro run
#     below — it only ever builds fixtures/sample.mp4. ---
New-Item -ItemType Directory -Force -Path fixtures | Out-Null
$fixture = (Resolve-Path fixtures).Path + "\sample.mp4"
if (-not (Test-Path $fixture)) {
    $fixtureFfmpeg = if ($ffmpegOnPath) { $ffmpegOnPath.Source } else { $null }
    if (-not $fixtureFfmpeg) {
        Write-Host "no ffmpeg on runner; installing one via choco solely to build the fixture…"
        choco install ffmpeg -y --no-progress | Out-Null
        $fixtureFfmpeg = "C:\ProgramData\chocolatey\bin\ffmpeg.exe"
        if (-not (Test-Path $fixtureFfmpeg)) {
            $found = Get-Command ffmpeg -ErrorAction SilentlyContinue
            if ($found) { $fixtureFfmpeg = $found.Source }
        }
    }
    if (-not (Test-Path $fixtureFfmpeg)) {
        Write-Host "BUGFIX_LAB_ABSENT (oracle could not build fixture: no ffmpeg obtainable on runner)"
        exit 2
    }
    & $fixtureFfmpeg -y -f lavfi -i "testsrc=size=320x240:rate=10:duration=2" `
        -f lavfi -i "sine=frequency=440:duration=2" -shortest -pix_fmt yuv420p $fixture
}

# --- driver: the real CLI entry point, unmodified pipeline source ---
$driver = Join-Path $env:RUNNER_TEMP "run_repro.py"
@'
import sys
from publikclip_pipeline.cli import main
raise SystemExit(main(sys.argv[1:]))
'@ | Set-Content -Path $driver -Encoding utf8

$homeDir = Join-Path $env:RUNNER_TEMP "publikclip_home"
New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
$env:PUBLIKCLIP_HOME = $homeDir

# Sanitize PATH so no system ffmpeg/ffprobe resolves — this IS the fresh-
# machine condition the guide leaves every real user in (unlike this CI
# image, which the check above may show already carries ffmpeg).
$safePath = (Resolve-Path "pipeline\.venv\Scripts").Path + ";C:\Windows\System32;C:\Windows"
$env:Path = $safePath

$stdoutLog = Join-Path $env:RUNNER_TEMP "stdout.jsonl"
$stderrLog = Join-Path $env:RUNNER_TEMP "stderr.txt"

& "pipeline\.venv\Scripts\python.exe" $driver --jsonl run $fixture 1>$stdoutLog 2>$stderrLog
$code = $LASTEXITCODE

Write-Host "=== publikclip --jsonl run <local mp4>, sanitized PATH (no ffmpeg) ==="
Write-Host "pipeline process exit code: $code"
Write-Host "--- stdout (the only channel main.rs reads) ---"
Get-Content $stdoutLog
Write-Host "--- stderr (discarded by the real app; shown here as oracle evidence) ---"
Get-Content $stderrLog -Tail 8

if ($code -eq 0) {
    Write-Host "BUGFIX_LAB_ABSENT (pipeline succeeded end to end)"
    exit 0
}

$hasResult = Select-String -Path $stdoutLog -Pattern '"event": "result"' -Quiet
if ($hasResult) {
    Write-Host "BUGFIX_LAB_ABSENT (pipeline failed but emitted an attributed result event)"
    exit 0
}

Write-Host "BUGFIX_LAB_PRESENT (bare crash, no result event -> generic checkpoint-exit banner, no stage named)"
exit 1
