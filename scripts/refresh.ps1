$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogPath = Join-Path $LogDir 'refresh.log'

Set-Location $ProjectRoot
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
exit $ExitCode
