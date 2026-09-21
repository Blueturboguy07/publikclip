# bugfix-lab CONTROL for cluster publikclip-windows-nsis-bundle-missing.
#
# Same two guide steps as the oracle (package, then install-app run verbatim), but
# WITHOUT the forced linker failure -- a working toolchain end to end. Confirms
# independently, on today's runner and today's checked-out commit, that the guide's
# own "install-app" command is not simply broken by construction: when package
# actually produces a bundle, install-app finds it and proceeds. This is the sibling
# of the oracle's presence check, not a second oracle -- its exit code is evidence,
# not the pass/fail contract for this cluster (that is oracle.sh / the "oracle" job).
#
# Prints BUGFIX_LAB_ABSENT (expected here) or BUGFIX_LAB_PRESENT (would mean the
# install-app step is broken even against a good build -- a different, worse bug).

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Host "[control] $m" }

function Invoke-FreshShell {
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

Say "=== GUIDE STEP dependencies: npm.cmd install ==="
$dep = Invoke-FreshShell -Script "npm.cmd install" -WorkingDirectory $appDir -TimeoutSeconds 600
Write-Host $dep.Output
Say "npm install exit: $($dep.ExitCode)"
if ($dep.ExitCode -ne 0) { Say "BUGFIX_LAB_INCONCLUSIVE: npm install itself failed"; exit 2 }

Say "=== GUIDE STEP install-uv: winget install --id astral-sh.uv -e ... (verbatim) ==="
$uv = Invoke-FreshShell -Script "winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements" -WorkingDirectory $appDir -TimeoutSeconds 420
Write-Host $uv.Output
Say "winget uv exit: $($uv.ExitCode) timedOut: $($uv.TimedOut)"

Say "=== GUIDE STEP package (verbatim, working toolchain) ==="
$packageScript = @'
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
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

Say "=== GUIDE STEP install-app (verbatim) ==="
$installAppScript = @'
$setup = Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe | Select-Object -First 1
Write-Host "SETUP_FOUND:$($null -ne $setup)"
if ($setup) { Write-Host "SETUP_NAME:$($setup.Name)" }
$installer = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -PassThru
$installer.WaitForExit()
Write-Host "INSTALLER_EXIT:$($installer.ExitCode)"
'@
$install = Invoke-FreshShell -Script $installAppScript -WorkingDirectory $appDir -TimeoutSeconds 120
Write-Host "----- install-app step output -----"
Write-Host $install.Output
Say "install-app step process exit: $($install.ExitCode) timedOut: $($install.TimedOut)"

$hit = $install.Output -match [regex]::Escape("Cannot find path")
$found = $install.Output -match "SETUP_FOUND:True"

if ($found -and -not $hit) { Say "BUGFIX_LAB_ABSENT"; exit 0 }
elseif ($hit) { Say "BUGFIX_LAB_PRESENT"; exit 1 }
else { Say "BUGFIX_LAB_INCONCLUSIVE"; exit 2 }
