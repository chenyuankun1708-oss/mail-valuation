param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$Python = (Get-Command python -ErrorAction Stop).Source
$BundledCloudflared = Join-Path $ProjectRoot '.runtime\cloudflared.exe'
$CloudflaredCommand = Get-Command cloudflared -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $BundledCloudflared) {
    $Cloudflared = $BundledCloudflared
}
elseif ($CloudflaredCommand) {
    $Cloudflared = $CloudflaredCommand.Source
}
else {
    throw 'cloudflared was not found. Run scripts\install_cloudflared.ps1 first.'
}
$ServerOut = Join-Path $LogDir 'share-server.log'
$ServerErr = Join-Path $LogDir 'share-server-error.log'
$TunnelLog = Join-Path $LogDir 'cloudflared.log'
$UrlFile = Join-Path $LogDir 'share-url.txt'

$Server = Start-Process -FilePath $Python -ArgumentList @('app.py', 'share', '--port', $Port, '--no-browser') `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $ServerOut `
    -RedirectStandardError $ServerErr -PassThru

try {
    Start-Sleep -Seconds 2
    if ($Server.HasExited) {
        throw "Share server failed to start. See $ServerErr"
    }
    while ($true) {
        # cloudflared writes normal informational messages to stderr. PowerShell 5
        # turns those into NativeCommandError when ErrorActionPreference is Stop.
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $Cloudflared tunnel --no-autoupdate --url ("http://127.0.0.1:{0}" -f $Port) 2>&1 | ForEach-Object {
            $Line = $_.ToString()
            Add-Content -LiteralPath $TunnelLog -Value $Line
            if ($Line -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
                Set-Content -LiteralPath $UrlFile -Value $Matches[0] -Encoding UTF8
            }
        }
        $ErrorActionPreference = $PreviousErrorActionPreference
        Add-Content -LiteralPath $TunnelLog -Value ("{0} tunnel exited; retrying in 10 seconds" -f (Get-Date -Format s))
        Start-Sleep -Seconds 10
    }
}
finally {
    if ($Server -and -not $Server.HasExited) {
        Stop-Process -Id $Server.Id -Force
    }
}
