# bugfix-lab CONTROL (attempt 2) for cluster publikclip-windows-build-cargo-not-found
# -- publikclip half.
#
# Same session model as the plantgpt oracle in this cluster: the LIVE publikclip Windows
# guide (https://publikhq.com/api/iris/guides/publikclip, version 7, sourceCommit
# a53a359b985b1d2d666266062936cc186f02340b) run as a web reader runs it -- ONE continuous
# PowerShell window, opened at guide step 3, never reopened -- on a machine first stripped
# of the CI image's preinstalled Rust toolchain.
#
# Counting the live branch's 2 setupSteps + 13 steps as 1..15 puts the tauri build at
# position 11, which is the "step 11" both publikclip reporters name.
#
# The difference from plantgpt, and the reason this run is the CONTROL: publikclip's
# step 11 command carries a PATH insurance line,
#   $env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
# and plantgpt's step 8 carries none. If the single-session model is what reproduces the
# reporters' error, this run should come back ABSENT while plantgpt's comes back PRESENT.
#
# Exit 1 / 0 / 2 and the markers mean exactly what they mean in the plantgpt script.

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Host "[control] $m" }

function Run-Bounded {
    param([string]$File, [string[]]$ArgList, [int]$TimeoutSeconds, [string]$Tag)
    $out = Join-Path $env:TEMP "$Tag.out.txt"
    $err = Join-Path $env:TEMP "$Tag.err.txt"
    Remove-Item -LiteralPath $out, $err -ErrorAction SilentlyContinue
    try {
        $p = Start-Process -FilePath $File -ArgumentList $ArgList -PassThru -NoNewWindow `
            -RedirectStandardOutput $out -RedirectStandardError $err
    } catch {
        return [PSCustomObject]@{ TimedOut = $false; ExitCode = -2; Output = "could not start ${File}: $_" }
    }
    $done = $p.WaitForExit($TimeoutSeconds * 1000)
    if (-not $done) {
        try { & taskkill /pid $p.Id /T /F 2>&1 | Out-Null } catch {}
        Start-Sleep -Seconds 2
    }
    $stdout = if (Test-Path $out) { Get-Content -Raw -LiteralPath $out } else { "" }
    $stderr = if (Test-Path $err) { Get-Content -Raw -LiteralPath $err } else { "" }
    [PSCustomObject]@{
        TimedOut = (-not $done)
        ExitCode = $(if ($done) { $p.ExitCode } else { -1 })
        Output   = "$stdout`n$stderr"
    }
}

$cargoExe = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"

Say "=== PHASE 0: strip the runner image's preinstalled Rust toolchain ==="
if (Get-Command rustup -ErrorAction SilentlyContinue) {
    & rustup self uninstall -y 2>&1 | Out-String | Write-Host
}
Remove-Item -Recurse -Force "$env:USERPROFILE\.cargo" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:USERPROFILE\.rustup" -ErrorAction SilentlyContinue

$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = "$machinePath;$userPath"
$before = $env:Path
$env:Path = (($env:Path -split ';') | Where-Object { $_ -and ($_ -notlike '*\.cargo\bin*') }) -join ';'
if ($before -ne $env:Path) { Say "removed a leftover .cargo\bin entry the CI image had in the registry PATH" }
Say "session PATH (as a freshly opened PowerShell would see it):"
Write-Host $env:Path

if ((Test-Path -LiteralPath $cargoExe) -or (Get-Command cargo -ErrorAction SilentlyContinue)) {
    Say "BUGFIX_LAB_INCONCLUSIVE: could not return the machine to a rust-less state"
    exit 2
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue) -or -not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    Say "BUGFIX_LAB_INCONCLUSIVE: node/git are not in the registry PATH"
    exit 2
}

# --- guide step 3: Open PowerShell. THIS process is that window. -----------
Say "=== GUIDE STEP 3: the reader's window (never reopened) ==="

