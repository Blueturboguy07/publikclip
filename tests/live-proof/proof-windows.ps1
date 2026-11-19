<#
.SYNOPSIS
  Live update proof on a Windows GitHub runner (.github/workflows/live-update-proof.yml).
  It installs and starts publikclip and clicks in its Help menu and dialogs, so run it only on a
  disposable machine (a GitHub Actions runner), never on a computer someone is using.
  Written for Windows PowerShell 5.1 (UI Automation from .NET Framework).

  1. Downloads the installer of release -FromTag from GitHub (the asset publik's listing links),
     checks it against the release's SHA256SUMS.txt and runs it silently (/S).
  2. Starts the installed app (no test hook, no feed override: the release asks the live feed
     https://publikhq.com/api/updates/publikclip/latest.json). Sign-in uses a local stand-in for
     publik's API (fake-publik.py "claimed", own key), so no real account is used and publik's
     database is not touched; the app gets past its gate and opens its main window.
  3. -Expect = the version of -FromTag: the app must find no update; "Check for Updates..." in
     the Help menu item must answer "publikclip <version> is the latest version."
     Higher: the app must download the installer by itself and ask; the proof answers
     "Restart and Install" (the app must come back as the new version and say so through the menu),
     then installs -FromTag again and answers "Later" (the app keeps running on the old version,
     and the menu offers the update again).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tests\live-proof\proof-windows.ps1 -FromTag v0.2.2 -Expect 0.2.2 -Out out
#>
param(
  [Parameter(Mandatory = $true)][string]$FromTag,
  [Parameter(Mandatory = $true)][string]$Expect,
  [Parameter(Mandatory = $true)][string]$Out
)
$ErrorActionPreference = "Stop"
$from = $FromTag -replace '^v', ''
$want = $Expect -replace '^v', ''
if ($from -notmatch '^\d+\.\d+\.\d+$' -or $want -notmatch '^\d+\.\d+\.\d+$') { throw "FromTag must be vX.Y.Z and Expect X.Y.Z" }
$mode = if ($from -eq $want) { "no-update" } else { "update" }
$repo = $env:GITHUB_REPOSITORY
$feed = "https://publikhq.com/api/updates/publikclip/latest.json"
$here = $PSScriptRoot
New-Item -ItemType Directory -Force $Out | Out-Null
$Out = (Resolve-Path $Out).Path
$work = Join-Path $env:RUNNER_TEMP "live-proof"
$dl = Join-Path $work "dl"
New-Item -ItemType Directory -Force $dl | Out-Null
$port = 47333

$script:pass = 0; $script:fails = 0; $script:shots = 0
$results = Join-Path $Out "results.md"
"| Check | Result | Detail |`n|---|---|---|" | Set-Content $results -Encoding UTF8
function Row([string]$a, [string]$b, [string]$c) { "| $a | $b | $(($c -replace '[|\r\n]', ' '))|" | Add-Content $results -Encoding UTF8 }
function Check([string]$name, [bool]$ok, [string]$detail = "") {
  if ($ok) { $script:pass++; Write-Host "PASS  $name ($detail)"; Row $name "PASS" $detail }
  else { $script:fails++; Write-Host "FAIL  $name ($detail)"; Row $name "**FAIL**" $detail }
}
function Note([string]$name, [string]$detail = "") { Write-Host "NOTE  $name ($detail)"; Row $name "note" $detail }
function Log([string]$m) { Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $m) }

Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class Mouse {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, int x, int y, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public static void Click(int x, int y) {
    SetCursorPos(x, y); System.Threading.Thread.Sleep(150);
    mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero); System.Threading.Thread.Sleep(60);
    mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
  }
}
"@
[void][Mouse]::SetProcessDPIAware()
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$root = $AE::RootElement
function Cond($prop, $value) { New-Object System.Windows.Automation.PropertyCondition($prop, $value) }

