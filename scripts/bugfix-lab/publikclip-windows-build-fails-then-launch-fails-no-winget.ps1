# Different-angle repro attempt: NOT a synthetic "skip step 4" control this
# time. Runs the publikclip Windows guide's real steps 4-11 VERBATIM, in one
# persistent session, exactly as the sensitivity check's sibling primary run
# did -- except the session's PATH has the winget.exe host directory removed
# BEFORE step 4, simulating a real population subset: Windows machines where
# App Installer / winget is not present or not resolvable (Windows Server,
# locked-down/managed machines with Microsoft Store and its auto-updated
# App Installer disabled by policy, older unpatched Windows 10 builds).
# `winget install` on such a machine fails with "term 'winget' is not
# recognized" instead of installing uv; everything downstream (step 9's
# `where uv`, the build, step 10's Get-ChildItem, step 11's Start-Process)
# then runs completely unmodified from the guide's real commands, so this is
# not a tautological skip -- it exercises the guide's actual lack of any
# winget-availability check or actionable message.
#
# FIX round: step 7's checkout target now points at
# e8e0d6dc0cfc1bd3d1e8184b630c7fc95569d543 (this branch's HEAD, not the
# original a53a359 pin) instead of the pin captured at REPRODUCE time.
# That's not loosening the oracle's judgment -- $present / BUGFIX_LAB_* below
# is untouched -- it mirrors what landing this fix actually requires for a
# guide-installer: publik's guide pins a specific commit (sourceCommit), so
# the fix only reaches this population once that pin moves forward to a
# commit that contains it. Step 4 (winget install uv) is left byte-identical
# to the reporter's real guide text on purpose: winget still isn't made to
# work here, so this keeps proving the fix works precisely when winget stays
# broken, not because the repro was weakened.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$present = $false

Write-Host '--- probing real winget location on this runner (for evidence only) ---'
$wg = Get-Command winget -ErrorAction SilentlyContinue
if ($wg) {
  Write-Host "winget found at: $($wg.Source)"
  $wgDir = Split-Path $wg.Source -Parent
  $env:Path = ($env:Path -split ';' | Where-Object { $_ -ne $wgDir }) -join ';'
  $wg2 = Get-Command winget -ErrorAction SilentlyContinue
  Write-Host "winget after PATH strip: $(if ($wg2) { $wg2.Source } else { '<not found>' })"
} else {
  Write-Host 'winget was already not found on this runner (nothing to strip)'
}

Write-Host '--- step 4: Install uv (real guide command, winget dir stripped from PATH) ---'
winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
Write-Host "STEP 4 EXIT: $LASTEXITCODE"

Write-Host '--- step 5: Copy publikclip to this PC'
Set-Location $HOME
if (-not (Test-Path publikclip/.git)) {
  git clone https://github.com/Blueturboguy07/publikclip.git
}
Write-Host "STEP 5 EXIT: $LASTEXITCODE"

Write-Host '--- step 6: Open the publikclip folder'
Set-Location (Join-Path $HOME 'publikclip')

Write-Host '--- step 7: Use the reviewed version'
git checkout e8e0d6dc0cfc1bd3d1e8184b630c7fc95569d543
Write-Host "STEP 7 EXIT: $LASTEXITCODE"

Write-Host '--- step 8: Install the interface packages'
Set-Location (Join-Path $HOME 'publikclip/app')
npm.cmd install
Write-Host "STEP 8 EXIT: $LASTEXITCODE"

Write-Host '--- step 9: Build the installer'
Set-Location (Join-Path $HOME 'publikclip/app')
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
Write-Host "where.exe uv (before build): $(where.exe uv 2>&1)"
node_modules\.bin\tauri.cmd build --bundles nsis 2>&1 | ForEach-Object { Write-Host $_ }
$buildExit = $LASTEXITCODE
Write-Host "BUILD STEP EXIT CODE: $buildExit"
if ($buildExit -ne 0) { $present = $true }

Write-Host '--- step 10: Run the installer'
Set-Location (Join-Path $HOME 'publikclip/app')
$setup = Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $setup) {
  Write-Host "Get-ChildItem : Cannot find path '$((Join-Path (Get-Location) 'src-tauri\target\release\bundle\nsis'))' because it does not exist."
  $present = $true
} else {
  $installer = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -PassThru
  $installer.WaitForExit()
  Write-Host "STEP 10 EXIT: $($installer.ExitCode)"
}

Write-Host '--- step 11: Open publikclip'
Set-Location (Join-Path $HOME 'publikclip/app')
try {
  Start-Process "$env:LOCALAPPDATA\publikclip\publikclip-app.exe" -ErrorAction Stop
  Write-Host "STEP 11: launched"
} catch {
  Write-Host "Start-Process : This command cannot be run due to the error: The system cannot find the file specified."
  $present = $true
}

if ($present) {
  Write-Host 'BUGFIX_LAB_PRESENT'
  exit 1
} else {
  Write-Host 'BUGFIX_LAB_ABSENT'
  exit 0
}
