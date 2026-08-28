param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot 'start_tailscale_share.ps1'

function Test-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    $PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    Start-Process -FilePath $PowerShell -Verb RunAs -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath),
        '-Port', $Port
    )
    Write-Host 'Administrator permission requested. Continue in the new window.'
    exit 0
}

$Tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $Tailscale) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
        Write-Host 'Installing Tailscale through winget...'
        & $Winget.Source install --id Tailscale.Tailscale --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw 'Tailscale installation through winget failed.' }
    }
    else {
        $Installer = Join-Path $env:TEMP 'tailscale-setup-full-1.102.3.exe'
        $DownloadUrl = 'https://pkgs.tailscale.com/stable/tailscale-setup-full-1.102.3.exe'
        Write-Host 'winget is unavailable. Downloading the signed Tailscale installer...'
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $Installer -UseBasicParsing
        $Signature = Get-AuthenticodeSignature -FilePath $Installer
        if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Tailscale') {
            throw 'The downloaded Tailscale installer does not have a valid Tailscale signature.'
        }
        Start-Process -FilePath $Installer -Wait
        Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
    }
}

$TailscalePath = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (-not (Test-Path -LiteralPath $TailscalePath)) {
    $Resolved = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $Resolved) { throw 'Tailscale is installed but tailscale.exe is not visible yet. Open a new PowerShell and rerun.' }
    $TailscalePath = $Resolved.Source
}

$StatusText = (& $TailscalePath status --json 2>$null | Out-String)
$Status = $StatusText | ConvertFrom-Json
if (-not $Status -or $Status.BackendState -ne 'Running') {
    Write-Host 'Follow the prompt to sign in to Tailscale in your browser.'
    & $TailscalePath up
    if ($LASTEXITCODE -ne 0) { throw 'Tailscale sign-in or connection did not complete.' }
}

# Do not use --yes here: the first Funnel enablement must expose Tailscale's
# browser approval flow. Later startup runs use --yes after approval exists.
Write-Host 'Enabling Funnel. Approve the browser prompt if Tailscale opens one.'
& $TailscalePath funnel --bg $Port
if ($LASTEXITCODE -ne 0) { throw 'Initial Tailscale Funnel approval did not complete.' }

Set-Location $ProjectRoot
& $StartScript -Port $Port
if ($LASTEXITCODE -ne 0) { throw 'Stable sharing setup failed.' }
Write-Host 'Initial setup completed. Next, rerun scripts\install_tasks.ps1 to install startup tasks.'
