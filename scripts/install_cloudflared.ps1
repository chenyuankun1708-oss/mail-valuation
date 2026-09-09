$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot '.runtime'
$Target = Join-Path $RuntimeDir 'cloudflared.exe'
$DownloadUrl = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
Write-Host "正在从Cloudflare官方发布页下载：$DownloadUrl"
Invoke-WebRequest -Uri $DownloadUrl -OutFile $Target -UseBasicParsing
& $Target --version
Write-Host "已安装到：$Target"
