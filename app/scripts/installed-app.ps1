# Installs or uninstalls the Windows build the way a user would, and checks the result (release workflow).
#
#   pwsh scripts/installed-app.ps1 install     # silent install of release/Syncora-Setup-*.exe
#   pwsh scripts/installed-app.ps1 uninstall   # silent uninstall through the Apps & features entry
#
# "install" writes MCSYNC_E2E_APP (the installed Syncora.exe) to $GITHUB_ENV when it is set.
param([Parameter(Mandatory)][ValidateSet("install", "uninstall")][string]$Action)
$ErrorActionPreference = "Stop"
$version = (Get-Content (Join-Path $PSScriptRoot "..\package.json") | ConvertFrom-Json).version
$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Syncora.lnk"

function Get-Entry {
  Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like "Syncora*" }
}

function Get-Uninstaller($entry) {
  if ($entry.QuietUninstallString -notmatch '^"([^"]+)"\s*(.*)$') {
    throw "unexpected QuietUninstallString: $($entry.QuietUninstallString)"
  }
  return @{ Path = $Matches[1]; Arguments = $Matches[2] }
}

if ($Action -eq "install") {
  $setup = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\release")) "Syncora-Setup-$version.exe"
  if (-not (Test-Path $setup)) { throw "no installer at $setup" }
  Write-Host "Installing $setup"
  $p = Start-Process -FilePath $setup -ArgumentList "/S" -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "the installer exited with $($p.ExitCode)" }

  $entry = Get-Entry
  if (-not $entry) { throw "no Apps & features entry for Syncora" }
  if ($entry.DisplayVersion -ne $version) { throw "installed version $($entry.DisplayVersion), expected $version" }
  $dir = Split-Path (Get-Uninstaller $entry).Path
  $exe = Join-Path $dir "Syncora.exe"
  foreach ($f in @($exe, (Join-Path $dir "resources\engine"), (Join-Path $dir "resources\ffmpeg"))) {
    if (-not (Test-Path $f)) { throw "missing after install: $f" }
  }
  if (-not (Test-Path $shortcut)) { throw "no desktop shortcut at $shortcut" }
  Write-Host "Installed $($entry.DisplayName) $($entry.DisplayVersion) in $dir"
  if ($env:GITHUB_ENV) { "MCSYNC_E2E_APP=$exe" | Out-File -Append -Encoding utf8 $env:GITHUB_ENV }
}
else {
  $entry = Get-Entry
  if (-not $entry) { throw "Syncora is not installed" }
  $un = Get-Uninstaller $entry
  $dir = Split-Path $un.Path
  Write-Host "Uninstalling from $dir"
  Start-Process -FilePath $un.Path -ArgumentList $un.Arguments -Wait
  # The uninstaller runs again from a temporary copy of itself: wait until it has finished.
  for ($i = 0; $i -lt 180 -and ((Test-Path (Join-Path $dir "Syncora.exe")) -or (Get-Entry)); $i++) {
    Start-Sleep -Seconds 1
  }
  if (Test-Path (Join-Path $dir "Syncora.exe")) { throw "Syncora.exe is still installed" }
  if (Get-Entry) { throw "the Apps & features entry is still there" }
  if (Test-Path $shortcut) { throw "the desktop shortcut was left behind" }
  Write-Host "Uninstalled"
}
