param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
$LogPath = Join-Path $LogDir 'tailscale-share.log'
$UrlFile = Join-Path $LogDir 'share-url.txt'
$ServerOut = Join-Path $LogDir 'share-server.log'
$ServerErr = Join-Path $LogDir 'share-server-error.log'
$EnvPath = Join-Path $ProjectRoot '.env'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-ShareLog([string]$Message) {
    $Line = '{0} {1}' -f (Get-Date -Format s), $Message
    Add-Content -LiteralPath $LogPath -Value $Line -Encoding UTF8
    Write-Host $Line
}

trap {
    $LineNumber = $_.InvocationInfo.ScriptLineNumber
    $ErrorType = $_.Exception.GetType().Name
    Write-ShareLog ("Startup failed at line {0} ({1})." -f $LineNumber, $ErrorType)
    exit 1
}

function Find-Tailscale {
    $Command = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    $Candidates = @(
        (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Tailscale\tailscale.exe')
    )
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate)) { return $Candidate }
    }
    throw 'Tailscale is not installed. Run scripts\setup_tailscale_share.ps1 first.'
}

function Test-LocalShare([int]$LocalPort) {
    try {
        Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/" -f $LocalPort) -Method Head `
            -UseBasicParsing -TimeoutSec 3 | Out-Null
        return $true
    }
    catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) { return $true }
        return $false
    }
}

function Test-PortOpen([int]$LocalPort) {
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Pending = $Client.BeginConnect('127.0.0.1', $LocalPort, $null, $null)
        return $Pending.AsyncWaitHandle.WaitOne(400, $false) -and $Client.Connected
    }
    catch { return $false }
    finally { $Client.Close() }
}

function Test-CurrentLocalShare([int]$LocalPort, [hashtable]$Headers) {
    try {
        $Response = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/v2/bootstrap" -f $LocalPort) `
            -Headers $Headers -UseBasicParsing -TimeoutSec 5
        return $Response.StatusCode -eq 200 -and $Response.Content -match '"navigation"'
    }
    catch { return $false }
}

if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw 'Missing .env. Run scripts\set_share_credentials.ps1 first.'
}
$EnvText = Get-Content -LiteralPath $EnvPath -Raw -Encoding UTF8
if ($EnvText -notmatch '(?m)^\s*SHARE_USER\s*=.+$' -or $EnvText -notmatch '(?m)^\s*SHARE_PASSWORD\s*=.{12,}$') {
    throw 'SHARE_USER or SHARE_PASSWORD is missing, or the password is shorter than 12 characters.'
}
$UserMatch = [regex]::Match($EnvText, '(?m)^\s*SHARE_USER\s*=\s*([^\r\n]+)')
$PasswordMatch = [regex]::Match($EnvText, '(?m)^\s*SHARE_PASSWORD\s*=\s*([^\r\n]+)')
$ShareUser = $UserMatch.Groups[1].Value.Trim().Trim('"').Trim("'")
$SharePassword = $PasswordMatch.Groups[1].Value.Trim().Trim('"').Trim("'")
$CredentialBytes = [Text.Encoding]::UTF8.GetBytes(('{0}:{1}' -f $ShareUser, $SharePassword))
$AuthHeaders = @{ Authorization = 'Basic ' + [Convert]::ToBase64String($CredentialBytes) }

$Tailscale = Find-Tailscale
$StatusText = (& $Tailscale status --json 2>$null | Out-String)
if ($LASTEXITCODE -ne 0 -or $StatusText -notmatch '"BackendState"\s*:\s*"Running"') {
    $TailscaleIpn = Join-Path (Split-Path -Parent $Tailscale) 'tailscale-ipn.exe'
    if (Test-Path -LiteralPath $TailscaleIpn) {
        Start-Process -FilePath $TailscaleIpn -WindowStyle Hidden | Out-Null
        Write-ShareLog 'Tailscale was not ready; the desktop client was started automatically.'
        Start-Sleep -Seconds 5
        $StatusText = (& $Tailscale status --json 2>$null | Out-String)
    }
    if ($LASTEXITCODE -ne 0 -or $StatusText -notmatch '"BackendState"\s*:\s*"Running"') {
        throw 'Tailscale is not logged in or connected. Open Tailscale, sign in, and retry.'
    }
}
$DnsMatch = [regex]::Match($StatusText, '"DNSName"\s*:\s*"([^"\\]+)"')
$DnsName = if ($DnsMatch.Success) { $DnsMatch.Groups[1].Value } else { '' }
if (-not $DnsName) { throw 'Tailscale did not return a MagicDNS name. Enable MagicDNS and retry.' }
$DnsName = $DnsName.TrimEnd('.')

$ActivePort = $Port
$CurrentShare = Test-CurrentLocalShare $ActivePort $AuthHeaders
if (-not $CurrentShare -and (Test-PortOpen $ActivePort)) {
    $RequestedPort = $ActivePort
    $ActivePort = $null
    foreach ($Candidate in (($RequestedPort + 1)..($RequestedPort + 10))) {
        if (Test-CurrentLocalShare $Candidate $AuthHeaders) {
            $ActivePort = $Candidate
            $CurrentShare = $true
            break
        }
        if (-not (Test-PortOpen $Candidate)) {
            $ActivePort = $Candidate
            break
        }
    }
    if ($null -eq $ActivePort) { throw 'No available loopback fallback port was found.' }
    Write-ShareLog ("Port {0} is occupied by an outdated or unrelated service; using {1}." -f $RequestedPort, $ActivePort)
}

if (-not $CurrentShare) {
    $VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { (Get-Command python -ErrorAction Stop).Source }
    Start-Process -FilePath $Python -ArgumentList @('app.py', 'share', '--port', $ActivePort, '--no-browser') `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $ServerOut `
        -RedirectStandardError $ServerErr | Out-Null
    Start-Sleep -Seconds 3
    if (-not (Test-CurrentLocalShare $ActivePort $AuthHeaders)) { throw "The current web server failed to start. See $ServerErr." }
    Write-ShareLog 'The password-protected local web server started.'
}
else {
    Write-ShareLog 'The current local web server is already running; no duplicate was started.'
}

$FunnelOutput = & $Tailscale funnel --bg --yes $ActivePort 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-ShareLog ("Funnel failed with exit code $LASTEXITCODE. Command output was not logged because it may contain an authorization URL.")
    throw 'Tailscale Funnel failed. Initial enablement may require an Administrator PowerShell and browser approval.'
}
$Url = 'https://' + $DnsName + '/#home'
Set-Content -LiteralPath $UrlFile -Value $Url -Encoding UTF8
Write-ShareLog ("Stable share URL: $Url")
Write-Host 'The browser will still require SHARE_USER and SHARE_PASSWORD from .env.'
