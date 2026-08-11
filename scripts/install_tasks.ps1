$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$ShareScript = Join-Path $PSScriptRoot 'start_share.ps1'
$RefreshScript = Join-Path $PSScriptRoot 'refresh.ps1'
$EnvPath = Join-Path $ProjectRoot '.env'
$Cloudflared = Join-Path $ProjectRoot '.runtime\cloudflared.exe'

if (-not (Test-Path -LiteralPath $Cloudflared)) { throw 'Run scripts\install_cloudflared.ps1 first.' }
if (-not (Test-Path -LiteralPath $EnvPath)) { throw 'Run scripts\set_share_credentials.ps1 first.' }
$EnvText = Get-Content -LiteralPath $EnvPath -Raw -Encoding UTF8
if ($EnvText -notmatch '(?m)^\s*SHARE_USER\s*=.+$' -or $EnvText -notmatch '(?m)^\s*SHARE_PASSWORD\s*=.{12,}$') {
    throw 'Run scripts\set_share_credentials.ps1 and set a password of at least 12 characters.'
}

$ShareAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ShareScript`""
$ShareTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$ShareSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'FOF Valuation Share' -Action $ShareAction -Trigger $ShareTrigger `
    -Settings $ShareSettings -Description 'Start the password-protected page and Cloudflare Quick Tunnel' -Force | Out-Null

$RefreshAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RefreshScript`""
$RefreshTrigger = New-ScheduledTaskTrigger -Daily -At '18:30'
$RefreshSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'FOF Valuation Daily Refresh' -Action $RefreshAction -Trigger $RefreshTrigger `
    -Settings $RefreshSettings -Description 'Download, organize and rebuild the page every day at 18:30' -Force | Out-Null

Write-Host 'Installed tasks: FOF Valuation Share, FOF Valuation Daily Refresh'
Write-Host "Project directory: $ProjectRoot"
