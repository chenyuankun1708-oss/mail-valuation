$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogPath = Join-Path $LogDir 'refresh.log'
$RotateScript = Join-Path $PSScriptRoot 'rotate_refresh_log.ps1'
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$PythonExe = if (Test-Path -LiteralPath $VenvPython) {
    $VenvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

Set-Location $ProjectRoot
& $RotateScript -LogPath $LogPath
"`n===== $(Get-Date -Format s) refresh started =====" | Out-File -LiteralPath $LogPath -Encoding utf8 -Append
"Python: $PythonExe" | Out-File -LiteralPath $LogPath -Encoding utf8 -Append
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PythonExe app.py refresh --latest-only 2>&1 | ForEach-Object {
    $Line = [string]$_
    Write-Output $Line
    $Line | Out-File -LiteralPath $LogPath -Encoding utf8 -Append
}
$ExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference
$Stopwatch.Stop()
$Elapsed = $Stopwatch.Elapsed.ToString('hh\:mm\:ss\.f')
$Finished = "===== $(Get-Date -Format s) refresh finished ($ExitCode), elapsed $Elapsed ====="
$Finished | Out-File -LiteralPath $LogPath -Encoding utf8 -Append
Write-Output $Finished
if ($ExitCode -eq 0) {
    $BackupDir = [Environment]::GetEnvironmentVariable('BACKUP_DIR')
    if (-not $BackupDir -and (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env'))) {
        $BackupLine = Get-Content -LiteralPath (Join-Path $ProjectRoot '.env') -Encoding UTF8 | Where-Object { $_ -match '^\s*BACKUP_DIR\s*=' } | Select-Object -Last 1
        if ($BackupLine) { $BackupDir = ($BackupLine -split '=', 2)[1].Trim() }
    }
    if ($BackupDir) {
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $PythonExe app.py backup --backup-root $BackupDir 2>&1 | ForEach-Object {
            $Line = [string]$_
            Write-Output $Line
            $Line | Out-File -LiteralPath $LogPath -Encoding utf8 -Append
        }
        $BackupExitCode = $LASTEXITCODE
        $ErrorActionPreference = $PreviousErrorActionPreference
        if ($BackupExitCode -ne 0) { $ExitCode = $BackupExitCode }
    } else {
        $BackupWarning = 'BACKUP_DIR is not configured; business backup was skipped.'
        $BackupWarning | Out-File -LiteralPath $LogPath -Encoding utf8 -Append
        Write-Output $BackupWarning
    }
}
exit $ExitCode
