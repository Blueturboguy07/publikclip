# bugfix-lab oracle body for cluster: publikclip-windows-build-fails-then-launch-fails
#
# Runs the publikclip Windows guide's steps 4 (install uv), 5-8 (clone/pin/deps),
# 9 (build the installer) and 10 (run the installer) EXACTLY as rendered from
# lib/guides/publikclip.ts (guide version 7, sourceCommit
# a53a359b985b1d2d666266062936cc186f02340b), in ONE persistent PowerShell
# session -- matching iris-windows's ShellSession contract (session persists
# across guide steps; see src/services/autopilot/shell.ts). Steps 1-3 (Open
# PowerShell / install Rust / install C++ build tools) are reader-driven "open"
# steps with no command; the windows-latest runner image ships cargo and the
# MSVC C++ toolchain already on the machine PATH, which is the same
# precondition those steps establish for a real reader who followed them.
#
# Presence bar (from clusters.json): step 9 (`node scripts/prepare-resources.mjs
# && npm run build`, invoked as tauri's beforeBuildCommand) exits non-zero, AND
# step 10's `Get-ChildItem ... bundle\nsis` finds no path / no setup exe.

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$present = $false
$evidence = @()

function Note($line) {
  Write-Host $line
  $script:evidence += $line
}

Note '--- step 4: Install uv (winget, same as the guide) ---'
winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
Note "winget exit code: $LASTEXITCODE"

Note '--- step 5-6: clone publikclip to this PC ---'
Set-Location $HOME
if (-not (Test-Path publikclip/.git)) {
  git clone https://github.com/Blueturboguy07/publikclip.git
}
Set-Location (Join-Path $HOME 'publikclip')

Note '--- step 7: pin to the reviewed commit ---'
git checkout a53a359b985b1d2d666266062936cc186f02340b 2>&1 | ForEach-Object { Note $_ }

Note '--- step 8: install the interface packages ---'
Set-Location (Join-Path $HOME 'publikclip/app')
npm.cmd install 2>&1 | Tee-Object -Variable npmOut | ForEach-Object { Note $_ }
Note "npm install exit code: $LASTEXITCODE"

Note '--- step 9: build the installer (the guide''s own command, verbatim) ---'
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
Note "PATH for build step: $env:Path"
Note "where.exe uv (before build): $(where.exe uv 2>&1)"
node_modules\.bin\tauri.cmd build --bundles nsis 2>&1 | Tee-Object -Variable buildOut | ForEach-Object { Note $_ }
$buildExit = $LASTEXITCODE
Note "BUILD STEP EXIT CODE: $buildExit"
if ($buildExit -ne 0) {
  $present = $true
  Note "PRESENCE SIGNAL: build step (step 9) exited non-zero ($buildExit), matching the reporter's beforeBuildCommand failure."
}

Note '--- step 10: run the installer (the guide''s own command, verbatim) ---'
$setup = Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $setup) {
  $present = $true
  Note "PRESENCE SIGNAL: no *-setup.exe under src-tauri\target\release\bundle\nsis (path exists: $(Test-Path 'src-tauri\target\release\bundle\nsis')) -- matches reporter's Get-ChildItem/Start-Process chain failure."
} else {
  Note "setup exe found: $($setup.FullName)"
  $installer = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -PassThru
  $installer.WaitForExit()
  Note "installer exit code: $($installer.ExitCode)"
}

Note '--- step 11: open publikclip (only meaningful if step 10 actually installed something) ---'
$exePath = "$env:LOCALAPPDATA\publikclip\publikclip-app.exe"
if (-not (Test-Path $exePath)) {
  $present = $true
  Note "PRESENCE SIGNAL: launch target missing ($exePath does not exist) -- matches reporter's Start-Process 'cannot find the file specified'."
} else {
  Note "launch target present: $exePath"
}

Note '--- summary ---'
Note "$($evidence -join [Environment]::NewLine)" | Out-Null

if ($present) {
  Write-Host 'BUGFIX_LAB_PRESENT'
  exit 1
} else {
  Write-Host 'BUGFIX_LAB_ABSENT'
  exit 0
}
