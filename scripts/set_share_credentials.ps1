$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $ProjectRoot '.env'

$User = Read-Host 'Share username (recommended: viewer)'
$SecurePassword = Read-Host 'Share password (at least 12 characters)' -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}
if ([string]::IsNullOrWhiteSpace($User)) { throw 'Username cannot be empty' }
if ($Password.Length -lt 12) { throw 'Password must contain at least 12 characters' }

$Lines = if (Test-Path -LiteralPath $EnvPath) { @(Get-Content -LiteralPath $EnvPath -Encoding UTF8) } else { @() }
$Lines = @($Lines | Where-Object { $_ -notmatch '^\s*SHARE_(USER|PASSWORD)\s*=' })
$Lines += "SHARE_USER=$User"
$Lines += "SHARE_PASSWORD=$Password"
Set-Content -LiteralPath $EnvPath -Value $Lines -Encoding UTF8
Write-Host 'Credentials saved to the git-ignored .env file. Restart the share service to apply them.'
