$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogPath = Join-Path $LogDir 'refresh.log'

Set-Location $ProjectRoot
"`n===== $(Get-Date -Format s) refresh started =====" | Add-Content -LiteralPath $LogPath
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
python app.py refresh --latest-only *>> $LogPath
$ExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference
"===== $(Get-Date -Format s) refresh finished ($ExitCode) =====" | Add-Content -LiteralPath $LogPath
exit $ExitCode