function Shot([string]$name) {
  try {
    $script:shots++
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
    $bmp.Save((Join-Path $Out ("{0:D2}-{1}.png" -f $script:shots, $name)), [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
  } catch { Write-Host "screenshot $name failed: $_" }
}
function Click-Center($el) { $r = $el.Current.BoundingRectangle; [Mouse]::Click([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2)) }
function Installed-Exe { Get-ChildItem (Join-Path $env:LOCALAPPDATA "publikclip") -Filter *.exe -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike "uninstall*" } | Select-Object -First 1 }
function Disk-Version { $e = Installed-Exe; if ($e) { return $e.VersionInfo.ProductVersion } else { return "" } }
function Reg-Version { $r = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "publikclip*" } | Select-Object -First 1; if ($r) { return $r.DisplayVersion } else { return "" } }
function App-Procs { $d = Join-Path $env:LOCALAPPDATA "publikclip"; @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$d\*" -and $_.Name -notlike "uninstall*" -and $_.Name -notlike "*installer*" }) }
function Proc-Version($p) { try { return $p.MainModule.FileVersionInfo.ProductVersion } catch { return "" } }
function Installer-Procs { @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "publikclip*installer*" -or $_.Name -like "publikclip_*setup*" }) }
function Updater-Dirs { @(Get-ChildItem $env:TEMP -Directory -Filter "publikclip-*-updater-*" -ErrorAction SilentlyContinue) }
function Stop-App { App-Procs | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep 2 }
function Wait-Until([int]$seconds, [scriptblock]$cond) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) { if (& $cond) { return $true }; Start-Sleep -Milliseconds 500 }
  return [bool](& $cond)
}
# Reads a log the app still has open for writing.
function Read-Shared([string]$file) {
  if (-not (Test-Path $file)) { return "" }
  try {
    $fs = [System.IO.File]::Open($file, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    $r = New-Object System.IO.StreamReader($fs)
    $t = $r.ReadToEnd(); $r.Close(); return $t
  } catch { return "" }
}
function Log-Has([string]$file, [string]$text) { (Read-Shared $file).Contains($text) }
function Proc-ById([int]$id) { Get-Process -Id $id -ErrorAction SilentlyContinue }

# ---- The app's dialogs and its tray menu, through UI Automation ---------------------------------
function Find-Dialog([int]$seconds = 30) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    $ids = @(App-Procs | ForEach-Object { $_.Id })
    foreach ($w in $root.FindAll($TS::Children, (Cond $AE::NameProperty "publikclip update"))) {
      if ($ids -contains $w.Current.ProcessId) { return $w }
    }
    Start-Sleep -Milliseconds 500
  }
  return $null
}
function Dialog-Text($w) {
  $names = foreach ($e in $w.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) { $e.Current.Name }
  return (($names | Where-Object { $_ }) -join " | ")
}
function Press($w, [string]$name) {
  $b = $null
  foreach ($e in $w.FindAll($TS::Descendants, (Cond $AE::ControlTypeProperty $CT::Button))) { if ($e.Current.Name -eq $name) { $b = $e; break } }
  if (-not $b) { return $false }
  try { $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch { Click-Center $b }
  return $true
}
$trayClasses = @("Shell_TrayWnd", "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland")
function Tray-Hosts { foreach ($w in $root.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)) { if ($trayClasses -contains $w.Current.ClassName) { $w } } }
function Tray-Buttons { foreach ($h in @(Tray-Hosts)) { foreach ($b in $h.FindAll($TS::Descendants, (Cond $AE::ControlTypeProperty $CT::Button))) { $b } } }
function Find-TrayIcon { foreach ($b in @(Tray-Buttons)) { if ($b.Current.Name -like "*publikclip*" -and -not $b.Current.IsOffscreen) { return $b } }; return $null }
# Clicks publikclip's tray icon (opening "Show hidden icons" first when it is hidden there), then the
# menu item. Returns the result text for the results table, or $null.
# publikclip on Windows has no tray: "Check for Updates…" is in the Help menu of the main window
# (app/src-tauri/src/updates.rs). Opens Help in that window's menu bar and clicks the item.
# Returns the result text for the results table, or $null.
function Tray-Menu([string]$item) {
  for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $wins = @()
    foreach ($p in @(App-Procs)) {
      foreach ($w in $root.FindAll($TS::Children, (Cond $AE::ProcessIdProperty $p.Id))) { $wins += $w }
    }
    ($wins | ForEach-Object { "$($_.Current.ClassName) '$($_.Current.Name)'" }) | Set-Content (Join-Path $Out "app-windows.txt")
    foreach ($w in $wins) {
      $help = $null
      foreach ($m in $w.FindAll($TS::Descendants, (Cond $AE::ControlTypeProperty $CT::MenuItem))) { if ($m.Current.Name -eq "Help") { $help = $m; break } }
      if (-not $help) { continue }
      try { $w.SetFocus() } catch {}
      try { $help.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand() } catch { Click-Center $help }
      Start-Sleep -Milliseconds 800
      Shot "help-menu"
      $cands = @()
      foreach ($m in $root.FindAll($TS::Children, (Cond $AE::ClassNameProperty "#32768"))) { $cands += $m.FindAll($TS::Descendants, (Cond $AE::ControlTypeProperty $CT::MenuItem)) }
      $cands += $w.FindAll($TS::Descendants, (Cond $AE::ControlTypeProperty $CT::MenuItem))
      ($cands | ForEach-Object { "'$($_.Current.Name)'" }) | Set-Content (Join-Path $Out "help-menu-items.txt")
      foreach ($e in $cands) {
        if ($e.Current.Name -like "$item*") {
          $name = $e.Current.Name
          try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch { Click-Center $e }
          return "window '$($w.Current.Name)': Help, then '$name'"
        }
      }
    }
    # The menu bar is not in the accessibility tree of this window: click "Help" by its place (the
    # first and only menu, at the left of the bar under the title bar), then the one item of the
    # popup menu that opens.
    foreach ($w in $wins) {
      if ($w.Current.Name -ne "publikclip" -or $w.Current.ClassName -ne "Tauri Window") { continue }
      try { $w.SetFocus() } catch {}
      $r = $w.Current.BoundingRectangle
      [Mouse]::Click([int]($r.X + 28), [int]($r.Y + 41))
      Start-Sleep -Milliseconds 900
      foreach ($m in $root.FindAll($TS::Children, (Cond $AE::ClassNameProperty "#32768"))) {
        if ($m.Current.IsOffscreen -or $m.Current.BoundingRectangle.Width -le 0) { continue }
        Shot "help-menu"
        $mr = $m.Current.BoundingRectangle
        $names = @($m.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name })
        [Mouse]::Click([int]($mr.X + $mr.Width / 2), [int]($mr.Y + $mr.Height / 2))
        return "clicked Help at the left of the menu bar, then the one item of its menu (menu $([int]$mr.Width)x$([int]$mr.Height); names in the tree: '$($names -join "', '")')"
      }
    }
    Start-Sleep 2
  }
  return $null
}
$trayItems = @()

