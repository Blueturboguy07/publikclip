# Windows repro: diarize ImportError "Lazy import of LazyModule ... speechbrain.integrations.k2_fsa"
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..\..
$env:PYTHONUTF8 = "1"
Push-Location pipeline
uv sync --frozen --group pipeline --python 3.12
if ($LASTEXITCODE -ne 0) { Write-Host "uv sync failed"; exit 2 }
Pop-Location
New-Item -ItemType Directory -Force -Path fixtures | Out-Null
$fixture = (Resolve-Path fixtures).Path + "\media.mp4"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { choco install ffmpeg -y --no-progress | Out-Null }
Add-Type -AssemblyName System.Speech
$wav = (Resolve-Path fixtures).Path + "\speech.wav"
$syn = New-Object System.Speech.Synthesis.SpeechSynthesizer
$syn.SetOutputToWaveFile($wav)
$syn.Speak("Welcome to the show everyone. Today we are going to talk about how to cook a perfect pot of rice. First you rinse the rice three times until the water runs clear. Then you add one and a half cups of water for every cup of rice. Bring it to a boil, cover it, and let it simmer for fifteen minutes.")
$syn.Dispose()
ffmpeg -y -f lavfi -i "testsrc=size=320x240:rate=10" -i $wav -shortest -pix_fmt yuv420p $fixture 2>&1 | Out-Null
$env:ORACLE_FIXTURE_MEDIA = $fixture
$env:PUBLIKCLIP_HOME = Join-Path $env:RUNNER_TEMP "pc_home"
New-Item -ItemType Directory -Force -Path $env:PUBLIKCLIP_HOME | Out-Null
$raw = Join-Path $env:RUNNER_TEMP "raw.json"
$env:ORACLE_OUT = $raw
Push-Location pipeline
uv run --frozen --no-sync --group pipeline python ..\scripts\bugfix-lab\publikclip-20261019-890940_driver.py 2>&1 | Out-Host
$pyExit = $LASTEXITCODE
Pop-Location
Write-Host "python exit: $pyExit"
if (-not (Test-Path $raw)) { Write-Host "no result file written"; exit 2 }
$txt = [System.IO.File]::ReadAllText($raw)
$safe = ($txt -replace "[^\x20-\x7e]"," ") -replace "::",": :"
for ($i=0; $i -lt $safe.Length; $i+=700) { Write-Host ("RAW> " + $safe.Substring($i,[Math]::Min(700,$safe.Length-$i))) }
$verdict = ""
try { $verdict = ($txt | ConvertFrom-Json).verdict } catch { Write-Host "result file is not JSON"; exit 2 }
Write-Host "driver verdict: $verdict (canned_asr=$($env:ORACLE_CANNED_ASR))"
if ($verdict -eq "present") { Write-Host "BUGFIX_LAB_PRESENT"; exit 1 }
if ($verdict -eq "absent") { Write-Host "BUGFIX_LAB_ABSENT"; exit 0 }
# anything else (HF 403 / rate limit / network / another crash) is not a verdict on this bug
Write-Host "other outcome (oracle could not observe the bug)"; exit 2
