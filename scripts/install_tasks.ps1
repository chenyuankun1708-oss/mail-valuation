$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$ShareScript = Join-Path $PSScriptRoot 'start_tailscale_share.ps1'
$RefreshScript = Join-Path $PSScriptRoot 'refresh.ps1'
$EnvPath = Join-Path $ProjectRoot '.env'

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process -FilePath $PowerShell -Verb RunAs -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`""
    ) -Wait
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $ShareScript)) { throw 'Missing scripts\start_tailscale_share.ps1.' }
if (-not (Test-Path -LiteralPath $EnvPath)) { throw 'Run scripts\set_share_credentials.ps1 first.' }
$EnvText = Get-Content -LiteralPath $EnvPath -Raw -Encoding UTF8
if ($EnvText -notmatch '(?m)^\s*SHARE_USER\s*=.+$' -or $EnvText -notmatch '(?m)^\s*SHARE_PASSWORD\s*=.{12,}$') {
    throw 'Run scripts\set_share_credentials.ps1 and set a password of at least 12 characters.'
}

$ShareAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ShareScript`""
$ShareTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$ShareSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'FOF Valuation Share' -Action $ShareAction -Trigger $ShareTrigger `
    -Settings $ShareSettings -User $env:USERNAME -RunLevel Highest `
    -Description 'Start the password-protected page through the persistent Tailscale Funnel URL' -Force | Out-Null

$RefreshAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RefreshScript`""
$RefreshTrigger = New-ScheduledTaskTrigger -Daily -At '18:30'
$RefreshSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'FOF Valuation Daily Refresh' -Action $RefreshAction -Trigger $RefreshTrigger `
    -Settings $RefreshSettings -Description 'Download, organize and rebuild the page every day at 18:30' -Force | Out-Null

Write-Host 'Installed tasks: FOF Valuation Share, FOF Valuation Daily Refresh'
Write-Host "Project directory: $ProjectRoot"