Log "publikclip live update proof: install $FromTag, expect $want ($mode)"
[System.Environment]::OSVersion.VersionString | Set-Content (Join-Path $Out "run.txt")
"from=$FromTag expect=$want mode=$mode" | Add-Content (Join-Path $Out "run.txt")
Invoke-WebRequest $feed -UseBasicParsing -OutFile (Join-Path $Out "feed-at-start.json")
$feedVersion = (Get-Content (Join-Path $Out "feed-at-start.json") -Raw | ConvertFrom-Json).version
Check "the live feed announces $want" ($feedVersion -eq $want) "$feed version $feedVersion"

# ---- 1. Download and install --------------------------------------------------------------------
$name = "publikclip_${from}_x64-setup.exe"
$installer = Join-Path $dl $name
$url = "https://github.com/$repo/releases/download/$FromTag/$name"
Log "download $url"
Invoke-WebRequest $url -UseBasicParsing -OutFile $installer
Invoke-WebRequest "https://github.com/$repo/releases/download/$FromTag/SHA256SUMS.txt" -UseBasicParsing -OutFile (Join-Path $dl "SHA256SUMS.txt")
$hash = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
Check "the installer is the release's (SHA256SUMS.txt)" ((Get-Content (Join-Path $dl "SHA256SUMS.txt")) -contains "$hash  $name") $hash.Substring(0, 16)
Check "the installer carries version $from" ((Get-Item $installer).VersionInfo.ProductVersion -eq $from) (Get-Item $installer).VersionInfo.ProductVersion
Note "installer" "the release's installer in its silent mode (/S): the same install as a user's, without clicking through its pages; it is not code-signed, so a browser download gets SmartScreen's 'Run anyway' once"
function Install-From {
  Stop-App
  $un = Join-Path $env:LOCALAPPDATA "publikclip\uninstall.exe"
  if (Test-Path $un) { Start-Process $un -ArgumentList "/S" -Wait; Start-Sleep 3 }
  $p = Start-Process -FilePath $installer -ArgumentList "/S" -PassThru -Wait
  Start-Sleep 2
  Stop-App
  return $p.ExitCode
}
$rc = Install-From
Check "publikclip $from installs silently" ($rc -eq 0) "exit $rc"
Check "the installed exe is $from" ((Disk-Version) -eq $from) "$((Installed-Exe).FullName) $(Disk-Version)"
Check "the uninstall entry says $from" ((Reg-Version) -eq $from) (Reg-Version)
$fromHash = (Get-FileHash (Installed-Exe).FullName).Hash

