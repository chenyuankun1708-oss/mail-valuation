param(
    [string]$LogPath = '',
    [long]$MaxBytes = 52428800,
    [int]$KeepArchives = 30,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $LogPath) { $LogPath = Join-Path $ProjectRoot 'logs\refresh.log' }
$LogPath = [IO.Path]::GetFullPath($LogPath)
$LogDir = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
if (-not (Test-Path -LiteralPath $LogPath)) {
    New-Item -ItemType File -Path $LogPath -Force | Out-Null
    exit 0
}

$File = Get-Item -LiteralPath $LogPath
$Now = Get-Date
$CrossedMonth = $File.Length -gt 0 -and ($File.LastWriteTime.Year -ne $Now.Year -or $File.LastWriteTime.Month -ne $Now.Month)
if (-not $Force -and $File.Length -lt $MaxBytes -and -not $CrossedMonth) { exit 0 }
if ($File.Length -eq 0) { exit 0 }

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Stamp = $Now.ToString('yyyyMMdd-HHmmss')
$ArchivePath = Join-Path $LogDir ("refresh-{0}.zip" -f $Stamp)
$Counter = 1
while (Test-Path -LiteralPath $ArchivePath) {
    $ArchivePath = Join-Path $LogDir ("refresh-{0}-{1}.zip" -f $Stamp, $Counter)
    $Counter++
}
$TemporaryArchive = $ArchivePath + '.tmp'
$SourceHash = (Get-FileHash -LiteralPath $LogPath -Algorithm SHA256).Hash
$SourceLength = $File.Length

try {
    $ArchiveStream = [IO.File]::Open($TemporaryArchive, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        $Archive = New-Object IO.Compression.ZipArchive($ArchiveStream, [IO.Compression.ZipArchiveMode]::Create, $true)
        try {
            $Entry = $Archive.CreateEntry('refresh.log', [IO.Compression.CompressionLevel]::Optimal)
            $Input = [IO.File]::OpenRead($LogPath)
            $Output = $Entry.Open()
            try { $Input.CopyTo($Output, 1048576) } finally { $Output.Dispose(); $Input.Dispose() }
        } finally { $Archive.Dispose() }
    } finally { $ArchiveStream.Dispose() }

    $VerifyStream = [IO.File]::OpenRead($TemporaryArchive)
    try {
        $VerifyArchive = New-Object IO.Compression.ZipArchive($VerifyStream, [IO.Compression.ZipArchiveMode]::Read, $false)
        try {
            $Entry = $VerifyArchive.GetEntry('refresh.log')
            if ($null -eq $Entry -or $Entry.Length -ne $SourceLength) { throw 'Rotated log length verification failed.' }
            $Hasher = [Security.Cryptography.SHA256]::Create()
            $EntryStream = $Entry.Open()
            try { $VerifiedHash = ([BitConverter]::ToString($Hasher.ComputeHash($EntryStream))).Replace('-', '') } finally { $EntryStream.Dispose(); $Hasher.Dispose() }
            if ($VerifiedHash -ne $SourceHash) { throw 'Rotated log hash verification failed.' }
        } finally { $VerifyArchive.Dispose() }
    } finally { $VerifyStream.Dispose() }

    Move-Item -LiteralPath $TemporaryArchive -Destination $ArchivePath
    Remove-Item -LiteralPath $LogPath
    New-Item -ItemType File -Path $LogPath | Out-Null
} catch {
    if (Test-Path -LiteralPath $TemporaryArchive) { Remove-Item -LiteralPath $TemporaryArchive }
    throw
}

$Archives = Get-ChildItem -LiteralPath $LogDir -Filter 'refresh-*.zip' -File | Sort-Object LastWriteTime -Descending
$Archives | Select-Object -Skip $KeepArchives | Remove-Item -Force
Write-Output $ArchivePath
