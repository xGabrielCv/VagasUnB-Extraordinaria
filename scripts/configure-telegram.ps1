param([switch]$SaveOnly, [switch]$Public)
. "$PSScriptRoot/common.ps1"
$secretsDir = if ($Public) { Join-Path $PrivateRoot 'public/secrets' } else { Join-Path $PrivateRoot 'secrets' }
New-Item -ItemType Directory -Force $secretsDir | Out-Null
$secret=Read-Host 'Token futuro (somente gravação; nenhuma conexão)' -AsSecureString
$ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $plain=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ($plain -notmatch '^\d+:[A-Za-z0-9_-]{20,}$') { throw 'Formato de token inválido.' }
    [IO.File]::WriteAllText((Join-Path $secretsDir 'telegram_token'),$plain)
} finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $plain=$null }
Write-Host 'Token gravado privadamente. Nenhum método Telegram foi chamado; modo e serviços não foram alterados.'
