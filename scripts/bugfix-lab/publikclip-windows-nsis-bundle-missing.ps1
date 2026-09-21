# bugfix-lab oracle for cluster publikclip-windows-nsis-bundle-missing.
#
# Report 3b08b8de: the Windows guide's "install-app" step (lib/guides/publikclip.ts,
# publikclipWindowsSteps(), array position 9 of 12) fails with
#   Get-ChildItem : Cannot find path '...src-tauri\target\release\bundle\nsis' because
#   it does not exist.
# The reporter attached no screenshot of the prior "package" step (position 8), so the
# upstream cause of THAT step's failure is unconfirmed. This oracle forces one concrete,
# plausible real-world cause the cluster notes name explicitly (missing C++ build tools /
# no working MSVC linker -- distinct from the already-separately-tracked cargo-not-found
# cluster, whose PATH-insurance line this repo's guide already carries and which a Sep 19
# CI run proved does NOT break this guide's package step) and then runs the guide's
# "install-app" command VERBATIM against the resulting state.
#
# Exit 1 = bug PRESENT: install-app's own Get-ChildItem throws the reporter's exact
#          "Cannot find path ...bundle\nsis..." error because package really did leave no
#          bundle directory at all.
# Exit 0 = bug ABSENT: install-app finds a *-setup.exe and proceeds to Start-Process.
# Exit 2 = oracle could not run -- a precondition failed (say which).
#
# Prints BUGFIX_LAB_PRESENT / BUGFIX_LAB_ABSENT / BUGFIX_LAB_INCONCLUSIVE.

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Host "[oracle] $m" }

function Invoke-FreshShell {
    # Runs $Script in a BRAND NEW pwsh.exe process, matching how a reader types each
    # guide step into PowerShell and matching Iris-windows's one-fresh-shell-per-command
    # autopilot model -- env vars set by a prior step do not silently carry over unless
    # this script itself re-sets them, exactly as for a real reader.
    # stdout/stderr are read ASYNCHRONOUSLY (event handlers), never via a blocking
    # ReadToEnd() before WaitForExit -- a prior cluster's oracle proved that ordering
    # deadlocks on verbose cargo/tauri output that fills the OS pipe buffer.
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [int]$TimeoutSeconds = 600
    )
    $scriptPath = [System.IO.Path]::GetTempFileName() + ".ps1"
    Set-Content -LiteralPath $scriptPath -Value $Script -Encoding UTF8
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "pwsh.exe"
    $psi.Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`""
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $outBuilder = New-Object System.Text.StringBuilder
    $outEvent = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
        if ($null -ne $EventArgs.Data) { [void]$Event.MessageData.AppendLine($EventArgs.Data) }
    } -MessageData $outBuilder
    $errEvent = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
        if ($null -ne $EventArgs.Data) { [void]$Event.MessageData.AppendLine($EventArgs.Data) }
    } -MessageData $outBuilder

    [void]$proc.Start()
    $proc.BeginOutputReadLine()
    $proc.BeginErrorReadLine()
    $finished = $proc.WaitForExit($TimeoutSeconds * 1000)
    if (-not $finished) {
        try { Start-Process -FilePath "taskkill" -ArgumentList "/pid", "$($proc.Id)", "/T", "/F" -Wait -WindowStyle Hidden } catch {}
        Start-Sleep -Seconds 2
    }
    Unregister-Event -SourceIdentifier $outEvent.Name -ErrorAction SilentlyContinue
    Unregister-Event -SourceIdentifier $errEvent.Name -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $scriptPath -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        TimedOut = (-not $finished)
        ExitCode = $(if ($finished) { $proc.ExitCode } else { -1 })
        Output   = $outBuilder.ToString()
    }
}

$repoRoot = (Get-Location).Path
$appDir = Join-Path $repoRoot "app"

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue) -or -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    Say "BUGFIX_LAB_INCONCLUSIVE: git/npm not on the runner's PATH"
    exit 2
}
Say "checked-out commit: $(git rev-parse HEAD)"

# --- guide step "dependencies": cd app ; npm.cmd install (verbatim) --------
Say "=== GUIDE STEP dependencies: npm.cmd install ==="
$dep = Invoke-FreshShell -Script "npm.cmd install" -WorkingDirectory $appDir -TimeoutSeconds 600
Write-Host $dep.Output
Say "npm install exit: $($dep.ExitCode) timedOut: $($dep.TimedOut)"
if ($dep.ExitCode -ne 0) { Say "BUGFIX_LAB_INCONCLUSIVE: npm install itself failed"; exit 2 }

# --- guide step "package", FORCED TO FAIL before it can produce a bundle --
# The guide's own command, unmodified, plus one extra env var this oracle adds to deny
# cargo a working linker -- the "no C++ build tools" real-world case the cluster's
# proposed oracle names, and a DIFFERENT failure class than publikclip-windows-build-
# cargo-not-found (that cluster is about cargo.exe itself not resolving; here cargo
# resolves fine and runs, but the final link step it depends on cannot).
Say "=== GUIDE STEP package (verbatim command; MSVC linker withheld) ==="
$packageScript = @'
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
$env:CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER = "bugfix-lab-missing-linker.exe"
node_modules\.bin\tauri.cmd build --bundles nsis
Write-Host "PACKAGE_EXIT:$LASTEXITCODE"
'@
$pkg = Invoke-FreshShell -Script $packageScript -WorkingDirectory $appDir -TimeoutSeconds 900
Write-Host "----- package step output -----"
Write-Host $pkg.Output
Say "package step process exit: $($pkg.ExitCode) timedOut: $($pkg.TimedOut)"

$bundleNsisDir = Join-Path $appDir "src-tauri\target\release\bundle\nsis"
$bundleDirExists = Test-Path -LiteralPath $bundleNsisDir
Say "PRECONDITION -- bundle\nsis directory exists after package step: $bundleDirExists"

# --- guide step "install-app", run VERBATIM ---------------------------------
Say "=== GUIDE STEP install-app (verbatim) ==="
$installAppScript = @'
$setup = Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe | Select-Object -First 1
$installer = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -PassThru
$installer.WaitForExit()
'@
$install = Invoke-FreshShell -Script $installAppScript -WorkingDirectory $appDir -TimeoutSeconds 120
Write-Host "----- install-app step output -----"
Write-Host $install.Output
Say "install-app step process exit: $($install.ExitCode) timedOut: $($install.TimedOut)"

$needle = "Cannot find path"
$needle2 = "bundle\nsis"
$needle3 = "does not exist"
$hit = ($install.Output -match [regex]::Escape($needle)) -and
       ($install.Output -match [regex]::Escape($needle2)) -and
       ($install.Output -match [regex]::Escape($needle3))

Say "reporter's exact error text present in install-app output: $hit"

if ($hit -and -not $bundleDirExists) {
    Say "BUGFIX_LAB_PRESENT"
    exit 1
} elseif (-not $hit -and $bundleDirExists) {
    Say "BUGFIX_LAB_ABSENT"
    exit 0
} else {
    Say "BUGFIX_LAB_INCONCLUSIVE: bundle dir exists=$bundleDirExists but error-text hit=$hit (inconsistent state)"
    exit 2
}
