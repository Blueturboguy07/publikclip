# bugfix-lab oracle for cluster publikclip-windows-build-cargo-not-found.
#
# Reproduces the Windows install guide's "package" step (lib/guides/publikclip.ts,
# publikclipWindowsSteps()) on a genuinely rust-less machine, running each guide
# step in its OWN fresh pwsh.exe process -- the same "one short-lived PowerShell
# per command" model Iris's autopilot uses (iris-windows/src/main/powershell-session.ts),
# so env vars set in one step (or by an installer's registry write) do NOT
# automatically carry into the next process unless something re-reads them.
#
# Exit 1 = bug PRESENT (cargo metadata "program not found" while cargo.exe
#          exists on disk at %USERPROFILE%\.cargo\bin).
# Exit 0 = bug ABSENT (the build step's shell resolves cargo and gets past
#          the cargo-metadata call).
# Exit 2 = oracle could not run (a precondition failed -- say which).
#
# Prints BUGFIX_LAB_PRESENT / BUGFIX_LAB_ABSENT / BUGFIX_LAB_INCONCLUSIVE.

$ErrorActionPreference = 'Continue'

function Invoke-FreshShell {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    # Runs $Script in a BRAND NEW pwsh.exe process -- no profile, no inherited
    # in-memory state from whatever spawned it -- so env vars set by a prior
    # step (or by an installer's registry write that the CURRENT process never
    # re-reads) are exactly as absent/present as they would be for a reader who
    # reopened PowerShell, or for Iris's autopilot spawning the next command.
    $scriptPath = [System.IO.Path]::GetTempFileName() + ".ps1"
    Set-Content -LiteralPath $scriptPath -Value $Script -Encoding UTF8
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "pwsh.exe"
        $psi.Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`""
        $psi.WorkingDirectory = $WorkingDirectory
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()
        return [PSCustomObject]@{
            ExitCode = $proc.ExitCode
            Stdout   = $stdout
            Stderr   = $stderr
        }
    } finally {
        Remove-Item -LiteralPath $scriptPath -ErrorAction SilentlyContinue
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$appDir = Join-Path $repoRoot "app"
Write-Host "repoRoot=$repoRoot"
Write-Host "appDir=$appDir"

# ── Step 0: start from a genuinely rust-less machine ────────────────────────
# windows-latest images ship a preinstalled Rust toolchain on PATH (the repo's
# OWN windows.yml relies on this -- it never installs rustup itself). A real
# first-time reader's PC has no such thing, so remove it before simulating the
# guide's own "Install Rust" step, or the PATH-gap this oracle exists to check
# is never actually created.
Write-Host "== Step 0: remove any preinstalled Rust toolchain =="
$clean = Invoke-FreshShell -WorkingDirectory $repoRoot -Script @'
if (Get-Command rustup -ErrorAction SilentlyContinue) {
  & rustup self uninstall -y 2>&1 | Out-String | Write-Host
}
Remove-Item -Recurse -Force "$env:USERPROFILE\.cargo" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:USERPROFILE\.rustup" -ErrorAction SilentlyContinue
Write-Host "cargo.exe present after cleanup: $(Test-Path (Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe'))"
'@
Write-Host $clean.Stdout
Write-Host $clean.Stderr

# ── Step 1: the guide's "Install Rust" step ─────────────────────────────────
# The guide step is `kind: open` (rustup.rs) + the reader runs the downloaded
# rustup-init.exe. rustup-init.exe -y is the documented non-interactive default
# install this open step's watch/hints describe ("take the default install").
Write-Host "== Step 1: Install Rust (guide step 'install-rust') =="
$installRust = Invoke-FreshShell -WorkingDirectory $repoRoot -Script @'
$ProgressPreference = "SilentlyContinue"
$exePath = Join-Path $env:TEMP "rustup-init.exe"
Invoke-WebRequest -Uri "https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe" -OutFile $exePath
& $exePath -y --default-toolchain stable --profile default
Write-Host "rustup-init exit: $LASTEXITCODE"
'@
Write-Host $installRust.Stdout
Write-Host $installRust.Stderr

$cargoPath = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
$cargoExists = Test-Path -LiteralPath $cargoPath
Write-Host "== Precondition: cargo.exe on disk at $cargoPath : $cargoExists =="
if (-not $cargoExists) {
    Write-Host "BUGFIX_LAB_INCONCLUSIVE: rustup install did not produce cargo.exe on disk -- cannot test the PATH-gap scenario the reporters describe"
    exit 2
}

# ── Step 1b: the guide's "Install uv" step ──────────────────────────────────
# prepare-resources.mjs (run by tauri's beforeBuildCommand) shells out to `uv`,
# so without this the package step fails for an unrelated reason (uv missing)
# before we can see whether cargo itself resolved. Verbatim guide command.
Write-Host "== Step 1b: Install uv (guide step 'install-uv') =="
$installUv = Invoke-FreshShell -WorkingDirectory $repoRoot -Script @'
winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
Write-Host "winget uv install exit: $LASTEXITCODE"
'@
Write-Host $installUv.Stdout
Write-Host $installUv.Stderr

# ── Step 2: the guide's "Install the interface packages" step ──────────────
Write-Host "== Step 2: npm install (guide step 'dependencies') =="
$npmInstall = Invoke-FreshShell -WorkingDirectory $appDir -Script @'
npm.cmd install
Write-Host "npm install exit: $LASTEXITCODE"
exit $LASTEXITCODE
'@
Write-Host $npmInstall.Stdout
Write-Host $npmInstall.Stderr
if ($npmInstall.ExitCode -ne 0) {
    Write-Host "BUGFIX_LAB_INCONCLUSIVE: npm install failed, cannot reach the build step"
    exit 2
}

# ── Step 3: the guide's "Build the installer" step, EXACT authored command ─
# Verbatim from lib/guides/publikclip.ts publikclipWindowsSteps() 'package' step.
Write-Host "== Step 3: package (guide step 'package', exact authored command, fresh shell) =="
$buildScript = @'
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
node_modules\.bin\tauri.cmd build --bundles nsis
Write-Host "tauri build exit: $LASTEXITCODE"
exit $LASTEXITCODE
'@
$build = Invoke-FreshShell -WorkingDirectory $appDir -Script $buildScript
$combined = ($build.Stdout + "`n" + $build.Stderr)
Write-Host "----- build step combined output -----"
Write-Host $combined
Write-Host "----- build step exit code: $($build.ExitCode) -----"

$needle = "failed to run command cargo metadata --no-deps --format-version 1: program not found"
if ($combined -match [regex]::Escape($needle)) {
    Write-Host "cargo.exe exists on disk ($cargoExists) but the build step's shell could not resolve it."
    Write-Host "BUGFIX_LAB_PRESENT"
    exit 1
} else {
    Write-Host "The build step's shell resolved cargo (cargo-metadata error text not found)."
    Write-Host "BUGFIX_LAB_ABSENT"
    exit 0
}
