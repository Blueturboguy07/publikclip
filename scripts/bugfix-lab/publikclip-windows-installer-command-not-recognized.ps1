<#
  Oracle for cluster: publikclip-windows-installer-command-not-recognized

  What the reporter did: followed publikclip's Windows guide (the only rendered
  path today for someone without desktop Iris is GuideArticle.tsx, served at
  /publikclip/install/windows — confirmed by reading
  components/iris/GuideArticle.tsx in the publik repo, which says explicitly:
  "This is what's left for a crawler or a reader without JavaScript, or
  without Iris"). Every step's instruction is pure advisory text
  ("Type this into PowerShell, then press Enter") with a Copy-command button
  and NO verification of which shell the reader is actually typing into. The
  guide itself tells the reader to close/reopen a shell more than once
  (after installing Rust; "Open a new PowerShell window so uv joins the
  PATH"), so nothing stops a reader from reopening Command Prompt instead of
  PowerShell for a later step.

  This script proves the resulting failure mode by literally doing what a
  reader who ended up in cmd.exe would do: feed the exact "Run the installer"
  step's command text (PowerShell-only: a `$var = ...` assignment plus the
  `Start-Process` cmdlet) to a FRESH cmd.exe process and see whether cmd
  rejects it the way the reporter described --
  "'$setup' is not recognized ... 'Start-Process' is not recognized ...".

  Tests two versions of the command text:
    - CURRENT: lib/guides/publikclip.ts on today's publik default branch
      (guide version 7, as rendered by render-guide.mts on 2026-09-21).
    - HISTORICAL: the exact text live on 2026-08-13/14 (publik commit
      6813bf7, guide version 2) -- what this report's author actually had,
      used only to confirm the oracle reproduces the reporter's literal
      error_text as a sensitivity check, not as the pass/fail gate.

  Exit code reflects the CURRENT text (what a guide-installer gets today):
    1 = bug PRESENT (cmd.exe rejects the PowerShell-only command)
    0 = bug ABSENT
    2 = oracle could not run
#>

$ErrorActionPreference = "Stop"
$work = Join-Path $env:RUNNER_TEMP "publikclip-installer-oracle"
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Run-InCmd {
    param(
        [string]$Label,
        [string]$CommandText
    )
    # A first attempt ran the whole block as one .bat via `cmd /c call file.bat`
    # and found that cmd aborts the WHOLE batch (exit 255) after the first
    # line's pipe (`Get-ChildItem ... | Select-Object ...`) fails to launch --
    # later lines never even got a chance to print their own "not recognized"
    # error, which would have under-counted the failure. Running each line as
    # its own freshly spawned `cmd.exe /c` process sidesteps that and is at
    # least as faithful a model of "pasting this block into an open cmd.exe
    # window" -- each Enter press submits one line for cmd to execute, and a
    # failed line never stops the ones after it in a real interactive session.
    $lines = $CommandText -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 }
    $combined = ""
    $lastExit = 0
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $outPath = Join-Path $work "$Label.$i.out.txt"
        $errPath = Join-Path $work "$Label.$i.err.txt"
        $proc = Start-Process -FilePath "$env:WINDIR\System32\cmd.exe" `
            -ArgumentList "/c", $line `
            -WorkingDirectory $work `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $outPath -RedirectStandardError $errPath
        $stdout = if (Test-Path $outPath) { Get-Content $outPath -Raw } else { "" }
        $stderr = if (Test-Path $errPath) { Get-Content $errPath -Raw } else { "" }
        $combined += "`n> $line`n$stdout`n$stderr"
        $lastExit = $proc.ExitCode
    }
    return @{
        ExitCode = $lastExit
        Output   = $combined
    }
}

# --- CURRENT: publik default branch, 2026-09-21, guide version 7 ---
# Rendered via: cd $WORK/publik && npx tsx bin/render-guide.mts publikclip windows --json
# step id "install-app", command field, verbatim.
$currentCommand = @'
$setup = Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe | Select-Object -First 1
$installer = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -PassThru
$installer.WaitForExit()
'@

# --- HISTORICAL: publik commit 6813bf7 (landed 2026-08-13, live through
# 2026-09-18's 66c5fd0), guide version 2 -- what the 2026-08-14 reporter had.
# Retrieved via: git show 6813bf7:lib/guides/publikclip.ts
$historicalCommand = @'
$setup = Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe | Select-Object -First 1
Start-Process -FilePath $setup.FullName -Wait
'@

Write-Host "=== Running CURRENT (v7) install-app command in a fresh cmd.exe ==="
$current = Run-InCmd -Label "current" -CommandText $currentCommand
Write-Host $current.Output
Write-Host "cmd.exe exit code: $($current.ExitCode)"

Write-Host ""
Write-Host "=== Running HISTORICAL (v2, 2026-08-14 era) install-app command in a fresh cmd.exe ==="
$historical = Run-InCmd -Label "historical" -CommandText $historicalCommand
Write-Host $historical.Output
Write-Host "cmd.exe exit code: $($historical.ExitCode)"

$notRecognized = "is not recognized as an internal or external command"

$currentSetupFailed = $current.Output -match [regex]::Escape("'`$setup' $notRecognized")
$currentInstallerOrStartProcessFailed =
    ($current.Output -match [regex]::Escape("'`$installer' $notRecognized")) -or
    ($current.Output -match [regex]::Escape("'Start-Process' $notRecognized"))

$histSetupFailed = $historical.Output -match [regex]::Escape("'`$setup' $notRecognized")
$histStartProcessFailed = $historical.Output -match [regex]::Escape("'Start-Process' $notRecognized")

Write-Host ""
Write-Host "=== Evidence ==="
Write-Host "CURRENT text: `$setup not-recognized=$currentSetupFailed ; Start-Process/`$installer not-recognized=$currentInstallerOrStartProcessFailed"
Write-Host "HISTORICAL text (matches reporter's literal error_text): `$setup not-recognized=$histSetupFailed ; Start-Process not-recognized=$histStartProcessFailed"

if ($histSetupFailed -and $histStartProcessFailed) {
    Write-Host "HISTORICAL text reproduces the reporter's exact error_text verbatim in a fresh cmd.exe session."
} else {
    Write-Host "HISTORICAL text did NOT reproduce the reporter's exact error_text -- oracle may be unsound, investigate."
}

if ($currentSetupFailed -and $currentInstallerOrStartProcessFailed) {
    Write-Host "BUGFIX_LAB_PRESENT"
    exit 1
} else {
    Write-Host "BUGFIX_LAB_ABSENT"
    exit 0
}