# --- guide step 4: Install Rust (kind: open -> rustup.rs; body says "Download
#     rustup-init.exe, run it"). Run from inside the already-open window, the way a
#     reader who downloaded it does. windows-latest already satisfies step 5 (the
#     C++ build tools), so rustup's MSVC prerequisite menu does not appear and -y is
#     the "default install" the step's own hint names.
Say "=== GUIDE STEP 4: rustup-init.exe (default install) ==="
$ProgressPreference = "SilentlyContinue"
$initExe = Join-Path $env:TEMP "rustup-init.exe"
Invoke-WebRequest -Uri "https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe" -OutFile $initExe
$ri = Run-Bounded -File $initExe -ArgList @('-y', '--default-toolchain', 'stable', '--profile', 'default') -TimeoutSeconds 420 -Tag "step4-rustup-init"
Write-Host $ri.Output
Say "rustup-init exit: $($ri.ExitCode)"
$cargoOnDisk = Test-Path -LiteralPath $cargoExe
Say "PRECONDITION -- cargo.exe on disk: $cargoOnDisk"
if (-not $cargoOnDisk) { Say "BUGFIX_LAB_INCONCLUSIVE: step 4 produced no cargo.exe"; exit 2 }
Say "cargo resolvable in THIS still-open session after step 4: $([bool](Get-Command cargo -ErrorAction SilentlyContinue))"

# --- guide step 6: Install uv (verbatim; bounded and non-fatal) -------------
Say "=== GUIDE STEP 6: winget install --id astral-sh.uv -e ... ==="
$uv = Run-Bounded -File "winget.exe" -ArgList @('install', '--id', 'astral-sh.uv', '-e', '--accept-source-agreements', '--accept-package-agreements') -TimeoutSeconds 420 -Tag "step6-uv"
Write-Host $uv.Output
Say "winget uv exit: $($uv.ExitCode) timedOut: $($uv.TimedOut)"

# --- guide steps 7-9: clone, enter, pin (verbatim, inline) -----------------
Say "=== GUIDE STEPS 7-9: clone / cd / pin ==="
cd ~
if (-not (Test-Path publikclip/.git)) {
    git clone https://github.com/Blueturboguy07/publikclip.git
}
cd publikclip
$origin = git config --get remote.origin.url 2>$null
if ($origin) { $origin = $origin -replace '^git@github\.com:', 'https://github.com/' -replace '\.git$', '' }
$dirty = git status --porcelain 2>$null
if ($origin -ne "https://github.com/Blueturboguy07/publikclip" -or $dirty) {
    Say "BUGFIX_LAB_INCONCLUSIVE: clone guard tripped (origin=$origin)"
    exit 2
}
git checkout a53a359b985b1d2d666266062936cc186f02340b
Say "HEAD: $(git rev-parse HEAD)"

# --- guide step 10: dependencies -------------------------------------------
Say "=== GUIDE STEP 10: cd app ; npm.cmd install ==="
cd app
$npm = Run-Bounded -File "npm.cmd" -ArgList @('install') -TimeoutSeconds 600 -Tag "step10-npm"
Write-Host $npm.Output
Say "npm install exit: $($npm.ExitCode)"
if ($npm.ExitCode -ne 0) { Say "BUGFIX_LAB_INCONCLUSIVE: npm install failed"; exit 2 }

# --- guide step 11: package, VERBATIM (insurance line + tauri build) -------
Say "=== GUIDE STEP 11: package (verbatim, same window) ==="
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
$build = Run-Bounded -File "node_modules\.bin\tauri.cmd" -ArgList @('build', '--bundles', 'nsis') -TimeoutSeconds 600 -Tag "step11-tauri"
Write-Host "----- step 11 output -----"
Write-Host $build.Output
Write-Host "----- exit: $($build.ExitCode) timedOut: $($build.TimedOut) -----"

$needle = "failed to run command cargo metadata --no-deps --format-version 1: program not found"
$hit = $build.Output -match [regex]::Escape($needle)
$cargoStillOnDisk = Test-Path -LiteralPath $cargoExe
Say "reporters' error text present : $hit"
Say "cargo.exe on disk at verdict  : $cargoStillOnDisk"

if ($hit -and $cargoStillOnDisk) { Say "BUGFIX_LAB_PRESENT"; exit 1 }
elseif ($hit) { Say "BUGFIX_LAB_INCONCLUSIVE: error text but no cargo.exe on disk"; exit 2 }
else { Say "BUGFIX_LAB_ABSENT"; exit 0 }
