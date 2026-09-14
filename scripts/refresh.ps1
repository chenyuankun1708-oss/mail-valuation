$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogPath = Join-Path $LogDir 'refresh.log'
$RotateScript = Join-Path $PSScriptRoot 'rotate_refresh_log.ps1'

Set-Location $ProjectRoot
& $RotateScript -LogPath $LogPath
"`n===== $(Get-Date -Format s) refresh started =====" | Add-Content -LiteralPath $LogPath
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& python app.py refresh --latest-only 2>&1 | Tee-Object -FilePath $LogPath -Append
$ExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference
$Stopwatch.Stop()
$Elapsed = $Stopwatch.Elapsed.ToString('hh\:mm\:ss\.f')
$Finished = "===== $(Get-Date -Format s) refresh finished ($ExitCode), elapsed $Elapsed ====="
$Finished | Tee-Object -FilePath $LogPath -Append
if ($ExitCode -eq 0) {
    $BackupDir = [Environment]::GetEnvironmentVariable('BACKUP_DIR')
    if (-not $BackupDir -and (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env'))) {
        $BackupLine = Get-Content -LiteralPath (Join-Path $ProjectRoot '.env') -Encoding UTF8 | Where-Object { $_ -match '^\s*BACKUP_DIR\s*=' } | Select-Object -Last 1
        if ($BackupLine) { $BackupDir = ($BackupLine -split '=', 2)[1].Trim() }
    }
    if ($BackupDir) {
        & python app.py backup --backup-root $BackupDir 2>&1 | Tee-Object -FilePath $LogPath -Append
        if ($LASTEXITCODE -ne 0) { $ExitCode = $LASTEXITCODE }
    } else {
        'BACKUP_DIR未配置，本次刷新未执行业务数据备份。' | Tee-Object -FilePath $LogPath -Append
    }
}
exit $ExitCode
