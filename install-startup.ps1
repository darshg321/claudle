<#
.SYNOPSIS
    Register Claudle as a Windows startup app — disabled by default.

.DESCRIPTION
    Drops a shortcut in the per-user Startup folder and marks it disabled in
    Explorer's StartupApproved registry key, so the bot shows up under Task
    Manager > Startup apps as "Disabled" and never launches until you flip it
    to Enabled there. No admin rights, nothing machine-wide.

    Task Manager's toggle writes the same registry value this script does, so
    the two stay in sync in both directions.

.PARAMETER Action
    Install   Create the shortcut, leave it disabled (default).
    Enable    Turn the entry on, same as Task Manager's Enable.
    Disable   Turn it off again.
    Status    Report what is currently installed and its state.
    Uninstall Remove the shortcut and the registry entry.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-startup.ps1
    powershell -ExecutionPolicy Bypass -File install-startup.ps1 -Action Status
#>

[CmdletBinding()]
param(
    [ValidateSet('Install', 'Enable', 'Disable', 'Status', 'Uninstall')]
    [string]$Action = 'Install'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$LinkName = 'Claudle.lnk'
$StartupDir = [Environment]::GetFolderPath('Startup')
$LinkPath = Join-Path $StartupDir $LinkName
$ApprovedKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder'

# Explorer stores state as 12 bytes: [0] is 0x02 enabled / 0x03 disabled, then
# three pad bytes, then a FILETIME of the last change (zeroed when enabling).
function New-ApprovalBytes {
    param([bool]$Enabled)

    $bytes = New-Object byte[] 12
    $bytes[0] = if ($Enabled) { 0x02 } else { 0x03 }
    if (-not $Enabled) {
        [BitConverter]::GetBytes([DateTime]::UtcNow.ToFileTimeUtc()).CopyTo($bytes, 4)
    }
    return $bytes
}

function Set-Approval {
    param([bool]$Enabled)

    if (-not (Test-Path $ApprovedKey)) {
        New-Item -Path $ApprovedKey -Force | Out-Null
    }
    New-ItemProperty -Path $ApprovedKey -Name $LinkName -PropertyType Binary `
        -Value (New-ApprovalBytes -Enabled:$Enabled) -Force | Out-Null
}

function Get-Approval {
    # Absent means enabled: Explorer only writes here once something has been
    # toggled, so a missing value is not the same as "off".
    $item = Get-ItemProperty -Path $ApprovedKey -Name $LinkName -ErrorAction SilentlyContinue
    if (-not $item) { return 'Enabled (no registry entry yet)' }
    if ($item.$LinkName[0] -eq 0x03) { return 'Disabled' }
    return 'Enabled'
}

function Resolve-Pythonw {
    $candidates = @()

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        # Ask the interpreter itself — a PATH hit can be the Store shim.
        $real = & $python.Source -c 'import sys; print(sys.executable)' 2>$null
        if ($real) { $candidates += (Join-Path (Split-Path -Parent $real) 'pythonw.exe') }
        $candidates += (Join-Path (Split-Path -Parent $python.Source) 'pythonw.exe')
    }

    $pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
    if ($pyw) { $candidates += $pyw.Source }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return (Resolve-Path $candidate).Path }
    }

    throw "Could not find pythonw.exe. Make sure Python is installed and on PATH."
}

function Invoke-Install {
    $launcher = Join-Path $Here 'startup.pyw'
    if (-not (Test-Path $launcher)) {
        throw "startup.pyw is missing from $Here."
    }
    $pythonw = Resolve-Pythonw

    # Disable first, so the entry can never fire in the window between the
    # shortcut appearing and the state being written.
    Set-Approval -Enabled:$false

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($LinkPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '"{0}"' -f $launcher
    $shortcut.WorkingDirectory = $Here
    $shortcut.Description = 'Claudle — Discord PC control bot'
    $shortcut.IconLocation = $pythonw
    $shortcut.Save()

    Write-Host "Installed $LinkPath"
    Write-Host "  runs: $pythonw `"$launcher`""
    Write-Host "  state: Disabled"
    Write-Host ""
    Write-Host "Turn it on in Task Manager > Startup apps > Claudle > Enable,"
    Write-Host "or run: .\install-startup.ps1 -Action Enable"
    Write-Host "If Task Manager is already open, close and reopen it to see the entry."
}

function Invoke-Uninstall {
    if (Test-Path $LinkPath) {
        Remove-Item $LinkPath -Force
        Write-Host "Removed $LinkPath"
    }
    else {
        Write-Host "No shortcut at $LinkPath"
    }
    if (Test-Path $ApprovedKey) {
        Remove-ItemProperty -Path $ApprovedKey -Name $LinkName -ErrorAction SilentlyContinue
    }
    Write-Host "Claudle no longer runs at startup."
}

function Invoke-Status {
    if (-not (Test-Path $LinkPath)) {
        Write-Host "Not installed. Run: .\install-startup.ps1"
        return
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($LinkPath)
    Write-Host "Shortcut: $LinkPath"
    Write-Host "  target:  $($shortcut.TargetPath) $($shortcut.Arguments)"
    Write-Host "  workdir: $($shortcut.WorkingDirectory)"
    Write-Host "  state:   $(Get-Approval)"
    $log = Join-Path $Here 'startup.log'
    if (Test-Path $log) { Write-Host "  log:     $log" }
}

function Assert-Installed {
    if (-not (Test-Path $LinkPath)) {
        throw "Claudle is not installed as a startup app yet. Run: .\install-startup.ps1"
    }
}

switch ($Action) {
    'Install' { Invoke-Install }
    'Enable' {
        Assert-Installed
        Set-Approval -Enabled:$true
        Write-Host "Claudle will start at your next sign-in."
    }
    'Disable' {
        Assert-Installed
        Set-Approval -Enabled:$false
        Write-Host "Claudle will no longer start at sign-in."
    }
    'Status' { Invoke-Status }
    'Uninstall' { Invoke-Uninstall }
}
