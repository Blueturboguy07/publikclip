<#
  Runs the NSIS installer `tauri build --bundles nsis` just produced,
  silently (/S), and waits for it to finish.

  This is the Windows install guide's "Run the installer" step
  (publikhq.com/publikclip/install/windows), as a script file instead of
  inline PowerShell pasted straight into whatever shell is open.

  Why a file: the guide's older text was three bare PowerShell lines
  ($var assignments + Start-Process). Reported bug
  (publikclip-windows-installer-command-not-recognized): a reader who
  reopens a terminal mid-guide -- the guide itself says to, more than once,
  after installing Rust and after installing uv -- commonly lands back in
  Command Prompt, not PowerShell. cmd.exe has no `$variable` syntax, so it
  rejected every line: "'$setup' is not recognized as an internal or
  external command...", then the same for Start-Process. A single inline
  `powershell -Command "$setup = ...; ..."` line does not fix this: tested
  against real Windows CI (windows-latest), that shape only works from
  cmd.exe -- typed inside an ALREADY-OPEN real PowerShell window, PowerShell
  itself interpolates the double-quoted string's `$setup`/`$installer`
  tokens (both undefined at that point) *before* handing it to the nested
  process, corrupting the command and breaking the common, previously-working
  case. Invoking a script FILE has no `$` tokens in the line the reader
  types at all, so there is nothing for either shell to corrupt:
  `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install-app.ps1`
  runs identically whether typed into cmd.exe or PowerShell (verified both
  ways on windows-latest). `-ExecutionPolicy Bypass` is a per-process
  override, not `Set-ExecutionPolicy` -- it changes nothing persistent, and
  only covers the default "Restricted" policy some freshly-imaged Windows
  machines ship with blocking an unsigned local script from running at all.

  Relative path below is intentional: the guide's workingDirectory for this
  step is ~/publikclip/app, and this script is invoked with that as the
  current directory (PowerShell's -File does not change $PWD to the
  script's own folder), matching what the three inline lines did before.
#>
$ErrorActionPreference = "Stop"

$setup = Get-ChildItem src-tauri\target\release\bundle\nsis -Filter *-setup.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $setup) {
    Write-Error "No *-setup.exe found under src-tauri\target\release\bundle\nsis -- run the build step first."
    exit 1
}

Write-Host "Running $($setup.Name) silently..."
$installer = Start-Process -FilePath $setup.FullName -ArgumentList '/S' -PassThru
$installer.WaitForExit()
exit $installer.ExitCode
