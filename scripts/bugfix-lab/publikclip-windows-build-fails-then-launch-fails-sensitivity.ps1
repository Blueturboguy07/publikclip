# Sensitivity check ONLY -- not the population oracle. Proves oracle.sh's
# detection logic actually fires PRESENT when uv is genuinely missing from
# PATH at build time (skips the guide's "Install uv" step on purpose), so a
# not_reproduced verdict on the real oracle isn't just a silently-broken check.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$present = $false

Write-Host '--- (sensitivity) deliberately SKIPPING uv install ---'
Set-Location $HOME
if (-not (Test-Path publikclip/.git)) {
  git clone https://github.com/Blueturboguy07/publikclip.git
}
Set-Location (Join-Path $HOME 'publikclip')
git checkout a53a359b985b1d2d666266062936cc186f02340b 2>&1 | Out-Null
Set-Location (Join-Path $HOME 'publikclip/app')
npm.cmd install 2>&1 | Out-Null
Write-Host "npm install exit code: $LASTEXITCODE"

Write-Host '--- step 9 (build), uv never installed ---'
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
Write-Host "where.exe uv (before build): $(where.exe uv 2>&1)"
node_modules\.bin\tauri.cmd build --bundles nsis 2>&1 | ForEach-Object { Write-Host $_ }
$buildExit = $LASTEXITCODE
Write-Host "BUILD STEP EXIT CODE: $buildExit"
if ($buildExit -ne 0) { $present = $true }

if ($present) {
  Write-Host 'BUGFIX_LAB_PRESENT'
  exit 1
} else {
  Write-Host 'BUGFIX_LAB_ABSENT'
  exit 0
}