# ---- 2. Start the app ---------------------------------------------------------------------------
$fake = Start-Process -FilePath python -ArgumentList "`"$here\fake-publik.py`"", $port, "claimed" -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Out "fake-publik.log") -RedirectStandardError (Join-Path $Out "fake-publik.err")
$apps = Join-Path $env:LOCALAPPDATA "publik\apps"
New-Item -ItemType Directory -Force $apps | Out-Null
'{"key":"pk_ci_fake","base_url":"http://127.0.0.1:' + $port + '/api/v1","install_id":"00000000-0000-4000-8000-000000000000","claim_url":"https://publikhq.com/claim/CI","choice":"own_key"}' |
  Set-Content (Join-Path $apps "publikclip.json") -Encoding ASCII
$env:PUBLIK_ORIGIN = "http://127.0.0.1:$port"
$env:DO_NOT_TRACK = "1"
$env:RUST_LOG = "info,tauri_plugin_updater=debug"
Wait-Until 20 { try { Invoke-WebRequest "http://127.0.0.1:$port/api/v1/wallet" -UseBasicParsing | Out-Null; $true } catch { $false } } | Out-Null
Note "sign-in" "PUBLIK_ORIGIN is a local stand-in that answers 'claimed' (own key): no real account, no write to publik"
function Start-publikclip([string]$log) {
  $exe = (Installed-Exe).FullName
  return Start-Process -FilePath $exe -PassThru -RedirectStandardError $log -RedirectStandardOutput "$log.stdout"
}
$log1 = Join-Path $Out "app-1.log"
$app = Start-publikclip $log1
Log "started $((Installed-Exe).FullName) pid $($app.Id)"
# The Windows build writes no request line; its updater line is the answer from the feed compiled
# into the app (the live feed above; no override is set in this job).
Check "the updater answers from the live feed" (Wait-Until 180 { Log-Has $log1 "updates: " }) $feed

if ($mode -eq "no-update") {
  Check "the app reports '$from is the latest version'" (Wait-Until 120 { Log-Has $log1 "updates: $from is the latest version" }) ""
  Start-Sleep 20
  Shot "no-update"
  Check "no update was offered" (-not (Log-Has $log1 "updates: publikclip ") -and -not (Find-Dialog 1)) ""
  Check "the app keeps running (same process)" (-not $app.HasExited) "pid $($app.Id)"
  Check "the running process is $from (file version of its image)" ((Proc-Version (Proc-ById $app.Id)) -eq $from) (Proc-Version (Proc-ById $app.Id))
  Check "the exe on disk is unchanged" ((Get-FileHash (Installed-Exe).FullName).Hash -eq $fromHash -and (Disk-Version) -eq $from) (Disk-Version)
  Check "no updater download or installer" ((Updater-Dirs).Count -eq 0 -and (Installer-Procs).Count -eq 0) "temp dirs: $((Updater-Dirs).Count)"
  $how = Tray-Menu "Check for Updates"
  Check "Help menu: 'Check for Updates...' clicked" ($null -ne $how) "$how"
  $d = Find-Dialog 60
  $text = if ($d) { Dialog-Text $d } else { "" }
  Shot "menu-answer"
  Check "the menu's answer: 'publikclip $from is the latest version.'" ($text -like "*publikclip $from is the latest version.*") $text
  if ($d) { [void](Press $d "OK") }
  Note "variants Restart and Later" "not applicable: the feed offers no newer version"
} else {
  # Variant A: "Restart and Install".
  Check "[Restart] the updater downloads and verifies $want by itself" (Wait-Until 300 { Log-Has $log1 "updates: publikclip $want is ready" }) ""
  $d = Find-Dialog 60
  $text = if ($d) { Dialog-Text $d } else { "" }
  Shot "restart-question"
  Check "[Restart] the question is on screen: 'publikclip $want is ready...'" ($text -like "*publikclip $want is ready*") $text
  Check "[Restart] nothing is installed before the answer" ((Disk-Version) -eq $from) (Disk-Version)
  $dirs = @(Updater-Dirs)
  $oldPid = $app.Id
  Check "[Restart] answered 'Restart and Install'" ($d -and (Press $d "Restart and Install")) ""
  $sawInstaller = Wait-Until 60 { (Installer-Procs).Count -gt 0 -or (Disk-Version) -eq $want }
  $dirs += @(Updater-Dirs)
  Check "[Restart] the old process exits" (Wait-Until 60 { $app.HasExited }) "pid $oldPid"
  Check "[Restart] the installer from the updater ran" ($sawInstaller) "$(($dirs | ForEach-Object Name | Select-Object -Unique) -join ', ')"
  Check "[Restart] the exe on disk is $want" (Wait-Until 180 { (Disk-Version) -eq $want -and (Installer-Procs).Count -eq 0 }) (Disk-Version)
  Check "[Restart] the uninstall entry says $want" ((Reg-Version) -eq $want) (Reg-Version)
  Check "[Restart] publikclip runs again (the installer opened it)" (Wait-Until 60 { @(App-Procs | Where-Object { $_.Id -ne $oldPid }).Count -ge 1 }) "$((App-Procs | ForEach-Object { "$($_.Id) $($_.Path)" }) -join '; ')"
  $new = @(App-Procs | Where-Object { $_.Id -ne $oldPid }) | Select-Object -First 1
  Check "[Restart] the running process is $want (file version of its image)" ($new -and (Proc-Version $new) -eq $want) "$(if ($new) { Proc-Version $new })"
  Shot "restarted"
  Start-Sleep 15
  $how = Tray-Menu "Check for Updates"
  Check "[Restart] Help menu: 'Check for Updates...' clicked" ($null -ne $how) "$how"
  $d = Find-Dialog 60
  $text = if ($d) { Dialog-Text $d } else { "" }
  Shot "restarted-menu-answer"
  Check "[Restart] the running app answers 'publikclip $want is the latest version.'" ($text -like "*publikclip $want is the latest version.*") $text
  if ($d) { [void](Press $d "OK") }
  $tempInstaller = @($dirs | ForEach-Object { Get-ChildItem $_.FullName -Filter *.exe -ErrorAction SilentlyContinue }) | Select-Object -First 1
  if ($tempInstaller) {
    $relHash = $null
    try {
      Invoke-WebRequest "https://github.com/$repo/releases/download/v$want/SHA256SUMS.txt" -UseBasicParsing -OutFile (Join-Path $work "SHA256SUMS-new.txt")
      $relHash = ((Get-Content (Join-Path $work "SHA256SUMS-new.txt")) | Where-Object { $_ -like "*  publikclip_${want}_x64-setup.exe" }) -replace '\s.*$', ''
    } catch { }
    $got = (Get-FileHash $tempInstaller.FullName -Algorithm SHA256).Hash.ToLower()
    Check "[Restart] the updater's installer is the $want release's, byte for byte" ($got -eq $relHash) "$($tempInstaller.Name) $($got.Substring(0, 16))"
  } else {
    Note "[Restart] the updater's installer file" "the installer removed its temp folder; its run is shown by the process and the app log"
  }

  # Variant B: "Later", from a fresh install of the old version.
  Log "variant Later: install $from again"
  $rc = Install-From
  Check "[Later] publikclip $from installed again" ($rc -eq 0 -and (Disk-Version) -eq $from) "exit $rc, $(Disk-Version)"
  $log2 = Join-Path $Out "app-2.log"
  $app = Start-publikclip $log2
  Check "[Later] the updater downloads $want by itself" (Wait-Until 300 { Log-Has $log2 "updates: publikclip $want is ready" }) ""
  $d = Find-Dialog 60
  Shot "later-question"
  Check "[Later] answered 'Later'" ($d -and (Press $d "Later")) ""
  Start-Sleep 15
  Check "[Later] the app keeps running (same process)" (-not $app.HasExited) "pid $($app.Id)"
  Check "[Later] nothing is installed (Windows installs on the answer only)" ((Disk-Version) -eq $from -and (Installer-Procs).Count -eq 0) (Disk-Version)
  Check "[Later] the running process is still $from" ((Proc-Version (Proc-ById $app.Id)) -eq $from) (Proc-Version (Proc-ById $app.Id))
  $how = Tray-Menu "Check for Updates"
  Check "[Later] Help menu: 'Check for Updates...' clicked" ($null -ne $how) "$how"
  $d = Find-Dialog 60
  $text = if ($d) { Dialog-Text $d } else { "" }
  Shot "later-menu-answer"
  Check "[Later] the menu offers the downloaded update again: 'publikclip $want is ready'" ($text -like "*publikclip $want is ready*") $text
  if ($d) { [void](Press $d "Later") }
}

# ---- What the app logged about the feed, and what this job downloaded ---------------------------
$lines = foreach ($f in Get-ChildItem $Out -Filter "app-*.log") {
  "== $($f.Name)"
  (Read-Shared $f.FullName) -split "`n" | Where-Object { $_ -match "checking for updates|update response|parsed release|updates:|Executing updater" } | ForEach-Object { if ($_.Length -gt 400) { $_.Substring(0, 400) } else { $_ } }
}
$lines | Set-Content (Join-Path $Out "updater-lines.txt")
$lines | ForEach-Object { Write-Host $_ }
$downloads = (Get-ChildItem $dl | ForEach-Object Name | Sort-Object) -join " "
$expected = (@($name, "SHA256SUMS.txt") | Sort-Object) -join " "
Check "downloads by this job before the check: only the $FromTag installer and its checksums" ($downloads -eq $expected) $downloads

Stop-App
Stop-Process -Id $fake.Id -Force -ErrorAction SilentlyContinue
Shot "end"
Write-Host ""
Write-Host "$($script:pass) passed, $($script:fails) failed"
if ($env:GITHUB_STEP_SUMMARY) {
  "### Windows live update proof: $FromTag -> $want ($mode): $($script:pass) passed, $($script:fails) failed`n" | Add-Content $env:GITHUB_STEP_SUMMARY
  Get-Content $results | Add-Content $env:GITHUB_STEP_SUMMARY
}
if ($script:fails -gt 0) { exit 1 }
exit 0
