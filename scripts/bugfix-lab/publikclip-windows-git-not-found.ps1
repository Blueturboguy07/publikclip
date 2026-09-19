# bugfix-lab oracle body for cluster: publikclip-windows-git-not-found
#
# Reproduces the exact failure the two reporters hit (bug_reports
# f2a11cd2-6c4b-4b48-9381-52f1d6397acb and 97136263-e6f7-42c0-bd68-7d92bc95d93a,
# 2026-08-14/15): on a Windows machine with no Git for Windows on PATH (never
# installed, or installed but PowerShell never reopened), publikclip's
# Windows install guide's "clone" step (branch.steps[4], the 5th step Iris
# actually walks a reader through -- see render-guide.mts output below) used
# to fail with `git : The term 'git' is not recognized ...`, so the
# publikclip folder was never created, and the following "Open the
# publikclip folder" step then also failed with a PathNotFound error.
#
# FIX (this revision): publik@7fc0198 (fix/publikclip-windows-git-not-found,
# based on origin/main e467460), guide version 7 -> 8. The "clone" step now
# guards on `Get-Command git` and installs Git itself via winget (silent)
# when it's missing, then refreshes $env:Path from the Machine+User registry
# values -- confirmed live on windows-latest CI (throwaway probe, now
# deleted) that this is where Git for Windows' installer actually registers
# PATH. See lib/guides/publikclip.ts on that commit for the full comment.
#
# Commands below are rendered VERBATIM via:
#   cd ~/publik (worktree at fix/publikclip-windows-git-not-found, 7fc0198)
#   && npx tsx ~/bugfix-lab/bin/render-guide.mts publikclip windows --to 6
# sourceCommit unchanged: a53a359b985b1d2d666266062936cc186f02340b (==
# publikclip's current main -- this population is NOT on a stale pin; only
# the guide's own step text changed, not what gets checked out).

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

Write-Host "=== baseline: is git normally on this runner? ==="
$normally = Get-Command git -ErrorAction SilentlyContinue
if ($normally) {
  Write-Host "git is present on this CI image at: $($normally.Source)"
} else {
  Write-Host "git was already absent on this CI image."
}

Write-Host "=== hiding git from PATH for this session (simulates the reporters' machines: no Git for Windows installed, or installed but PowerShell not reopened since) ==="
$parts = $env:Path -split ';'
$kept = $parts | Where-Object {
  $_ -and -not (Test-Path (Join-Path $_ 'git.exe') -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $_ 'git.cmd') -ErrorAction SilentlyContinue)
}
$env:Path = ($kept -join ';')
Remove-Item Env:\GIT_EXEC_PATH -ErrorAction SilentlyContinue

$after = Get-Command git -ErrorAction SilentlyContinue
if ($after) {
  Write-Host "COULD NOT HIDE GIT: still resolvable at $($after.Source)"
  Write-Host "BUGFIX_LAB_ABSENT"
  exit 2
}
Write-Host "git is now unresolvable on PATH, as the reporters had it."

cd ~
Remove-Item -Recurse -Force publikclip -ErrorAction SilentlyContinue

Write-Host "=== step 5 (branch.steps[4], id=clone): 'Copy publikclip to this PC' -- run verbatim (post-fix text) ==="
$step5Log = Join-Path $env:TEMP 'step5.log'
$step6Log = Join-Path $env:TEMP 'step6.log'

powershell -NoProfile -Command @'
cd ~
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User')
}
if (-not (Test-Path publikclip/.git)) {
git clone https://github.com/Blueturboguy07/publikclip.git
}
'@ *> $step5Log
Get-Content $step5Log | Write-Host

Write-Host "=== step 6 (branch.steps[5], id=enter-folder): 'Open the publikclip folder' -- run verbatim ==="
cd ~
powershell -NoProfile -Command @'
cd publikclip
'@ *> $step6Log
Get-Content $step6Log | Write-Host

$step5Text = Get-Content $step5Log -Raw
$step6Text = Get-Content $step6Log -Raw
$folderExists = Test-Path (Join-Path $HOME 'publikclip\.git')

Write-Host "=== evidence ==="
Write-Host "publikclip\.git exists after clone step: $folderExists"

$gitNotRecognized = $step5Text -match "'git' is not recognized"
$cdFailed = $step6Text -match "Cannot find path" -or $step6Text -match "ObjectNotFound|ItemNotFoundException"

if ($gitNotRecognized -and -not $folderExists -and $cdFailed) {
  Write-Host "PRESENT: clone step failed with the reporters' exact 'git' is not recognized error, no publikclip folder was created, and the following cd step failed too."
  Write-Host "BUGFIX_LAB_PRESENT"
  exit 1
} elseif ($folderExists -and -not $gitNotRecognized) {
  Write-Host "ABSENT: git resolved and the clone step succeeded (publikclip/.git exists)."
  Write-Host "BUGFIX_LAB_ABSENT"
  exit 0
} else {
  Write-Host "INCONCLUSIVE: neither the expected-present nor expected-absent pattern matched cleanly."
  Write-Host "BUGFIX_LAB_ABSENT"
  exit 2
}
